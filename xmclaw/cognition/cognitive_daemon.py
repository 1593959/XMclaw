"""CognitiveDaemon — Jarvis Phase 6.7 final integration.

The heartbeat-driven main loop that wires Phase 6.1–6.6 modules into a
single always-on cognitive process:

    PerceptionBus  →  AttentionFilter.tick()  →  actionable percepts
                                                         │
                                                         ▼
                                            ReasoningEngine.reason()
                                                         │
                                                         ▼
                                                Planner.plan() / execute()
                                                         │
                                                         ▼
                                            ActionDispatcher.execute_plan()

Periodically (every N ticks) the daemon also:
* asks :class:`xmclaw.cognition.goal_generator.GoalGenerator` for new
  maintenance / exploration / social goals (subject to
  :class:`AutonomyPolicy`); the resulting goals feed the Planner on
  subsequent ticks.
* runs the :class:`xmclaw.cognition.self_experiment.SelfExperimentLoop`
  if the autonomy policy permits self-experiments.

**Lifecycle.** :meth:`start` spawns a background ``asyncio.Task`` that
runs :meth:`_run` until :meth:`stop` cancels it. Heartbeat cadence is
``config.heartbeat_hz`` (default 1 Hz). :meth:`tick_once` is public so
tests can drive ticks deterministically without sleep.

**Safety contract.** Every tick is best-effort:

* Each pipeline step (attention / reason / plan / dispatch / goal
  generation / experiment) runs inside its own ``try/except`` block.
  Exceptions are logged and recorded in the per-tick summary's
  ``errors`` list; they NEVER propagate out of :meth:`tick_once` or
  :meth:`_run`.
* :meth:`_run` itself catches any exception, sleeps the heartbeat
  interval, and continues. The daemon NEVER raises out of its
  background task.
* :meth:`stop` is idempotent and bounded by ``timeout_s``.

This commit is the **consumer side**: it builds the loop that drains
percepts and turns them into action. Wiring percept *producers*
(WS / file / cron / network → ``PerceptionBus.push``) is a separate
follow-up; until then the daemon ticks an empty bus and just keeps
periodic goal generation + self-experiments alive when configured.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.8 for the spec.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CognitiveDaemonConfig:
    """Operator-tunable knobs for :class:`CognitiveDaemon`.

    Defaults (post 2026-05-10 "贾维斯化") are **opt-out**: the daemon
    runs continuous cognition by default, with ``autonomy_level=50``
    (suggest tier — proactive proposals surfaced for review but never
    auto-applied). Operator can dial down to 0 (observe-only) or up
    to 100 (execute) per their trust level.
    """

    enabled: bool = True
    autonomy_level: int = 50          # 0..100 (clamped at runtime)
    heartbeat_hz: float = 1.0          # ticks per second
    action_threshold: float = 0.6      # passed to AttentionFilter
    top_k_focus: int = 7               # 7 ± 2 working-memory cap

    # How often (in ticks) to invoke the optional periodic subsystems.
    # The defaults at 1 Hz heartbeat give:
    #   goal_generator  every  60s
    #   self_experiment every 600s (10 min)
    goal_gen_every_n_ticks: int = 60
    self_experiment_every_n_ticks: int = 600
    skill_propose_every_n_ticks: int = 300

    max_pending_goals: int = 16

    # Phase D: warn when any subsystem exceeds this latency (ms).
    # 500 ms default at 1 Hz heartbeat means a single slow step
    # consumes half the tick budget — worth surfacing.
    slow_subsystem_threshold_ms: float = 500.0


# ── Daemon ────────────────────────────────────────────────────────────


class CognitiveDaemon:
    """Heartbeat-driven main cognitive loop.

    Construct with the Phase 6.1–6.6 modules already built; the daemon
    just composes them. Every collaborator is duck-typed so tests can
    inject minimal fakes without dragging in the real graph / LLM /
    skill registry.
    """

    def __init__(
        self,
        config: CognitiveDaemonConfig,
        bus: Any,                         # PerceptionBus duck
        attention: Any,                   # AttentionFilter duck
        reasoning: Any | None = None,     # ReasoningEngine duck
        planner: Any | None = None,       # Planner duck
        goal_generator: Any | None = None,  # GoalGenerator duck
        experiment_loop: Any | None = None,  # SelfExperimentLoop duck
        process_watcher: Any | None = None,  # ProcessWatcher duck
        cognitive_state: Any | None = None,  # CognitiveState duck (for AutonomyPolicy)
        dispatcher: Any | None = None,    # ActionDispatcher duck
        # 2026-05-10 R1: ReflectionCycle wires the 3-bucket periodic
        # introspection (5 min reflect / 1 h consolidate / 1 day groom).
        # ``None`` disables the entire reflection layer — daemon falls
        # back to the legacy "react to percepts" loop unchanged.
        reflection_cycle: Any | None = None,
        skill_proposer: Any | None = None,   # SkillProposer duck
        event_bus: Any | None = None,        # InProcessEventBus duck for publishing proposals
        # Phase D: optional TickStore for persisting tick summaries.
        tick_store: Any | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._attention = attention
        self._reasoning = reasoning
        self._planner = planner
        self._goal_generator = goal_generator
        self._experiment_loop = experiment_loop
        self._process_watcher = process_watcher
        self._state = cognitive_state
        self._dispatcher = dispatcher
        self._reflection_cycle = reflection_cycle
        self._skill_proposer = skill_proposer
        self._event_bus = event_bus
        self._tick_store = tick_store

        self._task: asyncio.Task[Any] | None = None
        self._running = False
        self._tick_count = 0
        # Phase E: asyncio.Event so _run() sleep can be interrupted
        # immediately on stop() without cancelling an in-flight tick.
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public properties (mainly for tests + observability)
    # ------------------------------------------------------------------

    @property
    def config(self) -> CognitiveDaemonConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tick_count(self) -> int:
        return self._tick_count

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background heartbeat task. Idempotent."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="cognitive-daemon")
        logger.info(
            "CognitiveDaemon started: heartbeat_hz=%.2f autonomy_level=%d",
            self._config.heartbeat_hz,
            self._config.autonomy_level,
        )

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the background loop. Bounded by ``timeout_s`` seconds.

        Idempotent: safe to call when not running. Signals the loop
        via :attr:`_stop_event` so that an in-flight tick is allowed
        to finish (graceful).  If the tick does not complete within
        ``timeout_s``, the task is cancelled.  The daemon must never
        block an enclosing lifespan shutdown.
        """
        self._running = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return

        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "CognitiveDaemon.stop graceful timeout after %.1fs; "
                "cancelling task",
                timeout_s,
            )
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning(
                    "CognitiveDaemon.stop force-cancel timed out; "
                    "abandoning task",
                )
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("CognitiveDaemon background task raised on stop")

    def update_config(self, new_config: CognitiveDaemonConfig) -> None:
        """Replace the frozen config at runtime.

        Called by the lifespan's CONFIG_RELOADED handler when
        ``cognition.continuous_loop.*`` fields change in config.json.
        The new config takes effect on the *next* tick (heartbeat_hz
        is read fresh each cycle, and autonomy_level is consulted
        inside :meth:`tick_once`).
        """
        old_hz = self._config.heartbeat_hz
        self._config = new_config
        if old_hz != new_config.heartbeat_hz:
            logger.info(
                "CognitiveDaemon config updated: heartbeat_hz %.2f -> %.2f",
                old_hz, new_config.heartbeat_hz,
            )

    # ------------------------------------------------------------------
    # The heartbeat
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Background loop. Ticks at ``heartbeat_hz`` until stopped.

        Iron rule: this method NEVER raises out. Every per-tick
        exception is captured by :meth:`tick_once`; everything else
        (stop-event wake-up, etc.) is caught here. This is the safety
        net for the integrating lifespan — a buggy collaborator must
        not be able to take down the daemon process.
        """
        # Period in seconds. Treat heartbeat_hz <= 0 as "tick as fast
        # as possible" (test-only convenience); production uses 1 Hz.
        period = (
            1.0 / self._config.heartbeat_hz
            if self._config.heartbeat_hz > 0
            else 0.0
        )

        while self._running:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — must never crash the loop
                logger.exception("CognitiveDaemon tick raised; continuing")

            if not self._running or self._stop_event.is_set():
                break

            # Phase E: use wait_for on the stop event so stop() can
            # wake us immediately without cancelling an in-flight tick.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=period,
                )
            except asyncio.TimeoutError:
                pass  # normal — time for next tick
            except asyncio.CancelledError:
                raise

    async def tick_once(self) -> dict[str, Any]:
        """One heartbeat. Returns a summary of what happened.

        Public so tests can drive the loop deterministically without
        spinning real wall-clock sleep. The returned dict is keyed:

        * ``tick`` — sequential tick number (post-increment).
        * ``n_percepts`` — actionable percepts surfaced by AttentionFilter.
        * ``n_actionable`` — same number, kept for backward-compat with
          tests / dashboards that read either name.
        * ``n_goals_spawned`` — goals minted by GoalGenerator this tick.
        * ``n_plans_executed`` — plans that reached the dispatcher.
        * ``ran_experiment`` — whether SelfExperimentLoop fired this tick.
        * ``latency_ms`` — per-subsystem latency breakdown (Phase D).
        * ``errors`` — list of human-readable strings, one per caught
          exception. Empty when the tick was clean.

        NEVER raises; collaborators that raise are recorded in
        ``errors`` and the tick continues.
        """
        self._tick_count += 1
        tick = self._tick_count
        errors: list[str] = []
        actionable: list[Any] = []
        n_goals_spawned = 0
        n_plans_executed = 0
        ran_experiment = False
        latency_ms: dict[str, float] = {}

        async def _timed(name: str, coro):
            """Await *coro*, record wall-clock ms in ``latency_ms``."""
            t0 = time.perf_counter()
            try:
                return await coro
            finally:
                latency_ms[name] = round(
                    (time.perf_counter() - t0) * 1000, 2,
                )

        # 1. Attention pass — drain bus, score, return actionable.
        try:
            actionable = list(await _timed("attention", self._attention.tick()))
        except Exception as exc:  # noqa: BLE001
            logger.exception("CognitiveDaemon: attention.tick failed")
            errors.append(f"attention.tick: {type(exc).__name__}: {exc}")
            actionable = []

        # 2. For each actionable percept: reason → plan → dispatch.
        for percept in actionable:
            try:
                await _timed("react", self._react_to_percept(percept))
                n_plans_executed += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "CognitiveDaemon: reaction to percept %s failed",
                    getattr(percept, "id", "<unknown>"),
                )
                errors.append(
                    f"react_to_percept[{getattr(percept, 'id', '?')}]: "
                    f"{type(exc).__name__}: {exc}"
                )

        # 3. Periodic goal generation.
        if self._should_spawn_goals(tick):
            try:
                spawned = await _timed("goals", self._spawn_goals())
                n_goals_spawned = spawned
            except Exception as exc:  # noqa: BLE001
                logger.exception("CognitiveDaemon: goal-spawn failed")
                errors.append(f"goal_generator: {type(exc).__name__}: {exc}")

        # 4. Periodic self-experiment.
        if self._should_run_experiment(tick):
            try:
                ran_experiment = await _timed("experiment", self._run_experiment())
            except Exception as exc:  # noqa: BLE001
                logger.exception("CognitiveDaemon: self-experiment failed")
                errors.append(f"self_experiment: {type(exc).__name__}: {exc}")

        # 5. R1: 3-bucket reflection cycle.
        n_reflections = 0
        if self._reflection_cycle is not None:
            try:
                results = await _timed("reflection", self._reflection_cycle.run_due(tick))
                n_reflections = len(results)
            except Exception as exc:  # noqa: BLE001
                logger.exception("CognitiveDaemon: reflection_cycle failed")
                errors.append(
                    f"reflection_cycle: {type(exc).__name__}: {exc}",
                )

        # 6. Periodic skill proposal.
        n_skill_proposals = 0
        if self._should_propose_skills(tick):
            try:
                n_skill_proposals = await _timed("skills", self._run_skill_proposer())
            except Exception as exc:  # noqa: BLE001
                logger.exception("CognitiveDaemon: skill_proposer failed")
                errors.append(
                    f"skill_proposer: {type(exc).__name__}: {exc}",
                )

        # Phase D: slow-subsystem warnings (non-blocking).
        threshold = self._config.slow_subsystem_threshold_ms
        for subsys, ms in latency_ms.items():
            if ms >= threshold:
                errors.append(
                    f"slow_subsystem: {subsys}={ms:.1f}ms "
                    f"(threshold={threshold:.0f}ms)"
                )

        summary = {
            "tick": tick,
            "n_percepts": len(actionable),
            "n_actionable": len(actionable),
            "n_goals_spawned": n_goals_spawned,
            "n_plans_executed": n_plans_executed,
            "ran_experiment": ran_experiment,
            "n_reflections": n_reflections,
            "n_skill_proposals": n_skill_proposals,
            "latency_ms": latency_ms,
            "errors": errors,
        }

        # Stash for the /daemon endpoint to serve without re-computing.
        self._last_tick_summary = summary

        # Phase D: persist to TickStore for history queries.
        if self._tick_store is not None:
            try:
                await self._tick_store.save(
                    {**summary, "timestamp": time.time()},
                )
            except Exception:  # noqa: BLE001
                logger.exception("CognitiveDaemon: tick_store.save failed")

        # Publish tick summary to the event bus so dashboards / audit
        # logs / health monitors can observe daemon activity without
        # polling.
        if self._event_bus is not None:
            try:
                from xmclaw.core.bus.events import EventType, make_event

                event = make_event(
                    session_id="_system:cognitive_daemon",
                    agent_id="cognitive-daemon",
                    type=EventType.COGNITIVE_DAEMON_TICK,
                    payload={
                        **summary,
                        "timestamp": time.time(),
                    },
                )
                await self._event_bus.publish(event)
            except Exception:  # noqa: BLE001
                logger.exception("CognitiveDaemon: failed to publish tick event")

        return summary

    # ------------------------------------------------------------------
    # Per-tick subsystems
    # ------------------------------------------------------------------

    async def _react_to_percept(self, percept: Any) -> None:
        """Reason → Plan → Dispatch for one actionable percept.

        Each sub-call is independently fault-tolerant: a missing
        collaborator (e.g. no planner) just truncates the pipeline
        instead of erroring.
        """
        # Reasoning is optional; we still try to plan even when it's
        # absent (the planner can drive off the percept directly).
        reasoning_result = None
        if self._reasoning is not None:
            query = self._percept_to_query(percept)
            try:
                reasoning_result = await self._reasoning.reason(query)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "CognitiveDaemon: reasoning.reason raised; continuing"
                )

        if self._planner is None or self._dispatcher is None:
            # No way to act. The percept already updated working memory
            # via AttentionFilter; that's the best we can do.
            return

        # Build a goal-shaped dict from the percept and ask the planner
        # to decompose it. The planner duck accepts dicts (see
        # _goal_to_prompt_blob in planner.py).
        goal_blob = self._percept_to_goal(percept)
        if reasoning_result is not None:
            goal_blob = self._inject_reasoning_into_goal(
                goal_blob, reasoning_result,
            )

        try:
            plan = await self._planner.plan(goal_blob)
        except Exception:  # noqa: BLE001
            logger.exception("CognitiveDaemon: planner.plan raised; skipping dispatch")
            return

        # Empty / failed plan: nothing to dispatch.
        if not plan or not getattr(plan, "steps", None):
            return

        try:
            await self._dispatcher.execute_plan(plan)
        except Exception:  # noqa: BLE001
            logger.exception(
                "CognitiveDaemon: dispatcher.execute_plan raised; "
                "plan dropped"
            )

    async def _spawn_goals(self) -> int:
        """Run the GoalGenerator, return number of goals spawned.

        AutonomyPolicy gating happens INSIDE GoalGenerator (it returns
        ``[]`` when the policy disallows). We additionally short-circuit
        at level 0 here so a missing/zero-level policy never even pays
        the call. Returns the count of newly minted goals.
        """
        if self._goal_generator is None:
            return 0
        # Defensive: AutonomyPolicy at level 0 means "fully manual";
        # skip the call entirely. Level is the runtime setting on the
        # generator's policy if available, else our config's.
        level = self._policy_level()
        if level <= 0:
            return 0
        spawned = await self._goal_generator.generate_all()
        try:
            return len(list(spawned or ()))
        except TypeError:
            return 0

    async def _run_experiment(self) -> bool:
        """Fire one self-experiment cycle if the policy permits.

        v0 contract: only fire when the autonomy policy enables
        ``self_experiment``. The actual experiment to run is the loop's
        responsibility — we just trigger and return whether a cycle was
        kicked off. The loop's ``execute()`` requires a fully-built
        :class:`Experiment` + factories which the daemon does not
        synthesise; instead we call the loop's optional
        ``tick()`` / ``run_due()`` shim if it exposes one. If the loop
        only exposes the lower-level :meth:`propose` / :meth:`execute`
        pair, this method is a no-op (returns False) and the wiring
        ticket fills in the appropriate driver.
        """
        if self._experiment_loop is None:
            return False
        if not self._self_experiment_allowed():
            return False
        # Prefer a high-level tick API if the loop exposes one. We
        # don't import :class:`SelfExperimentLoop` directly, so we
        # duck-check.
        for method_name in ("tick", "run_due", "step"):
            fn = getattr(self._experiment_loop, method_name, None)
            if callable(fn):
                outcome = fn()
                if asyncio.iscoroutine(outcome):
                    await outcome
                return True
        return False

    # ------------------------------------------------------------------
    # Frequency control
    # ------------------------------------------------------------------

    def _should_spawn_goals(self, tick_count: int) -> bool:
        """True every ``goal_gen_every_n_ticks`` ticks (always-true at 1)."""
        if self._goal_generator is None:
            return False
        every = max(1, int(self._config.goal_gen_every_n_ticks))
        return tick_count % every == 0

    def _should_run_experiment(self, tick_count: int) -> bool:
        """True every ``self_experiment_every_n_ticks`` ticks."""
        if self._experiment_loop is None:
            return False
        if not self._self_experiment_allowed():
            return False
        every = max(1, int(self._config.self_experiment_every_n_ticks))
        return tick_count % every == 0

    def _should_propose_skills(self, tick_count: int) -> bool:
        """True every ``skill_propose_every_n_ticks`` ticks."""
        if self._skill_proposer is None:
            return False
        every = max(1, int(self._config.skill_propose_every_n_ticks))
        return tick_count % every == 0

    async def _run_skill_proposer(self) -> int:
        """Run SkillProposer and publish proposals to the event bus.

        Returns the number of proposals emitted.
        """
        if self._skill_proposer is None:
            return 0
        proposals = await self._skill_proposer.propose()
        if not proposals:
            return 0
        if self._event_bus is None:
            logger.debug(
                "skill_proposer: %d proposals but no event_bus wired",
                len(proposals),
            )
            return 0
        count = 0
        for proposed in proposals:
            try:
                from xmclaw.core.bus.events import EventType, make_event
                event = make_event(
                    session_id="_system:skill_proposer",
                    agent_id="cognitive-daemon",
                    type=EventType.SKILL_CANDIDATE_PROPOSED,
                    payload={
                        "decision": "propose",
                        "winner_candidate_id": proposed.skill_id,
                        "evidence": list(proposed.evidence),
                        "reason": (
                            f"pattern={proposed.source_pattern} "
                            f"confidence={proposed.confidence:.2f}"
                        ),
                        "draft": proposed.to_jsonable(),
                    },
                )
                await self._event_bus.publish(event)
                count += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "skill_proposer: failed to publish proposal for %s",
                    getattr(proposed, "skill_id", "?"),
                )
        return count

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _policy_level(self) -> int:
        """Read the autonomy level off the goal_generator's policy when
        present; else fall back to the config's level. Defensive — a
        test fake without ``_policy`` collapses to the config value.
        """
        gen = self._goal_generator
        if gen is not None:
            policy = getattr(gen, "_policy", None) or getattr(gen, "policy", None)
            level = getattr(policy, "level", None)
            if isinstance(level, int):
                return level
        return int(self._config.autonomy_level)

    def _self_experiment_allowed(self) -> bool:
        """True if autonomy policy permits self-experiments.

        Reads ``self_experiment_enabled`` off the goal_generator's
        policy if available; else derives from the config's
        autonomy_level (level >= 75 enables self-experiments — same
        threshold as :class:`AutonomyPolicy`).
        """
        gen = self._goal_generator
        if gen is not None:
            policy = getattr(gen, "_policy", None) or getattr(gen, "policy", None)
            flag = getattr(policy, "self_experiment_enabled", None)
            if isinstance(flag, bool):
                return flag
        return int(self._config.autonomy_level) >= 75

    @staticmethod
    def _percept_to_query(percept: Any) -> str:
        """Render a percept into a natural-language query for reasoning."""
        payload = getattr(percept, "payload", None) or {}
        for key in ("content", "text", "message", "summary", "path"):
            v = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(v, str) and v.strip():
                return v
        return f"{getattr(percept, 'source', '?')}:{getattr(percept, 'kind', '?')}"

    @staticmethod
    def _inject_reasoning_into_goal(
        goal: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        """Merge a :class:`ReasoningResult` into the goal blob so the
        planner sees conclusions, confidence, and suggested goals."""
        conclusion = getattr(result, "conclusion", "") or ""
        confidence = getattr(result, "confidence", 0.0) or 0.0
        suggested = list(getattr(result, "suggested_goals", ()) or ())
        if conclusion:
            goal["reasoning_conclusion"] = conclusion
            goal["reasoning_confidence"] = confidence
        if suggested:
            goal["reasoning_suggested_goals"] = suggested
        return goal

    @staticmethod
    def _percept_to_goal(percept: Any) -> dict[str, Any]:
        """Build a goal-shaped dict from a percept for the Planner."""
        pid = getattr(percept, "id", None) or "<no-id>"
        kind = getattr(percept, "kind", "") or "percept"
        source = getattr(percept, "source", "") or "?"
        description = CognitiveDaemon._percept_to_query(percept)
        return {
            "id": f"goal-from-percept-{pid}",
            "name": f"react_to_{source}_{kind}",
            "description": description,
            "priority": 5,
            "completion_criteria": {"percept_id": pid, "from_percept": True},
            "_generated_at": time.time(),
        }


__all__ = ["CognitiveDaemon", "CognitiveDaemonConfig"]