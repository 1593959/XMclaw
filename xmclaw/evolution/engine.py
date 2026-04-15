"""Evolution engine: observe -> learn -> evolve -> solidify."""
import json
import uuid
from datetime import datetime
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

        for decision in decisions:
            if decision["type"] == "gene":
                gene = await self._generate_gene(decision)
                if gene:
                    results["actions"].append({"type": "gene", "id": gene["id"]})
            elif decision["type"] == "skill":
                skill = await self._generate_skill(decision)
                if skill:
                    results["actions"].append({"type": "skill", "id": skill["id"]})

        logger.info("evolution_cycle_end", agent_id=self.agent_id, results=results)
        return results

    async def _get_recent_sessions(self) -> list[dict]:
        if not self.memory.sessions:
            return []
        return await self.memory.sessions.get_all(self.agent_id)

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
            gene_data = json.loads(text.strip().strip("`").replace("json", "").strip())
            concept = {
                "name": gene_data.get("name", "Unnamed Gene"),
                "description": gene_data.get("description", ""),
                "trigger": gene_data.get("trigger", ""),
                "action": gene_data.get("action", ""),
                "source": decision["insight"].get("source", ""),
            }

            # VFM scoring
            scores = self.vfm.score_gene(concept)
            threshold = self.config.evolution.get("vfm_threshold", 20.0)
            if not self.vfm.should_solidify(scores, threshold):
                logger.info("gene_rejected_by_vfm", concept=concept["name"], score=scores["total"])
                return None

            # Forge executable code
            gene = await self.gene_forge.forge(concept)
            if not gene:
                return None

            # Validate
            validation = self.validator.validate_gene(Path(gene["path"]))
            if not validation["passed"]:
                logger.warning("gene_validation_failed", gene_id=gene["id"], errors=validation)
                return None

            # Save to DB
            db = SQLiteStore(self.db_path)
            db.insert_gene(self.agent_id, gene)
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

        # Forge executable code
        skill = await self.skill_forge.forge(concept)
        if not skill:
            return None

        # Validate
        validation = self.validator.validate_skill(Path(skill["path"]))
        if not validation["passed"]:
            logger.warning("skill_validation_failed", skill_id=skill["id"], errors=validation)
            return None

        # Register in DB
        db = SQLiteStore(self.db_path)
        db.insert_skill(self.agent_id, skill)
        logger.info("skill_generated_and_validated", skill_id=skill["id"], name=skill["name"], score=scores["total"])
        return skill
