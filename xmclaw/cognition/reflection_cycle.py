"""ReflectionCycle — R1 真持续认知 Loop（2026-05-10）.

Three cadence buckets that turn ``CognitiveDaemon`` from "react to
percepts" into "actually has an inner life":

* **5-min reflect_recent**:
    Look back at the last N turns + recent percepts; ask the LLM to
    spot patterns ("user mentioned X 3 times", "tool foo failed
    twice in different sessions"). Each pattern emits an
    ``INNER_MONOLOGUE`` thought + optionally surfaces as a
    ``REFLECTION_CYCLE_RAN`` summary.

* **1-h consolidate_memory**:
    Walk the working layer of MemoryService; promote durable entries
    to long_term (re-remember to bump evidence_count past the
    promote threshold), merge near-duplicates via the built-in
    deduplicate() pass, archive stale (>1h working-layer) entries by
    moving them to long_term. Emits ``MEMORY_CONSOLIDATED``.

    Phase 7 V1→V2 (2026-05-23): switched from V1 ``UnifiedMemorySystem``
    duck-typed ``promote_durable_short_to_long`` /
    ``merge_near_duplicates`` / ``archive_stale_short`` hooks to V2
    native ``recall(only_layer="working", time_range=...)`` +
    ``remember(layer="long_term")`` + ``deduplicate()``. The
    ``short_term`` layer is gone — its semantic (recent + untrusted)
    is now expressed as "working + within last 1h" via time_range.

* **1-d groom_goals**:
    Walk CognitiveState.current_goals; archive completed ones, drop
    goals not advanced in 7d (``stale``), trigger replan for goals
    blocked >24h. Emits ``GOALS_GROOMED``.

Design notes
============

* **No new dependencies**: every collaborator (LLM, UnifiedMemory,
  CognitiveState) is duck-typed; the Cycle never imports anything
  outside ``xmclaw.cognition`` — agnostic of providers.
* **Best-effort everywhere**: any collaborator failure is caught
  and logged; the cycle ALWAYS returns a structured summary.
* **Stateless across runs**: each invocation re-reads the world.
  The cycle doesn't accumulate cross-tick state — that's the
  CognitiveState's job.

The ``ReflectionCycle`` class is single-instance per daemon. The
``CognitiveDaemon`` calls ``run_due(tick)`` once per heartbeat; the
cycle internally decides which buckets are due based on configurable
period_ticks.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


# ── Defaults (assumes 1Hz heartbeat) ─────────────────────────────


_DEFAULT_REFLECT_EVERY_TICKS = 300       # 5 min @ 1Hz
_DEFAULT_CONSOLIDATE_EVERY_TICKS = 3600  # 1 h
_DEFAULT_GROOM_EVERY_TICKS = 86400       # 1 day
# 2026-05-10 default flip: metacognize was originally tied to the
# 1-day groom cadence (cheap, conservative). User asked to "drop the
# privacy-by-default conservatism" so agent visibly does something on
# day one. New default = 60 ticks (~1 min @ 1 Hz) so the operator
# sees R3 metacognition propose things shortly after boot. Ramp back
# up to 86400 in cfg when feedback loop stabilises.
# 2026-05-19 (Epic #27 sweep #11): bumped 60 → 1800 (~30 min). The
# 1-minute cadence was burning ~60 LLM-calls/hour in idle, silently
# racking up token cost — the operator gets the same value at 30-min
# cadence (metacognition findings rarely turn over faster than that),
# and the cost per day drops from ~1440 LLM calls to ~48. Wire
# ``cognition.metacognize.interval_ticks`` in config to override per
# install — power users who want the 1-min feedback can still get it.
_DEFAULT_METACOGNIZE_EVERY_TICKS = 1800

# Cap how far back each cycle reaches by default — prevent unbounded
# work as the journal / memory grows.
_DEFAULT_REFLECT_LOOKBACK_TURNS = 20
_DEFAULT_CONSOLIDATE_BATCH = 50
_DEFAULT_GROOM_STALE_DAYS = 7
_DEFAULT_GROOM_BLOCKED_HOURS = 24


CycleScope = Literal["recent", "consolidate", "groom", "metacognize"]
ThoughtKind = Literal[
    "reflection", "wonder", "concern", "plan", "observation",
]


# ── Result types ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Outcome of one cycle tick. ``ran`` is False when the cycle
    skipped (not due yet, no work, missing collaborator)."""
    scope: CycleScope
    ran: bool
    summary: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class InnerThought:
    """One unit of agent self-talk emitted from a reflection."""
    kind: ThoughtKind
    text: str
    trigger: str  # one-line description of what prompted the thought


