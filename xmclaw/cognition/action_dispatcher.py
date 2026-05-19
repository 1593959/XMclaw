"""ActionDispatcher — Jarvis Phase 6.7 wiring follow-up B.

Routes plan steps to real executors based on ``PlanStep.action_kind``:

* ``llm_turn``         → ``AgentLoop.run_turn(session_id=goal_id, …)``
* ``skill_invoke``     → ``SkillRegistry.get(skill_id).run(SkillInput)``
* ``tool_call``        → ``ToolProvider.invoke(ToolCall)``
* ``wait_for_percept`` → returns immediately with ``pending=True``;
  the :class:`xmclaw.cognition.cognitive_daemon.CognitiveDaemon` main
  loop drives the resumption when the awaited percept arrives.

This commit replaces the v0 stub from cab6fb4 (which only echoed
``expected_outcome``). The :class:`CognitiveDaemon` constructor is
unchanged — once this lands, the daemon picks up the real impl
automatically because it constructs ``ActionDispatcher`` by name.

**Defensive contract.** Every routing method is best-effort:

* Routes NEVER raise out of :meth:`execute_step` / :meth:`execute_plan` —
  exceptions become ``StepExecutionResult(ok=False, error=str(exc))``
  so the caller (Planner / CognitiveDaemon) can apply
  ``PlanStep.retry_policy`` without a crash.
* When an executor for the requested ``action_kind`` is *not wired*
  (e.g. ``agent_loop=None`` because the daemon is running in a
  pure-cognition test harness), we transparently fall back to
  :meth:`_stub_execute` which echoes ``expected_outcome`` like the v0
  stub — so test harnesses and bench mode still work without
  fabricating real LLM / skill / tool plumbing.

* All collaborator types are duck-typed (``Any``). ``xmclaw/cognition/``
  is in ``core/`` import-direction territory and MUST NOT import from
  ``providers/`` or ``daemon/``. Real instances are wired in by
  ``daemon/factory.py``; tests inject minimal fakes.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.8 for the spec.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Epic #26 Phase B (2026-05-19) — lazy import of EventType / make_event
# so the cognition package keeps its import-direction discipline (this
# module sits below ``xmclaw.core.bus.events`` in the DAG; importing it
# at module-top is fine because bus/events is pure data, no deps). The
# ``try`` guard is for the test harness that may stub the import path.
try:
    from xmclaw.core.bus.events import EventType, make_event
except Exception:  # noqa: BLE001
    EventType = None  # type: ignore[assignment]
    make_event = None  # type: ignore[assignment]


# Pre-compiled template pattern for prior-result substitution. Matches
# ``{{step_id.path.to.field}}`` — the path is a dotted accessor into
# the step's output dict. Outer braces are LITERAL ``{{`` / ``}}``;
# this is template syntax, not Python format strings.
_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


ExecutorRoute = Literal[
    "llm_turn",
    "skill_invoke",
    "tool_call",
    "wait_for_percept",
    "stub",
]


# ── Result dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class StepExecutionResult:
    """Outcome of a single :class:`PlanStep` dispatch.

    ``route`` records which executor actually ran (``"stub"`` when we
    fell back because the corresponding collaborator was not wired).
    ``output`` is the action-specific payload — no schema is enforced
    across routes because each executor produces fundamentally different
    shapes (LLM turn summary vs SkillOutput vs ToolResult).

    ``pending=True`` is reserved for ``wait_for_percept``: the step has
    not failed; it is suspended pending external input. The
    CognitiveDaemon decides what to do with a pending step (typically
    parks the plan until the awaited percept lands, then resumes).
    """

    step_id: str
    route: ExecutorRoute
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    pending: bool = False


@dataclass(frozen=True)
class PlanExecutionResult:
    """Aggregate outcome of executing every step in a :class:`Plan`."""

    plan_id: str
    step_results: tuple[StepExecutionResult, ...]
    all_ok: bool
    error: str | None = None


# ── Dispatcher ─────────────────────────────────────────────────────────


class ActionDispatcher:
    """Real action routing for Phase 6 plans.

    Constructor parameters are duck-typed:

    * ``agent_loop``     — exposes ``await run_turn(session_id, user_message)``.
    * ``skill_registry`` — exposes ``get(skill_id, version=None) -> Skill``
      and raises a LookupError-shaped exception when the id is unknown.
      A ``SkillInput``-shape (``args: dict``) is constructed by the
      dispatcher; the registered skill's ``run(SkillInput)`` is awaited
      and its ``SkillOutput.result`` becomes the step output.
    * ``tool_provider``  — exposes ``await invoke(ToolCall) -> ToolResult``.
      The dispatcher synthesises a ``ToolCall`` from the step's payload
      (``tool_name`` + ``args``) using a lightweight duck-shape so we
      avoid importing ``xmclaw.core.ir`` from this module. (See
      :func:`_make_tool_call_shape`.)

    Any of the three may be ``None``; in that case the corresponding
    route falls through to :meth:`_stub_execute`. This keeps the
    dispatcher useful in test harnesses and bench mode where wiring
    the full provider graph is unnecessary.
    """

    def __init__(
        self,
        agent_loop: Any | None = None,
        skill_registry: Any | None = None,
        tool_provider: Any | None = None,
        *,
        bus: Any | None = None,
    ) -> None:
        self._agent_loop = agent_loop
        self._skill_registry = skill_registry
        self._tool_provider = tool_provider
        # Epic #26 Phase B (2026-05-19): optional event bus so
        # execute_plan can emit PLAN_STARTED / PLAN_STEP_STARTED /
        # PLAN_STEP_COMPLETED / PLAN_STEP_FAILED / PLAN_COMPLETED /
        # PLAN_FAILED. None = silent (test harness / bench mode).
        # Type-loose so tests can inject a list-of-events recorder
        # rather than the full InProcessEventBus.
        self._bus = bus

    # ── Public surface ────────────────────────────────────────────────

    async def execute_plan(self, plan: Any) -> PlanExecutionResult:
        """Execute every step in ``plan`` in order.

        We honour the plan's own topology by reading ``plan.steps`` as
        the iteration order — the :class:`Planner` already topologically
        sorts steps before stamping them on the Plan dataclass.

        Stops on first failure UNLESS the failed step's ``retry_policy``
        sets ``continue_on_failure: True`` (the Planner's own retry
        loop handles per-step retries; this method only decides whether
        the **plan as a whole** keeps going past a hard failure).

        ``pending`` results from ``wait_for_percept`` do NOT count as
        failure but DO halt plan execution — the CognitiveDaemon parks
        the plan until the awaited percept arrives, then re-invokes
        ``execute_plan`` (or just the suspended subgraph) to resume.

        Epic #26 Phase B (2026-05-19) — two structural additions:

        1. **prior_results threading**: each step receives a dict of
           prior steps' outputs keyed by step_id. The route methods
           use this for ``{{step_id.field}}`` template substitution
           in payload strings (intent / prompt / args), so step_2 can
           reference step_1's result without the planner having to
           hard-code it. Backward-compatible: a step whose payload
           contains no ``{{...}}`` markers behaves identically to
           pre-Phase-B.
        2. **Plan lifecycle bus events**: PLAN_STARTED at entry,
           PLAN_STEP_STARTED / PLAN_STEP_COMPLETED / PLAN_STEP_FAILED
           per step, PLAN_COMPLETED / PLAN_FAILED at exit. Events fire
           best-effort (bus optional, swallow publish errors). The UI
           "Autonomous Tasks" panel + observability layer subscribe.
        """
        plan_id = _attr_str(plan, "id", "<no-plan-id>")
        goal_id = _attr_str(plan, "goal_id", "")
        steps = list(_attr_iter(plan, "steps"))
        t0_plan = time.monotonic()

        await self._emit_plan_event(
            EventType.PLAN_STARTED if EventType else None,
            plan_id=plan_id,
            goal_id=goal_id,
            payload={
                "plan_id": plan_id,
                "goal_id": goal_id,
                "n_steps": len(steps),
                "step_ids": [
                    _attr_str(s, "id", "?") for s in steps
                ],
                "confidence": _attr_float(plan, "confidence", 0.5),
            },
        )

        results: list[StepExecutionResult] = []
        # Epic #26 Phase B: accumulator for prior step outputs.
        # Keyed by step_id → step.output dict. Passed to execute_step
        # so template references in later step payloads can resolve.
        prior_results: dict[str, dict[str, Any]] = {}
        all_ok = True
        agg_error: str | None = None

        for idx, step in enumerate(steps):
            step_id = _attr_str(step, "id", "<unknown>")
            action_kind = _attr_str(step, "action_kind", "llm_turn")
            t0_step = time.monotonic()
            await self._emit_plan_event(
                EventType.PLAN_STEP_STARTED if EventType else None,
                plan_id=plan_id,
                goal_id=goal_id,
                payload={
                    "plan_id": plan_id,
                    "goal_id": goal_id,
                    "step_id": step_id,
                    "step_index": idx,
                    "action_kind": action_kind,
                    "n_steps": len(steps),
                },
            )
            try:
                outcome = await self.execute_step(
                    step, prior_results=prior_results,
                )
            except Exception as exc:  # noqa: BLE001 — we never propagate
                # execute_step itself NEVER raises; defence-in-depth.
                logger.exception(
                    "ActionDispatcher.execute_step unexpectedly raised "
                    "for step %s; converting to failure",
                    step_id,
                )
                outcome = StepExecutionResult(
                    step_id=step_id,
                    route="stub",
                    ok=False,
                    output={},
                    error=f"{type(exc).__name__}: {exc}",
                    latency_ms=(time.monotonic() - t0_step) * 1000.0,
                )

            results.append(outcome)
            # Stash output so later steps can reference it via
            # {{step_id.field}} substitution.
            if outcome.output:
                prior_results[step_id] = dict(outcome.output)

            if outcome.ok and not outcome.pending:
                await self._emit_plan_event(
                    EventType.PLAN_STEP_COMPLETED if EventType else None,
                    plan_id=plan_id,
                    goal_id=goal_id,
                    payload={
                        "plan_id": plan_id,
                        "goal_id": goal_id,
                        "step_id": step_id,
                        "step_index": idx,
                        "action_kind": action_kind,
                        "latency_ms": outcome.latency_ms,
                        "output_keys": list(outcome.output.keys()),
                    },
                )
            elif not outcome.ok:
                await self._emit_plan_event(
                    EventType.PLAN_STEP_FAILED if EventType else None,
                    plan_id=plan_id,
                    goal_id=goal_id,
                    payload={
                        "plan_id": plan_id,
                        "goal_id": goal_id,
                        "step_id": step_id,
                        "step_index": idx,
                        "action_kind": action_kind,
                        "latency_ms": outcome.latency_ms,
                        "error": outcome.error or "unknown",
                    },
                )

            if outcome.pending:
                # Plan is parked, not failed. Caller decides resumption.
                # No PLAN_COMPLETED — the plan is suspended.
                return PlanExecutionResult(
                    plan_id=plan_id,
                    step_results=tuple(results),
                    all_ok=False,
                    error=None,
                )

            if not outcome.ok:
                all_ok = False
                if not _retry_policy_continue(step):
                    agg_error = (
                        f"step {outcome.step_id} failed: {outcome.error}"
                    )
                    duration_ms = (time.monotonic() - t0_plan) * 1000.0
                    await self._emit_plan_event(
                        EventType.PLAN_FAILED if EventType else None,
                        plan_id=plan_id,
                        goal_id=goal_id,
                        payload={
                            "plan_id": plan_id,
                            "goal_id": goal_id,
                            "n_steps": len(steps),
                            "n_step_results": len(results),
                            "status": "failed",
                            "duration_ms": duration_ms,
                            "error": agg_error,
                        },
                    )
                    return PlanExecutionResult(
                        plan_id=plan_id,
                        step_results=tuple(results),
                        all_ok=False,
                        error=agg_error,
                    )
                # else: retry_policy says continue — record the failure
                # but proceed with the next step.

        duration_ms = (time.monotonic() - t0_plan) * 1000.0
        await self._emit_plan_event(
            (EventType.PLAN_COMPLETED if all_ok else EventType.PLAN_FAILED)
            if EventType else None,
            plan_id=plan_id,
            goal_id=goal_id,
            payload={
                "plan_id": plan_id,
                "goal_id": goal_id,
                "n_steps": len(steps),
                "n_step_results": len(results),
                "status": "completed" if all_ok else "failed",
                "duration_ms": duration_ms,
            },
        )
        return PlanExecutionResult(
            plan_id=plan_id,
            step_results=tuple(results),
            all_ok=all_ok,
            error=None,
        )

    async def execute_step(
        self,
        step: Any,
        *,
        prior_results: dict[str, dict[str, Any]] | None = None,
    ) -> StepExecutionResult:
        """Route one step to the executor matching its ``action_kind``.

        Never raises — exceptions in any route are caught and converted
        to ``StepExecutionResult(ok=False, error=…)``. The latency
        captured includes all retries / fallbacks taken inside the
        route, so failure latencies are meaningful for observability.

        Epic #26 Phase B (2026-05-19): ``prior_results`` is a dict of
        already-completed step outputs keyed by step_id. The router
        methods use it for ``{{step_id.field}}`` substitution in
        prompts / args. Default None = no substitution, behavior
        matches pre-Phase-B.
        """
        kind = _attr_str(step, "action_kind", "llm_turn") or "llm_turn"
        priors = prior_results or {}

        try:
            if kind == "llm_turn":
                return await self._route_llm_turn(step, prior_results=priors)
            if kind == "skill_invoke":
                return await self._route_skill_invoke(step, prior_results=priors)
            if kind == "tool_call":
                return await self._route_tool_call(step, prior_results=priors)
            if kind == "wait_for_percept":
                return await self._route_wait_for_percept(step)
            # Unknown action_kind: stub fallback so we never crash.
            logger.warning(
                "ActionDispatcher: unknown action_kind %r for step %s; "
                "falling back to stub",
                kind,
                _attr_str(step, "id", "<unknown>"),
            )
            return await self._stub_execute(step)
        except Exception as exc:  # noqa: BLE001 — never propagate
            logger.exception(
                "ActionDispatcher: route for action_kind=%r raised on step %s",
                kind,
                _attr_str(step, "id", "<unknown>"),
            )
            return StepExecutionResult(
                step_id=_attr_str(step, "id", "<unknown>"),
                route="stub",
                ok=False,
                output={},
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _emit_plan_event(
        self,
        event_type: Any,
        *,
        plan_id: str,
        goal_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Best-effort emit of a PLAN_* lifecycle event on the bus.

        No-op when bus or EventType isn't wired (test harness).
        Publish errors are swallowed — these are observability
        signals, not control flow. The async ``execute_plan`` call
        site never needs to ``await`` this in a way that could fail
        the plan itself.
        """
        if self._bus is None or event_type is None or make_event is None:
            return
        # Tests can supply a list-of-events recorder via ``bus=
        # SimpleNamespace(publish=lambda evt: list.append(evt))``.
        # We tolerate both that shape AND the full async-publish bus.
        try:
            event = make_event(
                session_id=f"autonomous:plan:{plan_id}",
                agent_id="action-dispatcher",
                type=event_type,
                payload=payload,
            )
            publish = getattr(self._bus, "publish", None)
            if publish is None:
                return
            result = publish(event)
            # InProcessEventBus.publish is async; sync test stubs are
            # callable returning None. Await only when awaitable.
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001
            logger.exception(
                "ActionDispatcher._emit_plan_event failed; "
                "swallowing so plan execution is not blocked"
            )

    async def dispatch(self, step: Any) -> dict[str, Any]:
        """Compatibility shim for the :class:`Planner.execute` contract.

        The Planner calls ``await dispatcher.dispatch(step)`` and treats
        a returned-dict as success / a raised exception as failure. We
        therefore turn :class:`StepExecutionResult` into a dict and
        re-raise on ``ok=False`` so the Planner's own retry budget
        applies. Pending results are surfaced via the dict — the
        Planner does not understand ``pending`` natively, so we tag
        them and let the Planner treat them as completed (the
        CognitiveDaemon's plan-level orchestration handles real
        suspension).
        """
        result = await self.execute_step(step)
        payload: dict[str, Any] = {
            "step_id": result.step_id,
            "route": result.route,
            "ok": result.ok,
            "output": dict(result.output),
            "latency_ms": result.latency_ms,
            "pending": result.pending,
        }
        if result.error is not None:
            payload["error"] = result.error
        if not result.ok and not result.pending:
            # Planner.execute uses raises as the failure signal.
            raise RuntimeError(result.error or "step failed")
        return payload

    # ── Routes ────────────────────────────────────────────────────────

    async def _route_llm_turn(
        self,
        step: Any,
        *,
        prior_results: dict[str, dict[str, Any]] | None = None,
    ) -> StepExecutionResult:
        """Drive an LLM turn via the wired ``AgentLoop``."""
        if self._agent_loop is None:
            return await self._stub_execute(step)

        step_id = _attr_str(step, "id", _new_id("step"))
        payload = _attr_dict(step, "payload")
        # Epic #26 Phase B: substitute ``{{step_id.field}}`` references
        # in prompt / intent / expected_outcome against prior step
        # outputs so a multi-step plan can chain results without the
        # planner having to hard-code them upfront. Missing refs
        # render as ``<unresolved:...>`` strings so the agent sees
        # the problem rather than a silent gap.
        priors = prior_results or {}
        prompt = (
            _substitute_priors(payload.get("prompt"), priors)
            or _substitute_priors(payload.get("intent"), priors)
            or _substitute_priors(
                _attr_str(step, "expected_outcome", ""), priors,
            )
        )
        # Use the goal_id from the step's plan context when present —
        # the CognitiveDaemon parks the goal_id on the step's payload
        # under "goal_id" when materialising the plan; we tolerate its
        # absence and fall back to a UNIQUE auto-generated id rather
        # than the raw step_id.
        #
        # Wave-32+ (2026-05-19) collision fix: pre-fix the fallback
        # was just ``step_id`` — but the planner prompt template ships
        # ``"id": "step_1"`` as an example, and LLM-generated plans
        # routinely copy that literal verbatim. Across many plans this
        # collapsed every "step_1" / "step_2" llm_turn into a SINGLE
        # shared session, ballooning to 282+ messages of unrelated
        # autonomous work in one bucket. New rule: when no goal_id is
        # provided we mint ``autonomous:<step_id>:<uuid>`` so each
        # plan gets its own session(s) AND the colon prefix marks it
        # as internal for the Sessions UI filter to hide.
        session_id = (
            payload.get("goal_id")
            or payload.get("session_id")
            or f"autonomous:{step_id}:{uuid.uuid4().hex[:8]}"
        )

        t0 = time.monotonic()
        try:
            result = await self._agent_loop.run_turn(
                session_id=session_id,
                user_message=str(prompt),
            )
        except Exception as exc:  # noqa: BLE001
            return StepExecutionResult(
                step_id=step_id,
                route="llm_turn",
                ok=False,
                output={"session_id": session_id},
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )

        latency_ms = (time.monotonic() - t0) * 1000.0
        return StepExecutionResult(
            step_id=step_id,
            route="llm_turn",
            ok=True,
            output={
                "session_id": session_id,
                "agent_result": _coerce_jsonish(result),
            },
            latency_ms=latency_ms,
        )

    async def _route_skill_invoke(
        self,
        step: Any,
        *,
        prior_results: dict[str, dict[str, Any]] | None = None,
    ) -> StepExecutionResult:
        """Look up and invoke a registered skill."""
        if self._skill_registry is None:
            return await self._stub_execute(step)

        step_id = _attr_str(step, "id", _new_id("step"))
        payload = _attr_dict(step, "payload")
        skill_id = (
            payload.get("skill_id")
            or payload.get("skill_name")
            or payload.get("intent")
        )
        if not skill_id:
            return StepExecutionResult(
                step_id=step_id,
                route="skill_invoke",
                ok=False,
                output={},
                error="skill_invoke step is missing skill_id in payload",
            )

        raw_skill_args = payload.get("skill_args")
        if isinstance(raw_skill_args, dict):
            skill_args: dict[str, Any] = dict(raw_skill_args)
        else:
            fallback = payload.get("args")
            skill_args = dict(fallback) if isinstance(fallback, dict) else {}
        # Epic #26 Phase B: template-substitute any string values in
        # skill_args that reference prior steps (``{{step_id.field}}``).
        if prior_results:
            skill_args = _substitute_priors_in_dict(skill_args, prior_results)

        # Prefer a duck-typed `find` (Planner uses that path) and fall
        # back to the canonical `get` API on SkillRegistry.
        skill: Any | None = None
        find = getattr(self._skill_registry, "find", None)
        if callable(find):
            try:
                skill = find(skill_id)
            except Exception:  # noqa: BLE001
                skill = None
        if skill is None:
            getter = getattr(self._skill_registry, "get", None)
            if callable(getter):
                try:
                    skill = getter(str(skill_id))
                except Exception as exc:  # noqa: BLE001
                    return StepExecutionResult(
                        step_id=step_id,
                        route="skill_invoke",
                        ok=False,
                        output={"skill_id": skill_id},
                        error=(
                            f"skill not found: {skill_id} "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    )

        if skill is None:
            return StepExecutionResult(
                step_id=step_id,
                route="skill_invoke",
                ok=False,
                output={"skill_id": skill_id},
                error=f"skill not found: {skill_id}",
            )

        # Build a SkillInput-shape duck. We avoid importing the real
        # SkillInput dataclass (xmclaw/skills/base.py) to keep this
        # module a leaf — the Skill protocol only requires `.args`.
        skill_input = _SkillInputDuck(args=skill_args)

        t0 = time.monotonic()
        try:
            output = await skill.run(skill_input)
        except Exception as exc:  # noqa: BLE001
            return StepExecutionResult(
                step_id=step_id,
                route="skill_invoke",
                ok=False,
                output={"skill_id": skill_id},
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )

        latency_ms = (time.monotonic() - t0) * 1000.0
        ok = bool(getattr(output, "ok", True))
        result_payload = _coerce_jsonish(getattr(output, "result", output))
        side_effects = getattr(output, "side_effects", None)
        return StepExecutionResult(
            step_id=step_id,
            route="skill_invoke",
            ok=ok,
            output={
                "skill_id": skill_id,
                "result": result_payload,
                "side_effects": list(side_effects) if side_effects else [],
            },
            error=None if ok else "skill returned ok=False",
            latency_ms=latency_ms,
        )

    async def _route_tool_call(
        self,
        step: Any,
        *,
        prior_results: dict[str, dict[str, Any]] | None = None,
    ) -> StepExecutionResult:
        """Invoke a tool via the wired ``ToolProvider``."""
        if self._tool_provider is None:
            return await self._stub_execute(step)

        step_id = _attr_str(step, "id", _new_id("step"))
        payload = _attr_dict(step, "payload")
        tool_name = (
            payload.get("tool_name")
            or payload.get("name")
            or payload.get("intent")
        )
        if not tool_name:
            return StepExecutionResult(
                step_id=step_id,
                route="tool_call",
                ok=False,
                output={},
                error="tool_call step is missing tool_name in payload",
            )

        raw_tool_args = payload.get("tool_args")
        if isinstance(raw_tool_args, dict):
            tool_args: dict[str, Any] = dict(raw_tool_args)
        else:
            fallback_args = payload.get("args")
            tool_args = dict(fallback_args) if isinstance(fallback_args, dict) else {}
        # Epic #26 Phase B: substitute {{step_id.field}} references in
        # tool_args strings so a tool_call step can reference outputs
        # from earlier steps (e.g. ``file_path: "{{step1.path}}"``).
        if prior_results:
            tool_args = _substitute_priors_in_dict(tool_args, prior_results)

        call = _make_tool_call_shape(name=str(tool_name), args=tool_args)

        t0 = time.monotonic()
        try:
            result = await self._tool_provider.invoke(call)
        except Exception as exc:  # noqa: BLE001
            return StepExecutionResult(
                step_id=step_id,
                route="tool_call",
                ok=False,
                output={"tool_name": tool_name},
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )

        latency_ms = (time.monotonic() - t0) * 1000.0
        ok = bool(getattr(result, "ok", True))
        raw_content = getattr(result, "content", result)
        content = _coerce_jsonish(raw_content)
        err = getattr(result, "error", None)

        # B-273 parity: scan tool results for prompt injection before
        # they land in the agent's history.  AgentInterTools does this
        # for sub-agent replies; we do it for every tool_call route so
        # that a malicious/compromised tool cannot inject instructions.
        if isinstance(raw_content, str):
            try:
                from xmclaw.security import (
                    PolicyMode,
                    SOURCE_TOOL_RESULT,
                    apply_policy,
                )
                policy = getattr(
                    self._agent_loop, "_injection_policy", PolicyMode.DETECT_ONLY,
                )
                decision = apply_policy(
                    raw_content,
                    policy=policy,
                    source=SOURCE_TOOL_RESULT,
                    extra={"tool_name": tool_name, "step_id": step_id},
                )
                if decision.blocked:
                    content = (
                        "[B-273 tool result blocked by prompt-injection "
                        "policy — see PROMPT_INJECTION_DETECTED event]"
                    )
                    ok = False
                else:
                    content = _coerce_jsonish(decision.content)
            except Exception:  # noqa: BLE001 — never block on scanner failure
                pass

        return StepExecutionResult(
            step_id=step_id,
            route="tool_call",
            ok=ok,
            output={
                "tool_name": tool_name,
                "content": content,
                "side_effects": list(getattr(result, "side_effects", ()) or ()),
            },
            error=None if ok else (str(err) if err else "tool returned ok=False"),
            latency_ms=latency_ms,
        )

    async def _route_wait_for_percept(self, step: Any) -> StepExecutionResult:
        """Suspend the plan pending a percept.

        Returns immediately with ``pending=True`` and ``ok=True``. The
        :class:`CognitiveDaemon`'s main loop sees the ``pending`` flag
        and parks the plan until the awaited percept arrives on the
        :class:`PerceptionBus`. We do NOT block the dispatcher here —
        blocking would starve the heartbeat.
        """
        step_id = _attr_str(step, "id", _new_id("step"))
        payload = _attr_dict(step, "payload")
        wait_for = (
            payload.get("percept_kind")
            or payload.get("wait_for")
            or payload.get("intent")
            or "any"
        )
        return StepExecutionResult(
            step_id=step_id,
            route="wait_for_percept",
            ok=True,
            output={
                "percept_kind": wait_for,
                "expected_outcome": _attr_str(step, "expected_outcome", ""),
            },
            pending=True,
            # Effectively zero — we did no blocking work.
            latency_ms=0.0,
        )

    async def _stub_execute(self, step: Any) -> StepExecutionResult:
        """Fallback used when the matching collaborator is not wired.

        Echoes ``expected_outcome`` like the v0 stub — preserves the
        bench / pure-cognition test harness behaviour from cab6fb4.
        Marked ``route="stub"`` so consumers (eventually a
        ``ResultObserver``) can refuse to learn from non-real results.
        """
        step_id = _attr_str(step, "id", _new_id("step"))
        expected = _attr_str(step, "expected_outcome", "")
        kind = _attr_str(step, "action_kind", "llm_turn") or "llm_turn"
        return StepExecutionResult(
            step_id=step_id,
            route="stub",
            ok=True,
            output={
                "expected_outcome": expected,
                "action_kind": kind,
                "stub": True,
            },
            latency_ms=0.0,
        )


# ── Helpers ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SkillInputDuck:
    """Minimal SkillInput duck-shape — see :class:`xmclaw.skills.base.SkillInput`.

    Kept local to avoid `cognition → skills` import (skills depend on
    core/, not the other way round, and we want this module to remain
    a leaf).
    """

    args: dict[str, Any]


@dataclass(frozen=True)
class _ToolCallDuck:
    """Minimal ToolCall duck-shape — see :class:`xmclaw.core.ir.ToolCall`.

    Carries the four fields any ``ToolProvider.invoke`` consumer relies
    on: ``name``, ``args``, ``id``, ``provenance``. Real ToolProviders
    introspect more (``raw_snippet``, ``schema_version``) but tolerate
    None / default values; for the dispatcher's synthetic call shape
    that is the right contract.
    """

    name: str
    args: dict[str, Any]
    id: str
    provenance: str = "synthetic"
    raw_snippet: str | None = None
    session_id: str | None = None
    schema_version: int = 1


def _make_tool_call_shape(*, name: str, args: dict[str, Any]) -> _ToolCallDuck:
    return _ToolCallDuck(
        name=name,
        args=args,
        id=uuid.uuid4().hex,
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _attr_str(obj: Any, name: str, default: str) -> str:
    """Read ``obj.name`` (or ``obj[name]`` if obj is a dict) as str."""
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
    if isinstance(v, str) and v:
        return v
    if v is not None:
        return str(v)
    return default


def _attr_dict(obj: Any, name: str) -> dict[str, Any]:
    """Read ``obj.name`` as a dict; coerce / default to {} if missing."""
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
    if isinstance(v, dict):
        return v
    return {}


def _attr_iter(obj: Any, name: str) -> tuple[Any, ...]:
    """Read ``obj.name`` as an iterable; default to empty tuple."""
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
    if v is None:
        return ()
    try:
        return tuple(v)
    except TypeError:
        return ()


def _retry_policy_continue(step: Any) -> bool:
    """True iff the step's ``retry_policy`` says to continue past failure.

    Honours the ``continue_on_failure: True`` extension key (the
    Planner preserves unknown retry_policy keys for forward-compat).
    """
    if isinstance(step, dict):
        rp = step.get("retry_policy")
    else:
        rp = getattr(step, "retry_policy", None)
    if not isinstance(rp, dict):
        return False
    return bool(rp.get("continue_on_failure", False))


def _coerce_jsonish(value: Any) -> Any:
    """Best-effort coercion of an arbitrary executor return into a
    JSON-serialisable shape for ``StepExecutionResult.output``.

    We are deliberately not strict — the dispatcher's job is to record
    *what happened*, not to enforce a schema across heterogeneous
    executors. The Honest Grader / observer layers do schema policing
    later. For dicts, lists, primitives we pass through; for anything
    else we ``repr()`` it so observability still has a string handle.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    return repr(value)


# ── Epic #26 Phase B (2026-05-19) — prior-results threading helpers ──


def _attr_float(obj: Any, name: str, default: float) -> float:
    """Read ``obj.name`` (or ``obj[name]``) as a float. Tolerant
    of dataclasses and dicts, falls back to ``default`` on missing
    / non-coercible values. Mirrors ``_attr_str`` / ``_attr_dict``."""
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _resolve_prior_path(
    path: str, priors: dict[str, dict[str, Any]],
) -> tuple[Any, bool]:
    """Resolve a dotted path ``step_id.field.sub`` against the prior
    results map. Returns ``(value, found)``. ``found=False`` when any
    segment is missing — caller decides how to render the unresolved
    reference.

    The first segment is the step_id (looked up in priors). Subsequent
    segments are nested dict accesses. List indices like
    ``step1.items.0`` work via str → int coercion.
    """
    parts = [p.strip() for p in path.split(".") if p.strip()]
    if not parts:
        return None, False
    step_id, *rest = parts
    cur: Any = priors.get(step_id)
    if cur is None:
        return None, False
    for seg in rest:
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
                continue
            return None, False
        if isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
                continue
            except (ValueError, IndexError):
                return None, False
        # Attribute access fallback for object-shaped values.
        attr = getattr(cur, seg, _MISSING_SENTINEL)
        if attr is _MISSING_SENTINEL:
            return None, False
        cur = attr
    return cur, True


_MISSING_SENTINEL = object()


def _substitute_priors(
    s: Any,
    priors: dict[str, dict[str, Any]],
) -> Any:
    """If ``s`` is a string containing ``{{step_id.field}}`` markers,
    return a new string with each marker replaced by the resolved
    value (``str(...)`` of it). Non-string input passes through
    unchanged. Missing references render as
    ``<unresolved:step_id.field>`` so the LLM sees the problem
    rather than a silent empty.

    Pre-Phase-B: dispatcher passed payload strings to executors
    verbatim, so a planner that wrote ``"prompt": "summarize
    {{step_1.output.text}}"`` would just send the literal template
    to the LLM. Now the dispatcher resolves it.
    """
    if not isinstance(s, str) or "{{" not in s:
        return s

    def _sub(match: re.Match[str]) -> str:
        path = match.group(1)
        value, found = _resolve_prior_path(path, priors)
        if not found:
            return f"<unresolved:{path}>"
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        # Compact dump for complex values so the LLM sees structured
        # but won't see {} characters that trigger re-substitution.
        try:
            import json as _json
            return _json.dumps(value, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return repr(value)

    return _TEMPLATE_RE.sub(_sub, s)


def _substitute_priors_in_dict(
    d: dict[str, Any],
    priors: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Recurse into a dict and substitute ``{{step_id.field}}`` in
    every string value. Lists are walked element-wise; nested dicts
    recurse. Non-string scalars pass through. Keys are NOT
    substituted — only values."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _substitute_priors(v, priors)
        elif isinstance(v, dict):
            out[k] = _substitute_priors_in_dict(v, priors)
        elif isinstance(v, list):
            out[k] = [
                _substitute_priors_in_dict(x, priors) if isinstance(x, dict)
                else _substitute_priors(x, priors) if isinstance(x, str)
                else x
                for x in v
            ]
        else:
            out[k] = v
    return out


# Compatibility re-exports — async API and result dataclasses.
__all__ = [
    "ActionDispatcher",
    "ExecutorRoute",
    "PlanExecutionResult",
    "StepExecutionResult",
]
