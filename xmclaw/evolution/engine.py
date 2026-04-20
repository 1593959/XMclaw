"""Evolution engine: observe -> learn -> evolve -> solidify."""
import json
import shutil
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
from xmclaw.evolution.safety_policy import (
    check_gene_concept,
    check_skill_concept,
)
from xmclaw.evolution.coherence import (
    check_gene_coherence,
    check_skill_coherence,
)
from xmclaw.evolution.risk import assess_gene_risk, assess_skill_risk
from xmclaw.evolution.validator import EvolutionValidator
from xmclaw.evolution.journal import (
    CYCLE_PASSED,
    CYCLE_REJECTED,
    CYCLE_SKIPPED,
    EvolutionJournal,
    KIND_GENE,
    KIND_SKILL,
    STATUS_NEEDS_APPROVAL,
    STATUS_PROMOTED,
    STATUS_RETIRED,
    STATUS_SHADOW,
)
from xmclaw.daemon.config import DaemonConfig
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


def _retire_shadow_artifact(shadow_path: Path) -> None:
    """Remove a failing shadow artifact (and sibling .json, if any).

    Fail-closed: once validation rejects an artifact we MUST NOT leave the
    file where the loader can find it. Deleting keeps the filesystem clean;
    the journal retains the lineage row with status=retired for audit.
    """
    try:
        if shadow_path.exists():
            shadow_path.unlink()
        meta = shadow_path.with_suffix(".json")
        if meta.exists():
            meta.unlink()
    except Exception as e:
        logger.warning("retire_shadow_failed", path=str(shadow_path), error=str(e))


