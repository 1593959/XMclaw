"""Evolution engine: observe -> learn -> evolve -> solidify."""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from xmclaw.memory.manager import MemoryManager
from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.llm.router import LLMRouter
from xmclaw.core.prompt_builder import PromptBuilder
from xmclaw.evolution.vfm import VFMScorer
from xmclaw.evolution.gene_forge import GeneForge
from xmclaw.evolution.skill_forge import SkillForge
from xmclaw.evolution.validator import EvolutionValidator
from xmclaw.daemon.config import DaemonConfig
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


async def _reload_tool_registry() -> None:
    """Reload generated skills into the global tool registry."""
    try:
        from xmclaw.tools.registry import ToolRegistry
        registry = ToolRegistry()
        await registry._load_generated_skills()
        logger.info("tool_registry_reloaded_after_skill")
    except Exception as e:
        logger.warning("tool_registry_reload_failed", error=str(e))


class EvolutionEngine:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.llm = LLMRouter()
        self.builder = PromptBuilder()
        self.memory = MemoryManager()
        self.db_path = BASE_DIR / "shared" / "memory.db"
        self.vfm = VFMScorer()
        self.gene_forge = GeneForge()
        self.skill_forge = SkillForge()
        self.validator = EvolutionValidator()
        self.config = DaemonConfig.load()

    async def initialize(self) -> None:
        await self.memory.initialize()

    async def run_cycle(self) -> dict[str, Any]:
        """Run one full evolution cycle."""
        logger.info("evolution_cycle_start", agent_id=self.agent_id)

        # 1. Observe: get recent sessions and insights
        sessions = await self._get_recent_sessions()
        insights = self._extract_insights(sessions)

        if not insights:
            logger.info("evolution_no_insights", agent_id=self.agent_id)
            return {"status": "no_insights", "insights": 0}

        # 2. Learn: store insights
        for insight in insights:
            self.memory.save_insight(self.agent_id, insight)

        # 3. Evolve: decide whether to create Gene or Skill
        decisions = await self._decide_evolution(insights)

        results = {"status": "running", "insights": len(insights), "actions": []}

        # Run generation in parallel for speed
        import asyncio
        coros = []
        for decision in decisions:
            if decision["type"] == "gene":
                coros.append(self._generate_gene(decision))
            elif decision["type"] == "skill":
                coros.append(self._generate_skill(decision))

        generated = await asyncio.gather(*coros, return_exceptions=True)
        for item in generated:
            if isinstance(item, Exception):
                logger.error("evolution_generation_error", error=str(item))
                continue
            if item:
                results["actions"].append({"type": item.get("type", "unknown"), "id": item["id"]})

        # 4. Record and notify
        await self._record_results(results)
        if results["actions"]:
            await self._notify_user(results)

        logger.info("evolution_cycle_end", agent_id=self.agent_id, results=results)
        return results

    async def _record_results(self, results: dict[str, Any]) -> None:
        """Write evolution results to long-term memory (MEMORY.md)."""
        memory_md = BASE_DIR.parent / "MEMORY.md"
        if not memory_md.exists():
            memory_md = BASE_DIR / "MEMORY.md"
        if not memory_md.exists():
            return

        lines = [f"\n- **{datetime.now().strftime('%Y-%m-%d %H:%M')}** 进化循环结果:"]
        if not results["actions"]:
            lines.append("  - 无实质产出")
        else:
            for action in results["actions"]:
                atype = action["type"]
                aid = action["id"]
                lines.append(f"  - {atype.upper()} 生成: `{aid}`")

        content = "\n".join(lines)
        try:
            with open(memory_md, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except Exception as e:
            logger.warning("evolution_memory_write_failed", error=str(e))

        # Also append to daily log
        from xmclaw.utils.paths import get_agent_dir
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = get_agent_dir(self.agent_id) / "memory" / f"{today}.md"
        daily_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(daily_log, "a", encoding="utf-8") as f:
                f.write(f"\n## 进化记录 [{datetime.now().strftime('%H:%M')}]\n")
                if not results["actions"]:
                    f.write("- 无实质产出\n")
                else:
                    for action in results["actions"]:
                        f.write(f"- {action['type'].upper()}: `{action['id']}`\n")
        except Exception as e:
            logger.warning("evolution_daily_log_write_failed", error=str(e))

    async def _notify_user(self, results: dict[str, Any]) -> None:
        """Proactively notify user of evolution results via desktop/web if available."""
        try:
            from xmclaw.daemon.server import app
            # For now, just log. In the future, push via WebSocket or desktop notification.
            actions = results.get("actions", [])
            summary = ", ".join(f"{a['type']} {a['id']}" for a in actions)
            logger.info("evolution_notify", summary=summary)
        except Exception:
            pass

    async def _get_recent_sessions(self) -> list[dict]:
        if not self.memory.sessions:
            return []
        return await self.memory.sessions.get_recent(self.agent_id, limit=20)

    def _extract_insights(self, sessions: list[dict]) -> list[dict]:
        """Extract patterns from sessions."""
        insights = []

        # Count tool usage
        tool_counts: dict[str, int] = {}
        for session in sessions:
            for call in session.get("tool_calls", []):
                name = call.get("name", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1

        for tool, count in tool_counts.items():
            if count >= 3:
                insights.append({
                    "type": "pattern",
                    "title": f"Frequent {tool} usage",
                    "description": f"Tool '{tool}' was used {count} times recently.",
                    "source": "tool_usage_analysis",
                })

        # Detect negative feedback
        for session in sessions:
            user_msg = session.get("user", "").lower()
            if any(w in user_msg for w in ["wrong", "error", "fix", "broken", "not working"]):
                insights.append({
                    "type": "problem",
                    "title": "User reported issue",
                    "description": user_msg[:200],
                    "source": "negative_feedback",
                })

        return insights

    async def _decide_evolution(self, insights: list[dict]) -> list[dict]:
        """Decide what to evolve based on insights."""
        decisions = []
        for insight in insights:
            if insight["type"] == "pattern":
                decisions.append({
                    "type": "skill",
                    "insight": insight,
                    "reason": "Repeated task pattern suggests a skill",
                })
            elif insight["type"] == "problem":
                decisions.append({
                    "type": "gene",
                    "insight": insight,
                    "reason": "Repeated problem suggests a behavior gene",
                })
        return decisions

    async def _generate_gene(self, decision: dict) -> dict[str, Any] | None:
        """Generate a new Gene via LLM, validate with VFM, forge code, run validation."""
        prompt = self.builder.build_evolution_prompt([decision["insight"]])
        try:
            text = await self.llm.complete([{"role": "user", "content": prompt}])
            text = text.strip().strip("`").replace("json", "").strip()
            gene_data = json.loads(text)
            concept = {
                "name": str(gene_data.get("name", "Unnamed Gene")),
                "description": str(gene_data.get("description", "")),
                "trigger": str(gene_data.get("trigger", "")),
                "action": str(gene_data.get("action", "")),
                "source": str(decision["insight"].get("source", "")),
            }
            action_body = gene_data.get("action_body")

            # VFM scoring
            scores = self.vfm.score_gene(concept)
            threshold = self.config.evolution.get("vfm_threshold", 20.0)
            if not self.vfm.should_solidify(scores, threshold):
                logger.info("gene_rejected_by_vfm", concept=concept["name"], score=scores["total"])
                return None

            # Forge executable code (pass action_body to skip second LLM call)
            gene = await self.gene_forge.forge(concept, action_body=action_body)
            if not gene:
                return None

            # Validate
            validation = await self.validator.validate_gene(Path(gene["path"]))
            if not validation["passed"]:
                logger.warning("gene_validation_failed", gene_id=gene["id"], errors=validation)
                return None

            gene["type"] = "gene"
            # Save to DB
            db = SQLiteStore(self.db_path)
            db.insert_gene(self.agent_id, gene)

            # Auto-rollback on failure is handled by validation gate above
            logger.info("gene_generated_and_validated", gene_id=gene["id"], name=gene["name"], score=scores["total"])
            return gene
        except Exception as e:
            logger.error("gene_generation_failed", error=str(e))
            return None

    async def _generate_skill(self, decision: dict) -> dict[str, Any] | None:
        """Generate a new Skill via LLM, validate with VFM, forge code, run validation."""
        insight = decision["insight"]
        concept = {
            "name": f"auto_{insight['title'].replace(' ', '_').lower()}",
            "description": insight["description"],
            "source": insight.get("source", ""),
        }

        # VFM scoring
        scores = self.vfm.score_skill(concept)
        threshold = self.config.evolution.get("vfm_threshold", 20.0)
        if not self.vfm.should_solidify(scores, threshold):
            logger.info("skill_rejected_by_vfm", concept=concept["name"], score=scores["total"])
            return None

        # Try to generate action_body in one LLM call to avoid second forge LLM call
        action_body = None
        try:
            prompt = self.builder.build_evolution_prompt([insight])
            text = await self.llm.complete([{"role": "user", "content": prompt}])
            text = text.strip().strip("`").replace("json", "").strip()
            data = json.loads(text)
            action_body = data.get("action_body")
            if action_body:
                concept["parameters"] = data.get("parameters", {"input": {"type": "string", "description": "Input for the skill"}})
        except Exception:
            pass

        # Forge executable code
        skill = await self.skill_forge.forge(concept, action_body=action_body)
        if not skill:
            return None

        # Validate
        validation = await self.validator.validate_skill(Path(skill["path"]))
        if not validation["passed"]:
            logger.warning("skill_validation_failed", skill_id=skill["id"], errors=validation)
            return None

        skill["type"] = "skill"
        # Register in DB
        db = SQLiteStore(self.db_path)
        db.insert_skill(self.agent_id, skill)

        # Trigger tool registry reload so the new skill is immediately available
        await _reload_tool_registry()

        logger.info("skill_generated_and_validated", skill_id=skill["id"], name=skill["name"], score=scores["total"])
        return skill
