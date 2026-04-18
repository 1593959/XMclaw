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
from xmclaw.core.event_bus import Event, EventType, get_event_bus
from xmclaw.evolution.vfm import VFMScorer
from xmclaw.evolution.gene_forge import GeneForge
from xmclaw.genes.manager import GeneManager
from xmclaw.evolution.skill_forge import SkillForge
from xmclaw.evolution.validator import EvolutionValidator
from xmclaw.daemon.config import DaemonConfig
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


async def _reload_tool_registry(skill_name: str = "") -> None:
    """Reload generated skills into the shared tool registry (the orchestrator's one).

    Also publishes a SKILL_EXECUTED event so the frontend can show real-time feedback.
    """
    try:
        from xmclaw.tools.registry import ToolRegistry
        from xmclaw.core.event_bus import Event, EventType, get_event_bus
        registry = ToolRegistry.get_shared()
        if registry is None:
            logger.warning("tool_registry_reload_skipped_no_shared_instance")
            return
        # Only reload generated skills, not built-in tools
        await registry._load_generated_skills()
        logger.info("tool_registry_reloaded_after_skill", tool_count=len(registry._tools))
        # Notify frontend immediately
        bus = get_event_bus()
        await bus.publish(Event(
            event_type=EventType.SKILL_EXECUTED,
            source="evolution",
            payload={
                "skill_name": skill_name,
                "action": "hot_reloaded",
                "total_skills": len(registry._tools),
            },
        ))
    except Exception as e:
        logger.warning("tool_registry_reload_failed", error=str(e))