# ── Reflection prompt ────────────────────────────────────────────


_REFLECT_PROMPT = """\
你是 agent 自己。下面是你最近经历的事件 (最新在最后)。
你的任务：作为内省，用一段话 (50-200 字) 写下你的所思所想。
然后把这段话标记一个 kind:

- reflection: 反思 (我做对了什么? 错了什么?)
- wonder: 好奇 (有件事不太懂、想多了解)
- concern: 担忧 (用户可能受挫、某个 pattern 重复出现)
- plan: 打算 (我接下来该做什么)
- observation: 观察 (中性记录，没结论)

输出严格 JSON 列表，1-3 条 (没什么可说就输出空列表 []):

[
  {{"kind": "reflection", "text": "...", "trigger": "短描述触发的事"}},
  ...
]

最近 {n} 件事：
{recent_block}

注意:
- 只对**值得思考**的事说话; 一堆无聊事就返回 []。
- text 用第一人称 (我...)，自然语气。
- 不要重复刚发生的事，要给出**额外的洞察**。
- JSON 之外不要任何字符。
"""


# ── ReflectionCycle ──────────────────────────────────────────────


class ReflectionCycle:
    """Three-bucket periodic reflection driver.

    Args:
        llm: any object exposing ``async complete(messages, tools=None)``
            with ``LLMResponse.content``. Used by reflect_recent and
            consolidate_memory (Phase 4 LLM synthesis); groom_goals
            remains LLM-free.
        memory_service: optional ``MemoryService`` (V2) for the 1h
            consolidate cycle. None disables consolidation. Phase 7
            replaced the legacy ``unified_memory`` parameter; callers
            still passing the old keyword get a TypeError — update to
            pass a ``MemoryService`` instance.
        memory_gateway: optional ``CognitiveMemoryGateway`` (Phase 4).
            When wired, consolidate_memory routes synthesized facts
            through the Gateway instead of direct remember() calls.
        cognitive_state: optional ``CognitiveState`` for goal grooming.
            None disables groom.
        bus: event bus to publish INNER_MONOLOGUE / REFLECTION_CYCLE_RAN
            / MEMORY_CONSOLIDATED / GOALS_GROOMED events. None means
            cycles run silently (test convenience).
        recent_events_fn: callable that returns the last N events for
            reflect_recent. Defaults to a no-op returning []. Production
            wiring uses ``SqliteEventBus.query`` against events.db.
        agent_id: identifier stamped onto all emitted events (default
            "cognition").
        config: per-cycle period & lookback knobs.
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        memory_service: Any | None = None,
        memory_gateway: Any | None = None,
        cognitive_state: Any | None = None,
        bus: Any | None = None,
        recent_events_fn: Callable[[int], Awaitable[list[Any]]] | None = None,
        agent_id: str = "cognition",
        reflect_every_ticks: int = _DEFAULT_REFLECT_EVERY_TICKS,
        consolidate_every_ticks: int = _DEFAULT_CONSOLIDATE_EVERY_TICKS,
        groom_every_ticks: int = _DEFAULT_GROOM_EVERY_TICKS,
        reflect_lookback_turns: int = _DEFAULT_REFLECT_LOOKBACK_TURNS,
        consolidate_batch: int = _DEFAULT_CONSOLIDATE_BATCH,
        groom_stale_days: int = _DEFAULT_GROOM_STALE_DAYS,
        groom_blocked_hours: int = _DEFAULT_GROOM_BLOCKED_HOURS,
        # R3 (2026-05-10) — metacognize bucket. ``None`` disables it.
        # Pass MetaCognitionPass + Reformer to enable; period defaults
        # to the same as groom (1d) but is independently configurable.
        metacognition_pass: Any | None = None,
        reformer: Any | None = None,
        metacognize_every_ticks: int | None = None,
        metacognize_lookback: int = 100,
    ) -> None:
        self._llm = llm
        self._memory_service = memory_service
        self._memory_gateway = memory_gateway
        self._cognitive_state = cognitive_state
        self._bus = bus
        self._recent_events_fn = recent_events_fn
        self._agent_id = agent_id
        self._metacognition_pass = metacognition_pass
        self._reformer = reformer
        self._metacognize_every = max(
            1,
            int(metacognize_every_ticks)
            if metacognize_every_ticks is not None
            else _DEFAULT_METACOGNIZE_EVERY_TICKS,
        )
        self._metacognize_lookback = max(1, int(metacognize_lookback))
        self._reflect_every = max(1, int(reflect_every_ticks))
        self._consolidate_every = max(1, int(consolidate_every_ticks))
        self._groom_every = max(1, int(groom_every_ticks))
        self._reflect_lookback = max(1, int(reflect_lookback_turns))
        self._consolidate_batch = max(1, int(consolidate_batch))
        self._groom_stale_seconds = float(groom_stale_days) * 86400.0
        self._groom_blocked_seconds = float(groom_blocked_hours) * 3600.0
        # Track last-run tick per scope so an out-of-band run() bumps
        # the schedule.
        self._last_ran: dict[CycleScope, int] = {
            "recent": -1,
            "consolidate": -1,
            "groom": -1,
            "metacognize": -1,
        }

    # ── Public dispatch ──────────────────────────────────────────

    async def run_due(self, tick: int) -> list[CycleResult]:
        """Run whichever cycles are due at ``tick``. Called by the
        CognitiveDaemon once per heartbeat. Returns one result per
        cycle that actually ran (skipped cycles are NOT included)."""
        out: list[CycleResult] = []
        if self._is_due(tick, "recent", self._reflect_every):
            r = await self.reflect_recent(tick=tick)
            self._last_ran["recent"] = tick
            if r.ran:
                out.append(r)
        if self._is_due(tick, "consolidate", self._consolidate_every):
            r = await self.consolidate_memory(tick=tick)
            self._last_ran["consolidate"] = tick
            if r.ran:
                out.append(r)
        if self._is_due(tick, "groom", self._groom_every):
            r = await self.groom_goals(tick=tick)
            self._last_ran["groom"] = tick
            if r.ran:
                out.append(r)
        if self._is_due(tick, "metacognize", self._metacognize_every):
            r = await self.metacognize(tick=tick)
            self._last_ran["metacognize"] = tick
            if r.ran:
                out.append(r)
        return out

    def _is_due(self, tick: int, scope: CycleScope, every: int) -> bool:
        last = self._last_ran[scope]
        # First time always due (so cycles fire on cold-start daemon
        # without waiting a full period).
        if last < 0:
            return True
        return (tick - last) >= every

    # ── Scope 1: reflect_recent (5-min) ──────────────────────────

    async def reflect_recent(self, *, tick: int) -> CycleResult:
        """Look at the last N events; ask the LLM for inner thoughts.

        Each generated thought is published as ``INNER_MONOLOGUE``;
        a single ``REFLECTION_CYCLE_RAN`` summary closes the pass.
        """
        t0 = time.perf_counter()
        if self._llm is None or self._recent_events_fn is None:
            return CycleResult(scope="recent", ran=False)

        try:
            events = await self._recent_events_fn(self._reflect_lookback)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflection.recent_events_fn_failed err=%s", exc)
            return CycleResult(
                scope="recent", ran=False,
                error=f"recent_events_fn: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        if not events:
            return CycleResult(scope="recent", ran=False)

        recent_block = self._format_events(events)
        prompt = _REFLECT_PROMPT.format(
            n=len(events), recent_block=recent_block,
        )
        thoughts = await self._extract_thoughts(prompt)
        for t in thoughts:
            await self._publish_thought(t, tick=tick)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = {
            "scope": "recent",
            "lookback_n": len(events),
            "patterns_found": [t.text[:120] for t in thoughts],
            "actions_taken": [],  # action surfacing is R3 metacognition's job
            "elapsed_ms": round(elapsed_ms, 2),
        }
        await self._publish_event("reflection_cycle_ran", summary)
        return CycleResult(
            scope="recent", ran=True, summary=summary,
            elapsed_ms=elapsed_ms,
        )

    # ── Scope 2: consolidate_memory (1-h) ────────────────────────

    async def consolidate_memory(self, *, tick: int) -> CycleResult:
        """Cognitive memory consolidation (Phase 4).

        Replaces the mechanical "promote/archive" with an LLM-driven
        synthesis pipeline:

          1. ``deduplicate``  — V2 built-in near-dup merge (kept).
          2. ``synthesize``   — NEW. Cluster working facts by bucket,
             ask the LLM to condense each cluster into 1-3 coherent
             statements, write the synthesized facts to long_term,
             and supersede the fragment facts.
          3. ``promote``      — remaining high-confidence working facts
             that were NOT covered by synthesis → long_term.
          4. ``stale_detect`` — NEW. Scan long_term for facts that may
             have been contradicted by newer working facts; mark
             invalid so they stop surfacing in recall.

        Any per-step failure is logged + suppressed — consolidate is
        best-effort and never fails the cycle.
        """
        t0 = time.perf_counter()
        svc = self._memory_service
        gateway = getattr(self, "_memory_gateway", None)
        if svc is None:
            return CycleResult(scope="consolidate", ran=False)

        merged = 0
        synthesized = 0
        superseded = 0
        promoted = 0
        stale_marked = 0
        now = time.time()
        recent_cutoff = now - 3600.0   # 1 hour

        # ── Step 1: deduplicate ──────────────────────────────────
        try:
            merged_result = await svc.deduplicate()
            if isinstance(merged_result, dict):
                merged = int(merged_result.get("merged", 0))
            else:
                merged = int(merged_result or 0)
        except AttributeError:
            logger.warning("consolidate.deduplicate_missing")
        except Exception as exc:  # noqa: BLE001
            logger.warning("consolidate.deduplicate_failed err=%s", exc)

        # ── Step 2: LLM synthesis of fragment clusters ────────────
        if self._llm is not None:
            try:
                syn_count, sup_count = await self._synthesize_clusters(svc, gateway)
                synthesized = syn_count
                superseded = sup_count
            except Exception as exc:  # noqa: BLE001
                logger.warning("consolidate.synthesize_failed err=%s", exc)
        else:
            logger.debug("consolidate.synthesize_skipped (no llm)")

        # ── Step 3: promote remaining recent high-confidence ──────
        try:
            recent_working = await svc.recall(
                only_layer="working",
                time_range=(recent_cutoff, None),
                k=self._consolidate_batch,
                keyword_only=True,
                min_confidence=0.7,
                include_relations=False,
            )
            for hit in recent_working:
                try:
                    if getattr(hit.fact, "superseded_by", None):
                        continue
                    if gateway is not None:
                        from xmclaw.memory.v2.gateway_models import Observation
                        await gateway.ingest(
                            Observation(
                                source="cognition",
                                content=hit.fact.text,
                                turn_id=f"consolidate:{tick}",
                                timestamp=now,
                                metadata={
                                    "kind_hint": hit.fact.kind,
                                    "scope_hint": hit.fact.scope,
                                    "bucket_hint": getattr(hit.fact, "bucket", ""),
                                    "confidence_hint": hit.fact.confidence,
                                },
                            ),
                            context={"consolidate": True},
                        )
                    else:
                        await svc.remember(
                            text=hit.fact.text,
                            kind=hit.fact.kind,
                            scope=hit.fact.scope,
                            layer="long_term",
                        )
                    promoted += 1
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("consolidate.promote_failed err=%s", exc)

        # ── Step 4: stale detection on long_term ──────────────────
        if self._llm is not None:
            try:
                stale_marked = await self._detect_stale_long_term(svc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("consolidate.stale_detect_failed err=%s", exc)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = {
            "merged": merged,
            "synthesized": synthesized,
            "superseded": superseded,
            "promoted": promoted,
            "stale_marked": stale_marked,
            "elapsed_ms": round(elapsed_ms, 2),
        }
        await self._publish_event("memory_consolidated", summary)
        return CycleResult(
            scope="consolidate", ran=True,
            summary=summary, elapsed_ms=elapsed_ms,
        )

    # ── Phase 4 helpers ────────────────────────────────────────────

    async def _synthesize_clusters(
        self,
        svc: Any,
        gateway: Any | None,
    ) -> tuple[int, int]:
        """Cluster working facts by bucket, ask LLM to synthesize each
        cluster, write synthesized facts to long_term, supersede the
        old fragments.

        Returns (synthesized_count, superseded_count).
        """
        try:
            all_working = await svc.recall(
                only_layer="working",
                k=200,
                keyword_only=True,
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("consolidate.fetch_working_failed err=%s", exc)
            return 0, 0

        if len(all_working) < 3:
            return 0, 0

        from collections import defaultdict
        by_bucket: dict[str, list[Any]] = defaultdict(list)
        for h in all_working:
            bucket = getattr(h.fact, "bucket", "") or "misc"
            by_bucket[bucket].append(h.fact)

        synthesized_count = 0
        superseded_count = 0

        for bucket, facts in by_bucket.items():
            if len(facts) < 3:
                continue
            try:
                syn_texts = await self._llm_synthesize_bucket(bucket, facts)
                for syn_text in syn_texts:
                    representative = facts[0]
                    if gateway is not None:
                        from xmclaw.memory.v2.gateway_models import Observation
                        new_fact = await gateway.ingest(
                            Observation(
                                source="cognition",
                                content=syn_text,
                                turn_id=f"synthesize:{int(time.time())}",
                                timestamp=time.time(),
                                metadata={
                                    "kind_hint": representative.kind,
                                    "scope_hint": representative.scope,
                                    "bucket_hint": bucket,
                                    "confidence_hint": 0.85,
                                },
                            ),
                            context={"synthesize": True},
                        )
                    else:
                        new_fact = await svc.remember(
                            syn_text,
                            kind=representative.kind,
                            scope=representative.scope,
                            confidence=0.85,
                            layer="long_term",
                            bucket=bucket,
                        )
                    if new_fact is not None:
                        synthesized_count += 1
                        for old in facts:
                            if old.id == new_fact.id:
                                continue
                            try:
                                if hasattr(svc, "supersede"):
                                    await svc.supersede(
                                        old_fact_id=old.id,
                                        new_fact_id=new_fact.id,
                                    )
                                    superseded_count += 1
                            except Exception:  # noqa: BLE001
                                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "consolidate.synthesize_bucket_failed bucket=%s err=%s",
                    bucket, exc,
                )

        return synthesized_count, superseded_count

    async def _llm_synthesize_bucket(
        self,
        bucket: str,
        facts: list[Any],
    ) -> list[str]:
        """Ask the LLM to condense a cluster of fragment facts into
        1-3 coherent statements.
        """
        subset = facts[:15]
        numbered = "\n".join(
            f"{i+1}. {f.text}" for i, f in enumerate(subset)
        )
        prompt = (
            f"你是记忆整理助手。下面是一组关于「{bucket}」的记忆碎片，"
            f"共 {len(subset)} 条。请将它们合并、归纳为 1-3 条完整、"
            f"规范的陈述句。\n\n"
            f"要求：\n"
            f"1. 合并重复或近义的内容\n"
            f"2. 保留最重要的信息，删除次要细节\n"
            f"3. 如果碎片之间有矛盾，以最新/最具体的为准\n"
            f"4. 输出 JSON 数组格式：{{\"statements\": [\"...\", \"...\"]}}\n"
            f"5. 如果碎片已经够简洁且互不重复，可以原样保留（但优先合并）\n\n"
            f"碎片：\n{numbered}\n\n"
            f"输出（纯 JSON，不要 markdown）："
        )
        try:
            from xmclaw.core.ir import Message
            resp = await self._llm.complete(
                [Message(role="user", content=prompt)],
                tools=None,
            )
            raw = (resp.content or "").strip()
            if raw.startswith("```"):
                raw = raw.removeprefix("```json").removeprefix("```")
                raw = raw.removesuffix("```").strip()
            data = json.loads(raw)
            statements = data.get("statements") if isinstance(data, dict) else None
            if isinstance(statements, list):
                return [str(s).strip() for s in statements if str(s).strip()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("consolidate.llm_synthesize_parse_failed err=%s", exc)
        return [facts[0].text] if facts else []

    async def _detect_stale_long_term(
        self,
        svc: Any,
    ) -> int:
        """Scan long_term facts and detect potentially stale ones.

        Heuristic: for each long_term fact, search for newer working
        facts in the same bucket that are vector-close. If found, stamp
        ``invalid_at`` on the old fact so it stops surfacing.
        """
        try:
            old_facts = await svc.recall(
                only_layer="long_term",
                k=100,
                keyword_only=True,
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("consolidate.fetch_long_term_failed err=%s", exc)
            return 0

        stale_marked = 0
        now = time.time()
        for hit in old_facts:
            f = hit.fact
            try:
                neighbours = await svc.recall(
                    f.text,
                    k=3,
                    min_confidence=0.0,
                    include_relations=False,
                    include_superseded=False,
                )
                for nb in neighbours:
                    if (
                        nb.fact.id != f.id
                        and getattr(nb.fact, "layer", "") == "working"
                        and getattr(nb.fact, "ts_last", 0) > getattr(f, "ts_last", 0)
                    ):
                        d = float(getattr(nb, "distance", 1.0))
                        if d < 0.25:
                            if getattr(f, "invalid_at", None) is None:
                                f.invalid_at = now
                                f.confidence = min(getattr(f, "confidence", 1.0), 0.3)
                                await svc._vec.upsert([f])
                                stale_marked += 1
                        break
            except Exception:  # noqa: BLE001
                pass

        return stale_marked

    # ── Scope 3: groom_goals (1-day) ─────────────────────────────

    async def groom_goals(self, *, tick: int) -> CycleResult:
        """Prune completed goals, drop stale ones, replan stuck ones."""
        t0 = time.perf_counter()
        if self._cognitive_state is None:
            return CycleResult(scope="groom", ran=False)

        goals = list(getattr(self._cognitive_state, "current_goals", []) or [])
        before = len(goals)
        if before == 0:
            return CycleResult(scope="groom", ran=False)

        now = time.time()
        kept: list[Any] = []
        completed_archived = 0
        stale_dropped = 0
        stuck_replanned = 0

        for g in goals:
            status = (getattr(g, "status", "") or "").lower()
            updated_at = float(getattr(g, "updated_at", 0.0) or 0.0)
            created_at = float(getattr(g, "created_at", updated_at) or 0.0)
            age_since = now - max(updated_at, created_at)

            if status in ("completed", "done", "achieved"):
                completed_archived += 1
                continue
            if (
                status in ("blocked", "stuck", "waiting")
                and age_since > self._groom_blocked_seconds
            ):
                # Mark for replan rather than drop — keeps user
                # intent alive; the planner will re-decompose next
                # tick.
                try:
                    if hasattr(g, "status"):
                        # CognitiveState's Goal is dataclass(frozen=True)
                        # in some forks, mutable in others. Replace via
                        # reconstruction when frozen.
                        try:
                            g.status = "needs_replan"  # type: ignore[misc]
                        except Exception:  # noqa: BLE001
                            from dataclasses import replace
                            g = replace(g, status="needs_replan")
                except Exception:  # noqa: BLE001
                    pass
                stuck_replanned += 1
                kept.append(g)
                continue
            if (
                status in ("active", "pending", "needs_replan")
                and age_since > self._groom_stale_seconds
            ):
                stale_dropped += 1
                continue
            kept.append(g)

        # Write back the pruned list.
        try:
            self._cognitive_state.current_goals = kept
        except Exception:  # noqa: BLE001
            pass

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = {
            "before": before,
            "after": len(kept),
            "completed_archived": completed_archived,
            "stale_dropped": stale_dropped,
            "stuck_replanned": stuck_replanned,
            "elapsed_ms": round(elapsed_ms, 2),
        }
        await self._publish_event("goals_groomed", summary)
        return CycleResult(
            scope="groom", ran=True, summary=summary,
            elapsed_ms=elapsed_ms,
        )

    # ── Scope 4: metacognize (R3, 2026-05-10) ────────────────────

    async def metacognize(self, *, tick: int) -> CycleResult:
        """Run a MetaCognitionPass + Reformer.

        Pulls recent decision traces, asks the LLM for behavioural
        patterns, routes each surviving Pattern through the Reformer
        into a ReformProposal, and emits METACOGNITION_PROPOSAL
        events. The proposals themselves are NOT auto-applied — the
        operator (or, in R5, the AutonomyPolicy) decides whether to
        approve them.

        Skips when ``metacognition_pass`` or ``reformer`` not wired.
        """
        t0 = time.perf_counter()
        if self._metacognition_pass is None or self._reformer is None:
            return CycleResult(scope="metacognize", ran=False)

        try:
            patterns = await self._metacognition_pass.run(
                lookback=self._metacognize_lookback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("metacognize.pass_failed err=%s", exc)
            return CycleResult(
                scope="metacognize", ran=False,
                error=f"pass: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        proposals_emitted = 0
        kinds: list[str] = []
        for pat in patterns:
            try:
                rp = self._reformer.propose(pat)
                if rp.kind == "no_op":
                    continue
                # Use Reformer.emit if available (static method).
                emit = getattr(self._reformer, "emit", None)
                if callable(emit):
                    await emit(rp, bus=self._bus, agent_id=self._agent_id)
                proposals_emitted += 1
                kinds.append(rp.kind)
            except Exception as exc:  # noqa: BLE001
                logger.warning("metacognize.reform_failed err=%s", exc)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = {
            "patterns_found": len(patterns),
            "proposals_emitted": proposals_emitted,
            "proposal_kinds": kinds,
            "lookback": self._metacognize_lookback,
            "elapsed_ms": round(elapsed_ms, 2),
        }
        # No dedicated event for the cycle summary — patterns +
        # proposals already published. Return a CycleResult so the
        # CognitiveDaemon's tick summary still counts this run.
        return CycleResult(
            scope="metacognize", ran=True,
            summary=summary, elapsed_ms=elapsed_ms,
        )

    # ── Internals ─────────────────────────────────────────────────

    def _format_events(self, events: list[Any]) -> str:
        """Render the recent-events block for the reflection prompt.

        Each event becomes one line; we use whatever attributes are
        present (BehavioralEvent has ``type`` + ``payload``; raw dicts
        also work). Cap each line to ~250 chars so the prompt stays
        cheap.
        """
        lines: list[str] = []
        for i, ev in enumerate(events):
            t = getattr(ev, "type", None) or (
                ev.get("type") if isinstance(ev, dict) else "?"
            )
            t_str = getattr(t, "value", None) or str(t)
            payload = getattr(ev, "payload", None) or (
                ev.get("payload", {}) if isinstance(ev, dict) else {}
            )
            try:
                payload_str = json.dumps(payload, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                payload_str = str(payload)
            if len(payload_str) > 200:
                payload_str = payload_str[:197] + "..."
            lines.append(f"{i+1}. [{t_str}] {payload_str}")
        return "\n".join(lines)

    async def _extract_thoughts(self, prompt: str) -> list[InnerThought]:
        """Run the LLM, parse the JSON list, return InnerThoughts."""
        try:
            from xmclaw.providers.llm.base import Message
            resp = await self._llm.complete([
                Message(role="user", content=prompt),
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflection.llm_failed err=%s", exc)
            return []

        content = (getattr(resp, "content", "") or "").strip()
        # Strip code fences if the LLM wrapped despite instructions.
        if content.startswith("```"):
            # Crude but effective for the common ```json prefix.
            content = content.lstrip("`")
            if content.lower().startswith("json"):
                content = content[4:]
            content = content.strip("`").strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(
                "reflection.bad_json preview=%r", content[:200],
            )
            return []
        if not isinstance(data, list):
            return []

        out: list[InnerThought] = []
        for item in data[:5]:  # hard cap so a runaway LLM doesn't flood
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "observation")).lower().strip()
            if kind not in (
                "reflection", "wonder", "concern", "plan", "observation",
            ):
                kind = "observation"
            text = str(item.get("text", "")).strip()
            trigger = str(item.get("trigger", "")).strip() or "recent_events"
            if not text:
                continue
            out.append(InnerThought(
                kind=kind,  # type: ignore[arg-type]
                text=text[:600],
                trigger=trigger[:200],
            ))
        return out

    async def _publish_thought(
        self, t: InnerThought, *, tick: int,
    ) -> None:
        """Emit one InnerThought as an INNER_MONOLOGUE event."""
        await self._publish_event(
            "inner_monologue",
            {
                "kind": t.kind,
                "text": t.text,
                "tick": tick,
                "trigger": t.trigger,
            },
        )

    async def _publish_event(
        self, type_name: str, payload: dict[str, Any],
    ) -> None:
        """Publish via bus when wired; silent no-op otherwise."""
        if self._bus is None:
            return
        try:
            from xmclaw.core.bus import EventType, make_event
            try:
                ev_type = EventType(type_name)
            except ValueError:
                # Unknown to this build — skip silently. Lets the
                # cycle ship before the EventType enum is in place
                # (defensive against schema drift).
                return
            ev = make_event(
                session_id="_system",
                agent_id=self._agent_id,
                type=ev_type,
                payload=payload,
            )
            await self._bus.publish(ev)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection.publish_failed type=%s err=%s",
                type_name, exc,
            )


__all__ = [
    "CycleResult",
    "InnerThought",
    "ReflectionCycle",
]