def _promote_shadow_artifact(shadow_path: Path, active_dir: Path) -> Path:
    """Move a passing shadow artifact into the active dir. Returns new path.

    The active dir is the one the tool/gene registry actually loads from,
    so this step is the canary-to-production handoff. Only call AFTER
    validation has passed."""
    active_dir.mkdir(parents=True, exist_ok=True)
    new_path = active_dir / shadow_path.name
    shutil.move(str(shadow_path), str(new_path))
    meta = shadow_path.with_suffix(".json")
    if meta.exists():
        new_meta = active_dir / meta.name
        shutil.move(str(meta), str(new_meta))
    return new_path


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

        # Journal: opens a cycle row per run_cycle() call and records every
        # forged/promoted/retired artifact. See xmclaw/evolution/journal.py.
        # The store is lazily created so headless tests can stub it out.
        self._journal: EvolutionJournal | None = None
        self._current_cycle_id: str | None = None

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

    def _get_journal(self) -> EvolutionJournal:
        """Lazily build the journal so tests that stub sqlite aren't forced
        to initialise one. Callers must await self.initialize() first."""
        if self._journal is None:
            store = SQLiteStore(self.db_path)
            self._journal = EvolutionJournal(store, agent_id=self.agent_id)
        return self._journal

    async def _find_live_skill_for_concept(self, concept_name: str) -> dict | None:
        """Return a DB row for a live (promoted/shadow) skill with this
        concept name, or None. A skill is 'live' if its lineage status is
        promoted OR shadow. Retired / rolled_back skills are ignored — they
        are NOT live and re-forging is allowed (the last attempt failed).
        """
        try:
            store = SQLiteStore(self.db_path)
        except Exception:
            return None
        row = store.get_skill_by_concept_name(self.agent_id, concept_name)
        if not row:
            return None
        skill_id = row.get("id")
        if not skill_id:
            return None
        try:
            lineage = store.lineage_for_artifact(skill_id)
        except Exception:
            lineage = None
        if not lineage:
            # No lineage row (pre-journal skill). Treat as live — we can't
            # tell whether it was rolled back, so be conservative.
            return row
        if lineage.get("status") in (STATUS_PROMOTED, STATUS_SHADOW):
            return row
        return None

    def _live_gene_snapshot(self) -> list[dict[str, Any]]:
        """Return every live gene for this agent, for coherence checks.

        Swallows DB errors — coherence is a hint, not a gate. A failure
        to read the snapshot just means coherence passes trivially; the
        safety policy + VFM downstream still run.
        """
        try:
            store = SQLiteStore(self.db_path)
            return store.get_genes(self.agent_id)
        except Exception as e:
            logger.warning("live_gene_snapshot_failed", error=str(e))
            return []

    def _live_skill_snapshot(self) -> list[dict[str, Any]]:
        """Return live skill metadata (name + description).

        The skills table only stores name/category/version/path — the
        concept description lives in the sidecar JSON written by
        SkillForge. We join the two here so coherence can compare
        descriptions without the callers doing any filesystem work.
        """
        try:
            store = SQLiteStore(self.db_path)
            rows = store.get_skills(self.agent_id)
        except Exception as e:
            logger.warning("live_skill_snapshot_failed", error=str(e))
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            meta_desc = ""
            path_str = row.get("path")
            if path_str:
                try:
                    meta_path = Path(path_str).with_suffix(".json")
                    if meta_path.exists():
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        meta_desc = meta.get("description", "") or ""
                except Exception:
                    meta_desc = ""
            result.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "description": meta_desc,
            })
        return result

    async def approve_artifact(
        self, artifact_id: str, approved: bool,
    ) -> dict[str, Any]:
        """Resolve a ``needs_approval`` artifact per the user's decision.

        Called from the daemon layer in response to a UI approve/decline
        click. Idempotent: if the artifact is already in a terminal state
        (promoted, retired, rolled_back) the call is a noop and returns
        the current status. If the artifact doesn't exist, returns
        ``{"status": "not_found"}`` — the caller decides whether that's
        an error.

        On approval: moves the shadow file into the active dir, flips
        lineage to PROMOTED, inserts a DB row so the registry picks it
        up on next reload, and emits EVOLUTION_ARTIFACT_PROMOTED +
        EVOLUTION_APPROVAL_DECIDED. For skills, it additionally hot-
        reloads the tool registry so the new tool is usable immediately.

        On decline: deletes the shadow file, flips lineage to RETIRED,
        emits EVOLUTION_ARTIFACT_RETIRED + EVOLUTION_APPROVAL_DECIDED.
        """
        journal = self._get_journal()
        row = await journal.get_artifact(artifact_id)
        if not row:
            return {"status": "not_found", "artifact_id": artifact_id}
        current = row.get("status")
        if current != STATUS_NEEDS_APPROVAL:
            # Already decided (or never held). Don't double-promote.
            return {
                "status": "noop",
                "artifact_id": artifact_id,
                "current_status": current,
            }

        kind = row.get("kind")
        if kind == KIND_SKILL:
            shadow_dir = self.skill_forge.shadow_dir
            active_dir = self.skill_forge.active_dir
            ext = ".py"
        elif kind == KIND_GENE:
            shadow_dir = self.gene_forge.shadow_dir
            active_dir = self.gene_forge.active_dir
            ext = ".py"
        else:
            return {"status": "unsupported_kind", "kind": kind}

        shadow_path = Path(shadow_dir) / f"{artifact_id}{ext}"

        if approved:
            try:
                active_path = _promote_shadow_artifact(shadow_path, active_dir)
            except Exception as e:
                logger.error("approve_promote_failed",
                            artifact_id=artifact_id, error=str(e))
                return {"status": "promote_failed", "error": str(e)}
            await journal.update_artifact_status(artifact_id, STATUS_PROMOTED)

            # Persist to DB so the registry loader sees the artifact.
            try:
                db = SQLiteStore(self.db_path)
                meta_path = active_path.with_suffix(".json")
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                else:
                    meta = {"id": artifact_id, "name": artifact_id}
                meta["path"] = str(active_path)
                meta["status"] = STATUS_PROMOTED
                if kind == KIND_SKILL:
                    db.insert_skill(self.agent_id, meta)
                else:
                    # Gene concept is stored alongside the shadow file; pull
                    # the full row back out so insert_gene has what it needs.
                    db.insert_gene(self.agent_id, meta)
            except Exception as e:
                logger.warning("approve_db_insert_failed",
                              artifact_id=artifact_id, error=str(e))

            await self._emit(EventType.EVOLUTION_ARTIFACT_PROMOTED, {
                "artifact_id": artifact_id, "kind": kind,
                "via": "approval",
            })
            await self._emit(EventType.EVOLUTION_APPROVAL_DECIDED, {
                "artifact_id": artifact_id, "kind": kind, "approved": True,
            })
            if kind == KIND_SKILL:
                await _reload_tool_registry(skill_name=artifact_id)
            return {"status": "promoted", "artifact_id": artifact_id}

        # Declined: delete shadow, mark retired.
        _retire_shadow_artifact(shadow_path)
        await journal.update_artifact_status(artifact_id, STATUS_RETIRED)
        await self._emit(EventType.EVOLUTION_ARTIFACT_RETIRED, {
            "artifact_id": artifact_id, "kind": kind,
            "reason": "approval_declined",
        })
        await self._emit(EventType.EVOLUTION_APPROVAL_DECIDED, {
            "artifact_id": artifact_id, "kind": kind, "approved": False,
        })
        return {"status": "retired", "artifact_id": artifact_id}

    async def _emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish a journal state-machine event. Swallows errors — telemetry
        must never break an evolution cycle."""
        try:
            await get_event_bus().publish(Event(
                event_type=event_type,
                source=self.agent_id,
                payload=payload,
            ))
        except Exception as e:
            logger.warning("evolution_emit_failed", type=event_type.value, error=str(e))

    async def run_cycle(self, trigger: str = "manual") -> dict[str, Any]:
        """Run one full evolution cycle.

        Emits 10 journal state-machine events (see WS_EVENT_MAP) so the
        Evolution Live panel can show the cycle progress end-to-end, and
        persists every decision/artifact into evolution_journal for audit.
        """
        logger.info("evolution_cycle_start", agent_id=self.agent_id, trigger=trigger)

        # Open journal cycle. If the store isn't available (rare — only in
        # degenerate tests), fall back to a uuid so the rest of the cycle
        # still runs; the events are still useful without persistence.
        try:
            journal = self._get_journal()
            cycle_id = await journal.open_cycle(trigger=trigger)
        except Exception as e:
            logger.warning("journal_open_failed", error=str(e))
            journal = None
            cycle_id = f"cycle_{uuid.uuid4().hex[:8]}"
        self._current_cycle_id = cycle_id

        await self._emit(EventType.EVOLUTION_CYCLE_STARTED,
                         {"cycle_id": cycle_id, "trigger": trigger})
        # Legacy event kept for backwards compat with dashboards still
        # subscribing to EVOLUTION_CYCLE.
        await self._emit(EventType.EVOLUTION_CYCLE,
                         {"phase": "start", "cycle_id": cycle_id})

        # 1. Reflect: get recent sessions and insights
        await self._emit(EventType.EVOLUTION_REFLECTING, {"cycle_id": cycle_id})
        sessions = await self._get_recent_sessions()
        insights = self._extract_insights(sessions)

        if not insights:
            logger.info("evolution_no_insights", agent_id=self.agent_id)
            if journal:
                await journal.record_inputs(cycle_id, {"sessions": len(sessions), "insights": 0})
                await journal.close_cycle(cycle_id, verdict=CYCLE_SKIPPED,
                                          reject_reason="no_insights")
            await self._emit(EventType.EVOLUTION_CYCLE_ENDED,
                             {"cycle_id": cycle_id, "verdict": "skipped",
                              "reason": "no_insights"})
            return {"status": "no_insights", "insights": 0, "cycle_id": cycle_id}

        # 2. Learn: store insights
        for insight in insights:
            self.memory.save_insight(self.agent_id, insight)

        # 3. Evolve: decide whether to create Gene or Skill
        decisions = await self._decide_evolution(insights)

        # Limit generation per cycle to prevent overwhelming the system
        max_per_type = self.config.evolution.get("max_genes_per_day", 10)
        max_per_cycle = min(3, max_per_type)
        gene_decisions = [d for d in decisions if d["type"] == "gene"][:max_per_cycle]
        skill_decisions = [d for d in decisions if d["type"] == "skill"][:max_per_cycle]
        limited_decisions = gene_decisions + skill_decisions

        if journal:
            await journal.record_inputs(cycle_id, {
                "sessions": len(sessions),
                "insights": len(insights),
            })
            await journal.record_decisions(cycle_id, {
                "gene_decisions": [d["insight"].get("title", "") for d in gene_decisions],
                "skill_decisions": [d["insight"].get("title", "") for d in skill_decisions],
                "decisions_total": len(decisions),
            })

        results = {"status": "running", "insights": len(insights),
                   "decisions": len(limited_decisions), "actions": [], "cycle_id": cycle_id}

        logger.info("evolution_generation_start",
                   total_decisions=len(decisions),
                   limited_decisions=len(limited_decisions),
                   genes=len(gene_decisions),
                   skills=len(skill_decisions))

        # Forging + validating happens inside _generate_* — those helpers now
        # emit EVOLUTION_FORGING/_VALIDATING/_ARTIFACT_* for each decision.
        import asyncio
        coros = []
        for decision in limited_decisions:
            if decision["type"] == "gene":
                coros.append(self._generate_gene(decision))
            elif decision["type"] == "skill":
                coros.append(self._generate_skill(decision))

        generated = await asyncio.gather(*coros, return_exceptions=True)
        any_promoted = False
        any_rejected = False
        for item in generated:
            if isinstance(item, Exception):
                logger.error("evolution_generation_error", error=str(item))
                any_rejected = True
                continue
            if item:
                results["actions"].append({"type": item.get("type", "unknown"),
                                           "id": item["id"]})
                any_promoted = True
            else:
                # None from _generate_* means either VFM rejected or validation
                # retired the shadow artifact — both count as a rejection.
                any_rejected = True

        # Verdict rules: at least one promoted → passed; none promoted and at
        # least one rejected → rejected; otherwise skipped.
        if any_promoted:
            verdict = CYCLE_PASSED
            reject_reason = None
        elif any_rejected:
            verdict = CYCLE_REJECTED
            reject_reason = "all_candidates_failed"
        else:
            verdict = CYCLE_SKIPPED
            reject_reason = "no_candidates"

        # 4. Record and notify
        await self._record_results(results)
        if results["actions"]:
            await self._notify_user(results)

        if journal:
            await journal.close_cycle(cycle_id, verdict=verdict,
                                      reject_reason=reject_reason,
                                      metrics={"actions": len(results["actions"])})
        if verdict == CYCLE_REJECTED:
            await self._emit(EventType.EVOLUTION_REJECTED,
                             {"cycle_id": cycle_id, "reason": reject_reason})
        await self._emit(EventType.EVOLUTION_CYCLE_ENDED,
                         {"cycle_id": cycle_id, "verdict": verdict,
                          "actions": len(results["actions"])})
        # Legacy end event
        await self._emit(EventType.EVOLUTION_CYCLE,
                         {"phase": "end", "cycle_id": cycle_id, "results": results})

        results["verdict"] = verdict
        logger.info("evolution_cycle_end", agent_id=self.agent_id,
                    cycle_id=cycle_id, verdict=verdict, results=results)
        self._current_cycle_id = None
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

            # Phase E5 safety policy: reject malformed genes (empty trigger,
            # uncompilable regex, intent trigger with no intents, name
            # collision) before the forge writes anything to disk.
            ok, reject_reason = check_gene_concept(concept)
            if not ok:
                logger.info("gene_policy_rejected", concept=concept["name"], reason=reject_reason)
                if self._current_cycle_id:
                    try:
                        await self._emit(EventType.EVOLUTION_REJECTED, {
                            "cycle_id": self._current_cycle_id,
                            "reason": f"policy:{reject_reason}",
                            "concept": concept["name"],
                            "kind": "gene",
                        })
                    except Exception:
                        pass
                return None

            # Phase E6 coherence: reject genes whose trigger collides with
            # an existing live gene. Two genes with the same trigger silently
            # compete — only one wins per turn, which makes the other a
            # dead artifact the rollback path won't catch cheaply.
            live_genes = self._live_gene_snapshot()
            ok_c, reason_c = check_gene_coherence(concept, live_genes)
            if not ok_c:
                logger.info("gene_coherence_rejected", concept=concept["name"], reason=reason_c)
                if self._current_cycle_id:
                    try:
                        await self._emit(EventType.EVOLUTION_REJECTED, {
                            "cycle_id": self._current_cycle_id,
                            "reason": f"coherence:{reason_c}",
                            "concept": concept["name"],
                            "kind": "gene",
                        })
                    except Exception:
                        pass
                return None

            # VFM scoring
            scores = self.vfm.score_gene(concept)
            threshold = self.config.evolution.get("vfm_threshold", 20.0)
            if not self.vfm.should_solidify(scores, threshold):
                logger.info("gene_rejected_by_vfm", concept=concept["name"], score=scores["total"])
                return None

            # Forge executable code into SHADOW quarantine
            cycle_id = self._current_cycle_id
            await self._emit(EventType.EVOLUTION_FORGING,
                             {"cycle_id": cycle_id, "kind": "gene",
                              "name": concept["name"]})
            gene = await self.gene_forge.forge(concept, action_body=action_body)
            if not gene:
                return None

            shadow_path = Path(gene["path"])
            journal = self._journal
            if journal and cycle_id:
                await journal.record_artifact(cycle_id, KIND_GENE, gene["id"],
                                              status=STATUS_SHADOW)
            await self._emit(EventType.EVOLUTION_ARTIFACT_SHADOW,
                             {"cycle_id": cycle_id, "artifact_id": gene["id"],
                              "kind": "gene"})

            # Validate the shadow artifact. Fail-closed: on any validation
            # failure the shadow file is deleted so the loader never sees it
            # (fixes bug M22 — broken skills used to stay on disk).
            await self._emit(EventType.EVOLUTION_VALIDATING,
                             {"cycle_id": cycle_id, "artifact_id": gene["id"]})
            validation = await self.validator.validate_gene(shadow_path)
            if not validation["passed"]:
                logger.warning("gene_validation_failed", gene_id=gene["id"], errors=validation)
                _retire_shadow_artifact(shadow_path)
                if journal:
                    await journal.update_artifact_status(gene["id"], STATUS_RETIRED)
                await self._emit(EventType.EVOLUTION_ARTIFACT_RETIRED,
                                 {"cycle_id": cycle_id, "artifact_id": gene["id"],
                                  "reason": "validation_failed"})
                return None

            # Phase E7 risk gate: a validated gene with a greedy regex or
            # high priority or sensitive-domain keywords is parked in shadow
            # until a human approves. The file stays on disk at its shadow
            # path; promote is skipped; EVOLUTION_APPROVAL_REQUESTED fires
            # so the UI can render a prompt.
            risk_level, risk_reasons = assess_gene_risk(concept)
            if risk_level == "high":
                if journal:
                    await journal.update_artifact_status(gene["id"], STATUS_NEEDS_APPROVAL)
                await self._emit(EventType.EVOLUTION_APPROVAL_REQUESTED, {
                    "cycle_id": cycle_id,
                    "artifact_id": gene["id"],
                    "kind": "gene",
                    "risk_level": risk_level,
                    "reasons": risk_reasons,
                    "shadow_path": str(shadow_path),
                    "name": concept["name"],
                })
                gene["status"] = STATUS_NEEDS_APPROVAL
                gene["type"] = "gene"
                gene["risk_reasons"] = risk_reasons
                logger.info("gene_held_for_approval",
                           gene_id=gene["id"], reasons=risk_reasons)
                return gene

            # Promote: move shadow → active dir so the registry can pick it up
            active_path = _promote_shadow_artifact(shadow_path, self.gene_forge.active_dir)
            gene["path"] = str(active_path)
            gene["status"] = "promoted"
            gene["type"] = "gene"
            if journal:
                await journal.update_artifact_status(gene["id"], STATUS_PROMOTED)
            await self._emit(EventType.EVOLUTION_ARTIFACT_PROMOTED,
                             {"cycle_id": cycle_id, "artifact_id": gene["id"],
                              "kind": "gene"})
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

        # Phase E5 safety policy: block name collisions with built-ins and
        # other structural problems BEFORE we write anything to disk. Cheaper
        # than validating after forge and keeps shadow/ clean.
        ok, reject_reason = check_skill_concept(concept)
        if not ok:
            logger.info("skill_policy_rejected", concept=concept["name"], reason=reject_reason)
            if self._current_cycle_id:
                try:
                    await self._emit(EventType.EVOLUTION_REJECTED, {
                        "cycle_id": self._current_cycle_id,
                        "reason": f"policy:{reject_reason}",
                        "concept": concept["name"],
                        "kind": "skill",
                    })
                except Exception:
                    pass
            return None

        # Phase E4 dedup guard: if a previous cycle has already produced an
        # active skill for this exact concept name, don't forge a duplicate.
        # Reflection (which reads artifact health) is the channel that should
        # propose revising an existing skill — duplicate forging just pollutes
        # the shadow dir and adds rollback noise.
        existing = await self._find_live_skill_for_concept(concept["name"])
        if existing:
            logger.info(
                "skill_dedup_guard_hit",
                concept=concept["name"],
                existing_id=existing.get("id"),
            )
            if self._journal and self._current_cycle_id:
                try:
                    await self._emit(EventType.EVOLUTION_REJECTED, {
                        "cycle_id": self._current_cycle_id,
                        "reason": "duplicate_concept",
                        "concept": concept["name"],
                        "existing_artifact_id": existing.get("id"),
                    })
                except Exception:
                    pass
            return None

        # Phase E6 coherence: catches near-duplicate descriptions where the
        # forge picked a slightly different auto-name from a reworded insight
        # (e.g. "Frequent web_search usage" vs "Frequent web search usage"
        # producing different concept names but near-identical skills).
        live_skills = self._live_skill_snapshot()
        ok_c, reason_c = check_skill_coherence(concept, live_skills)
        if not ok_c:
            logger.info("skill_coherence_rejected", concept=concept["name"], reason=reason_c)
            if self._current_cycle_id:
                try:
                    await self._emit(EventType.EVOLUTION_REJECTED, {
                        "cycle_id": self._current_cycle_id,
                        "reason": f"coherence:{reason_c}",
                        "concept": concept["name"],
                        "kind": "skill",
                    })
                except Exception:
                    pass
            return None

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

        # Forge executable code into SHADOW quarantine
        cycle_id = self._current_cycle_id
        await self._emit(EventType.EVOLUTION_FORGING,
                         {"cycle_id": cycle_id, "kind": "skill",
                          "name": concept["name"]})
        skill = await self.skill_forge.forge(concept, action_body=action_body)
        if not skill:
            return None

        shadow_path = Path(skill["path"])
        journal = self._journal
        if journal and cycle_id:
            await journal.record_artifact(cycle_id, KIND_SKILL, skill["id"],
                                          status=STATUS_SHADOW)
        await self._emit(EventType.EVOLUTION_ARTIFACT_SHADOW,
                         {"cycle_id": cycle_id, "artifact_id": skill["id"],
                          "kind": "skill"})

        # Validate the shadow artifact. Fail-closed: on any validation failure
        # the shadow file is deleted so the registry's next reload cannot
        # pick up a broken skill (fixes bug M22).
        await self._emit(EventType.EVOLUTION_VALIDATING,
                         {"cycle_id": cycle_id, "artifact_id": skill["id"]})
        validation = await self.validator.validate_skill(shadow_path)
        if not validation["passed"]:
            logger.warning("skill_validation_failed", skill_id=skill["id"], errors=validation)
            _retire_shadow_artifact(shadow_path)
            if journal:
                await journal.update_artifact_status(skill["id"], STATUS_RETIRED)
            await self._emit(EventType.EVOLUTION_ARTIFACT_RETIRED,
                             {"cycle_id": cycle_id, "artifact_id": skill["id"],
                              "reason": "validation_failed"})
            return None

        # Phase E7 risk gate: skills whose action_body uses subprocess /
        # eval / os.remove / network writes, or whose name-and-description
        # touch sensitive domain keywords, don't auto-promote. The file
        # stays in shadow; EVOLUTION_APPROVAL_REQUESTED prompts the user.
        risk_level, risk_reasons = assess_skill_risk(concept, action_body)
        if risk_level == "high":
            if journal:
                await journal.update_artifact_status(skill["id"], STATUS_NEEDS_APPROVAL)
            await self._emit(EventType.EVOLUTION_APPROVAL_REQUESTED, {
                "cycle_id": cycle_id,
                "artifact_id": skill["id"],
                "kind": "skill",
                "risk_level": risk_level,
                "reasons": risk_reasons,
                "shadow_path": str(shadow_path),
                "name": concept["name"],
            })
            skill["status"] = STATUS_NEEDS_APPROVAL
            skill["type"] = "skill"
            skill["risk_reasons"] = risk_reasons
            logger.info("skill_held_for_approval",
                       skill_id=skill["id"], reasons=risk_reasons)
            return skill

        # Promote: move shadow → active dir so the registry can load it
        active_path = _promote_shadow_artifact(shadow_path, self.skill_forge.active_dir)
        skill["path"] = str(active_path)
        skill["status"] = "promoted"
        skill["type"] = "skill"
        if journal:
            await journal.update_artifact_status(skill["id"], STATUS_PROMOTED)
        await self._emit(EventType.EVOLUTION_ARTIFACT_PROMOTED,
                         {"cycle_id": cycle_id, "artifact_id": skill["id"],
                          "kind": "skill"})
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