class EvolutionEngine:
    def __init__(self, agent_id: str = "default", memory=None):
        """Create evolution engine.

        Args:
            agent_id: Agent to evolve for.
            memory: Optional shared MemoryManager. If provided, the engine reads
                    sessions from this shared instance instead of creating its own.
                    This ensures evolution sees the same session data as the orchestrator.
        """
        self.agent_id = agent_id
        self.llm = LLMRouter()
        self.builder = PromptBuilder()
        self.memory = memory if memory is not None else MemoryManager()
        self.db_path = BASE_DIR / "shared" / "memory.db"
        self.vfm = VFMScorer()
        self.gene_forge = GeneForge()
        self.skill_forge = SkillForge()
        self.validator = EvolutionValidator()
        self.config = DaemonConfig.load()

        # Load configurable thresholds from config
        pattern_cfg = self.config.evolution.get("pattern_thresholds", {})
        self._insight_tool_count = pattern_cfg.get("insight_tool_usage_count", 2)
        self._insight_repeat_count = pattern_cfg.get("insight_repeated_count", 2)
        self._session_limit = pattern_cfg.get("session_limit", 200)

        # Chinese + English problem keywords for better detection
        self._problem_keywords = [
            # English
            "wrong", "error", "fix", "broken", "not working", "bug", "failed",
            "issue", "doesn't work", "dont work", "cannot", "unable to",
            "exception", "crash", "timeout", "refuse",
            # Chinese
            "错误", "失败", "有问题", "不行", "不能用", "坏了", "bug",
            "异常", "崩溃", "超时", "拒绝", "怎么不行", "为什么不行",
            "不对", "有问题", "出错了", "搞不定", "无法", "没办法",
        ]

    async def initialize(self) -> None:
        await self.memory.initialize()

    async def run_cycle(self) -> dict[str, Any]:
        """Run one full evolution cycle."""
        logger.info("evolution_cycle_start", agent_id=self.agent_id)

        # Publish cycle start event
        try:
            bus = get_event_bus()
            await bus.publish(Event(
                event_type=EventType.EVOLUTION_CYCLE,
                source=self.agent_id,
                payload={"phase": "start"},
            ))
        except Exception:
            pass

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

        # Limit generation per cycle to prevent overwhelming the system
        max_per_type = self.config.evolution.get("max_genes_per_day", 10)
        # Use a smaller limit per cycle (e.g., max 3 of each type per cycle)
        max_per_cycle = min(3, max_per_type)

        # Separate genes and skills, limit each
        gene_decisions = [d for d in decisions if d["type"] == "gene"][:max_per_cycle]
        skill_decisions = [d for d in decisions if d["type"] == "skill"][:max_per_cycle]
        limited_decisions = gene_decisions + skill_decisions

        results = {"status": "running", "insights": len(insights), "decisions": len(limited_decisions), "actions": []}

        logger.info("evolution_generation_start",
                   total_decisions=len(decisions),
                   limited_decisions=len(limited_decisions),
                   genes=len(gene_decisions),
                   skills=len(skill_decisions))

        # Run generation in parallel for speed
        import asyncio
        coros = []
        for decision in limited_decisions:
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

        # Publish cycle end event
        try:
            bus = get_event_bus()
            await bus.publish(Event(
                event_type=EventType.EVOLUTION_CYCLE,
                source=self.agent_id,
                payload={"phase": "end", "results": results},
            ))
        except Exception:
            pass

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
        """Publish evolution results as an EventBus event — all WebSocket clients receive it."""
        try:
            from xmclaw.core.event_bus import Event, EventType, get_event_bus
            actions = results.get("actions", [])
            summary = ", ".join(f"{a['type']} {a['id']}" for a in actions) or "Evolution cycle completed"
            genes = [a for a in actions if a["type"] == "gene"]
            skills = [a for a in actions if a["type"] == "skill"]
            await get_event_bus().publish(Event(
                event_type=EventType.EVOLUTION_NOTIFY,
                source=f"evolution:{self.agent_id}",
                payload={
                    "summary": summary,
                    "gene_count": len(genes),
                    "skill_count": len(skills),
                    "actions": actions,
                },
            ))
            logger.info("evolution_notify_published", summary=summary, genes=len(genes), skills=len(skills))
        except Exception as e:
            logger.warning("evolution_notify_failed", error=str(e))

    async def _get_recent_sessions(self) -> list[dict]:
        if not self.memory.sessions:
            return []
        # Look at ALL historical sessions — recent ones often have empty tool_calls
        return await self.memory.sessions.get_recent(self.agent_id, limit=self._session_limit)

    def _extract_insights(self, sessions: list[dict]) -> list[dict]:
        """Extract patterns from sessions with improved multi-dimensional detection.

        Looks across all available sessions (up to _session_limit) since recent sessions
        often have empty tool_calls. Generates insights when:
        - A tool is used >= _insight_tool_count times (configurable, default 2)
        - A user message pattern repeats >= _insight_repeat_count times (configurable, default 2)
        - A user or agent mentions problem/error keywords
        - Agent response quality is low (detected via keywords)
        - Task complexity patterns (code, search, plan types)
        """
        insights = []
        seen_titles: set[str] = set()  # deduplicate

        def add(title: str, insight: dict) -> None:
            if title not in seen_titles:
                seen_titles.add(title)
                insights.append(insight)

        # ── 1. Tool usage patterns (primary signal) ────────────────────────────
        # Count tool usage across all sessions
        tool_counts: dict[str, int] = {}
        tool_sessions: dict[str, list[dict]] = {}
        for session in sessions:
            for call in session.get("tool_calls", []):
                name = call.get("name", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1
                tool_sessions.setdefault(name, []).append(call)

        for tool, count in tool_counts.items():
            if count >= self._insight_tool_count:
                title = f"Frequent {tool} usage"
                add(title, {
                    "type": "pattern",
                    "title": title,
                    "description": f"Tool '{tool}' was used {count} times across recent sessions. "
                                   f"This suggests a repeated workflow that could be automated as a skill.",
                    "source": "tool_usage_analysis",
                    "tool_name": tool,
                    "usage_count": count,
                    "confidence": min(count / 5, 1.0),  # Higher count = higher confidence
                })
                logger.info("insight_tool_pattern", tool=tool, count=count)

        # ── 2. Repeated user message patterns (intent signal) ─────────────────
        user_msg_counts: dict[str, int] = {}
        for session in sessions:
            msg = session.get("user", "").strip()
            if msg:
                # Normalise: collapse whitespace, strip function-call wrappers
                normalized = " ".join(msg.split()).replace("<function>", "").replace("</function>", "").strip()
                if len(normalized) >= 3:
                    user_msg_counts[normalized] = user_msg_counts.get(normalized, 0) + 1

        for msg, count in user_msg_counts.items():
            if count >= self._insight_repeat_count:
                key = f"repeat:{msg[:60]}"
                add(key, {
                    "type": "pattern",
                    "title": f"Repeated user intent: {msg[:50]}",
                    "description": f"Same or similar request repeated {count} times: {msg[:200]}. "
                                   f"This indicates a frequent user need that should be handled more efficiently.",
                    "source": "repeated_request",
                    "repeated_text": msg[:100],
                    "repeat_count": count,
                    "confidence": min(count / 10, 1.0),
                })
                logger.info("insight_repeated_pattern", msg=msg[:50], count=count)

        # ── 3. Problem / error patterns (negative feedback signal) ─────────────
        # Check BOTH user messages and agent responses for problem keywords
        for session in sessions:
            user_msg = session.get("user", "").lower()
            assistant_msg = session.get("assistant", "").lower()

            # Check user for explicit problem reports
            if any(w in user_msg for w in self._problem_keywords):
                raw_msg = session.get("user", "")[:300]
                key = f"problem:{raw_msg[:60]}"
                add(key, {
                    "type": "problem",
                    "title": "User reported issue",
                    "description": raw_msg,
                    "source": "negative_feedback_user",
                    "confidence": 0.9,  # User complaints are high-value signals
                })
                logger.info("insight_problem_user", msg=raw_msg[:80])

            # Check agent response for error patterns (indicates agent struggling)
            if any(w in assistant_msg for w in ["error", "wrong", "cannot", "unable", "failed", "错误", "无法"]):
                assistant_raw = session.get("assistant", "")[:300]
                # Only add if different from user problem (avoid duplicates)
                if assistant_raw[:100] != session.get("user", "")[:100]:
                    key = f"agent_issue:{assistant_raw[:60]}"
                    add(key, {
                        "type": "problem",
                        "title": "Agent response indicates difficulty",
                        "description": f"Agent's response suggests it had difficulty: {assistant_raw[:200]}",
                        "source": "negative_feedback_agent",
                        "confidence": 0.7,
                    })
                    logger.info("insight_problem_agent", msg=assistant_raw[:80])

        # ── 4. Task type patterns (capability signal) ──────────────────────────
        # Detect code-related patterns from user messages
        code_keywords_en = ["code", "python", "javascript", "function", "debug", "bug", "fix"]
        code_keywords_cn = ["代码", "python", "写代码", "调试", "修复", "程序"]
        search_keywords_en = ["search", "find", "lookup", "how to", "what is"]
        search_keywords_cn = ["搜索", "查找", "怎么", "是什么", "如何"]
        plan_keywords_en = ["plan", "analyze", "design", "strategy"]
        plan_keywords_cn = ["规划", "分析", "计划", "设计", "策略"]

        task_patterns: dict[str, list[str]] = {"code": [], "search": [], "plan": []}

        for session in sessions:
            msg = session.get("user", "").lower()
            # Count code-related requests
            if any(kw in msg for kw in code_keywords_en + code_keywords_cn):
                task_patterns["code"].append(session.get("user", "")[:100])
            if any(kw in msg for kw in search_keywords_en + search_keywords_cn):
                task_patterns["search"].append(session.get("user", "")[:100])
            if any(kw in msg for kw in plan_keywords_en + plan_keywords_cn):
                task_patterns["plan"].append(session.get("user", "")[:100])

        # If a task type appears frequently, suggest creating a specialized skill
        for task_type, examples in task_patterns.items():
            if len(examples) >= 3:
                unique_examples = list(dict.fromkeys(examples))[:5]  # Dedupe, keep order, limit 5
                add(f"frequent_{task_type}_tasks", {
                    "type": "pattern",
                    "title": f"Frequent {task_type.upper()} tasks detected",
                    "description": f"Detected {len(examples)} {task_type}-related requests. "
                                   f"Examples: {'; '.join(unique_examples)}. "
                                   f"Consider creating a specialized {task_type} skill for better handling.",
                    "source": "task_type_analysis",
                    "task_type": task_type,
                    "count": len(examples),
                    "confidence": min(len(examples) / 20, 0.9),
                })
                logger.info("insight_task_pattern", task_type=task_type, count=len(examples))

        # ── 5. Long conversation patterns (complexity signal) ───────────────────
        # Detect sessions with many turns (high complexity tasks)
        tool_call_counts = [len(s.get("tool_calls", [])) for s in sessions]
        for i, count in enumerate(tool_call_counts):
            if count >= 5:  # Sessions with 5+ tool calls are complex
                session = sessions[i]
                user_preview = session.get("user", "")[:100]
                add(f"complex_task:{user_preview}", {
                    "type": "pattern",
                    "title": f"Complex task detected ({count} tool calls)",
                    "description": f"A complex multi-step task was performed: {user_preview}. "
                                   f"This suggests the agent should handle similar tasks more efficiently.",
                    "source": "complexity_analysis",
                    "tool_count": count,
                    "confidence": min(count / 20, 0.8),
                })

        logger.info("insights_extracted",
                    total=len(insights),
                    tool_patterns=sum(1 for i in insights if i.get("source") == "tool_usage_analysis"),
                    repeated_patterns=sum(1 for i in insights if i.get("source") == "repeated_request"),
                    problem_patterns=sum(1 for i in insights if i.get("type") == "problem"),
                    task_patterns=sum(1 for i in insights if i.get("source") == "task_type_analysis"),
                    sessions_analyzed=len(sessions))

        return insights

    async def _decide_evolution(self, insights: list[dict]) -> list[dict]:
        """Decide what to evolve based on insights with confidence scoring.

        Decision logic:
        - High confidence pattern (>= 0.7) → skill (automation)
        - Problem insights → gene (behavior fix)
        - Medium confidence pattern (>= 0.4) → skill with caution
        - Low confidence (< 0.4) → skip unless it's a problem
        """
        decisions = []
        # Track what we've already decided to avoid duplicates
        decided_tools: set[str] = set()
        decided_tasks: set[str] = set()

        for insight in insights:
            confidence = insight.get("confidence", 0.5)

            if insight["type"] == "problem":
                # Problems always get a gene (high priority)
                decisions.append({
                    "type": "gene",
                    "insight": insight,
                    "reason": f"Problem detected (confidence: {confidence:.2f}). Needs behavior fix via gene.",
                    "priority": 1,  # Highest priority
                })
                logger.info("evolution_decision", type="gene", reason="problem", confidence=confidence)

            elif insight["type"] == "pattern":
                source = insight.get("source", "")

                if source == "tool_usage_analysis":
                    tool_name = insight.get("tool_name", "unknown")
                    if tool_name not in decided_tools:
                        if confidence >= 0.3:  # Lowered from 0.7 - frequent tools are valuable signals
                            decisions.append({
                                "type": "skill",
                                "insight": insight,
                                "reason": f"Frequent {tool_name} usage (confidence: {confidence:.2f}). "
                                           f"Should be automated as a skill.",
                                "priority": 2,
                            })
                            decided_tools.add(tool_name)
                            logger.info("evolution_decision", type="skill", tool=tool_name,
                                       confidence=confidence, reason="frequent_tool")

                elif source == "task_type_analysis":
                    task_type = insight.get("task_type", "unknown")
                    key = f"task_{task_type}"
                    if key not in decided_tasks and confidence >= 0.3:  # Lowered from 0.5
                        decisions.append({
                            "type": "skill",
                            "insight": insight,
                            "reason": f"Frequent {task_type} tasks (confidence: {confidence:.2f}). "
                                       f"Need specialized skill.",
                            "priority": 3,
                        })
                        decided_tasks.add(key)
                        logger.info("evolution_decision", type="skill", task=task_type,
                                   confidence=confidence, reason="task_type")

                elif source == "repeated_request":
                    # Repeated requests = strong signal for skill
                    if confidence >= 0.3:  # Lowered from 0.5
                        decisions.append({
                            "type": "skill",
                            "insight": insight,
                            "reason": f"Repeated user intent (confidence: {confidence:.2f}). "
                                       f"Should be handled by skill.",
                            "priority": 2,
                        })
                        logger.info("evolution_decision", type="skill",
                                   confidence=confidence, reason="repeated_request")

                elif source == "complexity_analysis":
                    # Complex tasks - suggest improvement but lower priority
                    if confidence >= 0.5:  # Kept at 0.5 - complexity is harder to judge
                        decisions.append({
                            "type": "gene",
                            "insight": insight,
                            "reason": f"Complex task handling (confidence: {confidence:.2f}). "
                                       f"Need improved behavior.",
                            "priority": 3,
                        })
                        logger.info("evolution_decision", type="gene",
                                   confidence=confidence, reason="complexity")

        # Sort by priority (lower number = higher priority)
        decisions.sort(key=lambda d: d.get("priority", 999))
        logger.info("evolution_decisions_made", count=len(decisions),
                   genes=sum(1 for d in decisions if d["type"] == "gene"),
                   skills=sum(1 for d in decisions if d["type"] == "skill"))

        return decisions

    async def _generate_gene(self, decision: dict) -> dict[str, Any] | None:
        """Generate a new Gene via LLM, validate with VFM, forge code, run validation."""
        prompt = self.builder.build_evolution_prompt([decision["insight"]])
        try:
            text = await self.llm.complete([{"role": "user", "content": prompt}])
            text = text.strip().strip("`").replace("json", "").strip()
            gene_data = json.loads(text)
            trigger_type = str(gene_data.get("trigger_type", "keyword")).lower()
            # Guard against garbage values from LLM
            if trigger_type not in GeneManager.TRIGGER_TYPES:
                trigger_type = "keyword"
            concept = {
                "name": str(gene_data.get("name", "Unnamed Gene")),
                "description": str(gene_data.get("description", "")),
                "trigger": str(gene_data.get("trigger", "")),
                "trigger_type": trigger_type,
                "action": str(gene_data.get("action", "")),
                "priority": int(gene_data.get("priority", 5)),
                "enabled": bool(gene_data.get("enabled", True)),
                "intents": gene_data.get("intents", []),
                "regex_pattern": str(gene_data.get("regex_pattern", "")),
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

            # Publish event for monitoring / WebSocket forwarding
            try:
                bus = get_event_bus()
                await bus.publish(Event(
                    event_type=EventType.GENE_GENERATED,
                    source=self.agent_id,
                    payload={"gene_id": gene["id"], "name": gene["name"], "score": scores["total"]},
                ))
            except Exception:
                pass

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

        # Publish event for monitoring / WebSocket forwarding
        try:
            bus = get_event_bus()
            await bus.publish(Event(
                event_type=EventType.SKILL_GENERATED,
                source=self.agent_id,
                payload={"skill_id": skill["id"], "name": skill["name"], "score": scores["total"]},
            ))
        except Exception:
            pass

        # Trigger tool registry reload so the new skill is immediately available
        await _reload_tool_registry(skill_name=skill.get("name", skill["id"]))

        logger.info("skill_generated_and_validated", skill_id=skill["id"], name=skill["name"], score=scores["total"])
        return skill
