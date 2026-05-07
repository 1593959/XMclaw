"""B-294: drive ``EvolutionAgent.evaluate()`` on a debounced schedule.

Phase 3.1 left the self-improvement chain critically half-wired:

  hop 1: agent_loop → grader.grade()                   ✓ fires every tool call
  hop 2: GRADER_VERDICT → EvolutionAgent._ingest       ✓ EWMA per (skill_id, ver)
  hop 3: aggregate → evaluate() → controller.decide()  ✗✗✗ NEVER CALLED IN PROD
  hop 4: PROMOTE proposal → orchestrator               ✓ subscribed
  hop 5: orchestrator → registry.promote               ⚠ gated by auto_apply

``EvolutionAgent.evaluate()`` is fully implemented (evolution_agent.py:293-320)
and is the ONLY way to turn an aggregated EWMA snapshot into a
``SKILL_CANDIDATE_PROPOSED`` event. But ``grep -rn "\.evaluate("`` against
the production daemon code returns zero matches — only tests + the bench
harness call it. Production daemon ``app.py:656-666`` does
``await evo_agent.start()`` then **never touches the agent again**. Verdicts
silently accumulate in ``_arms`` forever, evaluate is unreachable, no
candidate ever proposes, no skill ever promotes. The "self-evolving agent"
promise is dead in production.

This trigger closes hop 3. Same pattern as B-164's
``RealtimeEvolutionTrigger`` (skill_dream.py:246-410):

  * subscribe to GRADER_VERDICT bus events
  * debounce so a burst of verdicts (one turn = N tool calls) collapses
    into a single evaluate() call after the burst settles
  * cooldown so multi-session bursts don't pin the eval loop
  * minimum-new-verdicts threshold so we don't waste a controller round
    on N=2 plays — wait until there's enough signal to be worth the call

Default settings are intentionally conservative:
  * ``debounce_s=30`` — wait 30s of quiet after the last verdict before
    firing. RealtimeEvolutionTrigger uses 15s for proposal generation;
    evaluation is less time-sensitive (the proposal goes through human
    review when ``auto_apply=False``).
  * ``cooldown_s=300`` (5min) — at most one evaluate() per 5 minutes.
    The controller's decision is monotonic given the same snapshot, so
    re-running every 60s would just burn CPU.
  * ``min_new_verdicts=10`` — only fire when we've ingested at least 10
    new verdicts since the last evaluate(). The controller's
    ``min_plays`` threshold defaults to 5; below that there's nothing
    to decide.

The trigger does NOT pass ``head_version`` / ``head_mean`` to evaluate()
— at this layer we don't know what the SkillRegistry's HEAD pointer is.
``evaluate()`` defaults those to None, which the controller interprets
as "no incumbent baseline known; promote anything that clears plays +
mean thresholds". Phase 2 of this work can wire SkillRegistry.head() into
the trigger to enable gap-vs-head decisions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from xmclaw.core.bus import EventType, InProcessEventBus

if TYPE_CHECKING:
    from xmclaw.core.bus import BehavioralEvent
    from xmclaw.daemon.evolution_agent import EvolutionAgent

log = logging.getLogger(__name__)


class EvolutionEvaluationTrigger:
    """Subscribe to GRADER_VERDICT, fire EvolutionAgent.evaluate() on a
    debounced + cooldown schedule when enough new verdicts have arrived.

    Lifecycle: ``start()`` subscribes; ``stop()`` cancels subscription
    + any in-flight debounce timer. Both idempotent. The daemon's
    lifespan calls them in order around evolution_agent.start/stop.
    """

    # Don't recurse on internal sessions that themselves emit verdicts.
    # ``evolution:*`` is the agent's own audit emissions; the others are
    # background workspaces that shouldn't drive HEAD movement.
    _SKIP_SESSION_PREFIXES = (
        "_system",
        "skill-dream",
        "dream:",
        "evolution:",
        "reflect:",
    )

    def __init__(
        self,
        evo_agent: "EvolutionAgent",
        bus: InProcessEventBus,
        *,
        debounce_s: float = 30.0,
        cooldown_s: float = 300.0,
        min_new_verdicts: int = 10,
        enabled: bool = True,
    ) -> None:
        self._evo_agent = evo_agent
        self._bus = bus
        # Tiny floor so config of 0 doesn't pin the loop; tests pass
        # smaller values explicitly.
        self._debounce_s = max(0.01, float(debounce_s))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._min_new_verdicts = max(1, int(min_new_verdicts))
        self._enabled = bool(enabled)

        self._subscription = None
        self._pending_task: asyncio.Task | None = None
        self._last_run_ts: float = 0.0
        self._fire_lock = asyncio.Lock()
        self._fire_count: int = 0
        self._verdicts_since_last_fire: int = 0

    # ── Public surface ──────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    @property
    def fire_count(self) -> int:
        """Total successful evaluate() invocations. Useful for tests."""
        return self._fire_count

    @property
    def verdicts_since_last_fire(self) -> int:
        """How many GRADER_VERDICT events ingested since last evaluate.
        Resets to 0 after each successful fire."""
        return self._verdicts_since_last_fire

    async def start(self) -> None:
        """Subscribe to GRADER_VERDICT. Idempotent. No-op when disabled."""
        if not self._enabled:
            return
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe(
            self._predicate, self._on_verdict,
        )
        log.info(
            "evolution_eval.start debounce_s=%.1f cooldown_s=%.1f "
            "min_new_verdicts=%d",
            self._debounce_s, self._cooldown_s, self._min_new_verdicts,
        )

    async def stop(self) -> None:
        """Cancel subscription + pending debounce. Idempotent."""
        sub = self._subscription
        self._subscription = None
        if sub is not None:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass
        pending = self._pending_task
        self._pending_task = None
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── Bus handler + debounce ──────────────────────────────────────

    def _predicate(self, event: "BehavioralEvent") -> bool:
        if event.type is not EventType.GRADER_VERDICT:
            return False
        sid = event.session_id or ""
        for pref in self._SKIP_SESSION_PREFIXES:
            if sid.startswith(pref):
                return False
        return True

    async def _on_verdict(self, _event: "BehavioralEvent") -> None:
        """Reset the debounce: cancel previous pending fire, schedule fresh.

        The latest verdict in a burst wins — we want one evaluate() call
        per burst, not one per individual verdict. Burst size is tracked
        via ``_verdicts_since_last_fire`` so the threshold check at fire
        time can short-circuit on tiny bursts.
        """
        self._verdicts_since_last_fire += 1

        prev = self._pending_task
        if prev is not None and not prev.done():
            prev.cancel()
        self._pending_task = asyncio.create_task(self._wait_and_fire())

    async def _wait_and_fire(self) -> None:
        """Sleep debounce_s, then check cooldown + threshold, then evaluate."""
        try:
            await asyncio.sleep(self._debounce_s)
        except asyncio.CancelledError:
            return  # superseded by a fresher verdict — let the new task fire

        # Threshold check: don't bother the controller with too few plays.
        if self._verdicts_since_last_fire < self._min_new_verdicts:
            return

        # Cooldown check: at most one evaluate per cooldown_s.
        loop = asyncio.get_event_loop()
        now = loop.time()
        elapsed = now - self._last_run_ts
        if self._last_run_ts > 0.0 and elapsed < self._cooldown_s:
            return

        # Serialise concurrent fires (defensive — debounce should already
        # collapse most bursts, but channel adapters can fire from a
        # different task without going through the debounce path).
        if not self._fire_lock.locked():
            async with self._fire_lock:
                await self._fire()

    async def _fire(self) -> None:
        """Call evaluate() and bookkeep. Errors are swallowed +
        logged — a misbehaving controller MUST NOT bring down the
        agent loop's verdict producer."""
        try:
            report = await self._evo_agent.evaluate()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "evolution_eval.fire_failed err=%s "
                "verdicts_since_last_fire=%d",
                exc, self._verdicts_since_last_fire,
            )
            # Don't reset counters — next burst gets a fresh shot.
            return

        loop = asyncio.get_event_loop()
        self._last_run_ts = loop.time()
        self._fire_count += 1
        verdicts_consumed = self._verdicts_since_last_fire
        self._verdicts_since_last_fire = 0

        log.info(
            "evolution_eval.fired count=%d decision=%s "
            "verdicts_consumed=%d",
            self._fire_count,
            getattr(report.decision, "value", str(report.decision)),
            verdicts_consumed,
        )


__all__ = ["EvolutionEvaluationTrigger"]
