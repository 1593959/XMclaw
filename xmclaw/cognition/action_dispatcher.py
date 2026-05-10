"""ActionDispatcher — Jarvis Phase 6.7 minimal stub.

Routes plan steps to executors. **v0 (this commit) is a stub**: every
``PlanStep`` simply gets its ``expected_outcome`` echoed back as a
success result with no real side effect. Real wiring (call
``AgentLoop.run_turn``, invoke a registered ``Skill``, dispatch a
``ToolProvider`` call, suspend on ``wait_for_percept``) is a Phase 6.7
follow-up commit — this file ships the **interface** so
:class:`xmclaw.cognition.cognitive_daemon.CognitiveDaemon` has a stable
contract to call against.

Why ship a stub now: the alternative is for ``CognitiveDaemon`` to call
into ``Any``-typed dispatcher ducks invented case-by-case in tests, and
each subsequent integration step would have to invent its own. Defining
the surface here once means the rest of Phase 6.7 (and downstream
dispatcher tickets) all target the same shape.

The dispatcher contract (also satisfied by tests' fakes):
* ``await dispatcher.execute_plan(plan) -> dict`` — run every step in
  the plan in order; return aggregate ``{"plan_id", "status", "step_results"}``.
* ``await dispatcher.execute_step(step) -> dict`` — run one step;
  return ``{"step_id", "ok", "outcome", ...}``.
* ``await dispatcher.dispatch(step) -> dict`` — alias of
  ``execute_step``, kept because :meth:`xmclaw.cognition.planner.Planner.execute`
  drives steps through ``dispatcher.dispatch(step)``.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.8.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ActionDispatcher:
    """Minimal v0 dispatcher — echoes ``expected_outcome`` as success.

    Real routing (AgentLoop / Skills / Tools / wait_for_percept) is the
    Phase 6.7 follow-up. This stub is defensive: it never raises, and
    every method returns a well-shaped dict so the
    :class:`CognitiveDaemon` summary accounting always works.
    """

    def __init__(
        self,
        agent_loop: Any | None = None,
        skill_registry: Any | None = None,
        tool_provider: Any | None = None,
    ) -> None:
        # Stored for the follow-up — reading them in v0 stub is fine
        # but we intentionally do NOT call into them so the contract
        # of "no side effects in v0" stays honest.
        self._agent_loop = agent_loop
        self._skill_registry = skill_registry
        self._tool_provider = tool_provider

    async def execute_plan(self, plan: Any) -> dict[str, Any]:
        """Execute every step in the plan, in plan order.

        Returns ``{"plan_id", "status", "step_results"}``. Status is
        ``"completed"`` if all steps return ``ok=True``, ``"failed"``
        otherwise. Never raises — a step that throws is logged and
        captured into ``step_results``.
        """
        plan_id = getattr(plan, "id", None) or "<no-plan-id>"
        steps = list(getattr(plan, "steps", ()) or ())

        step_results: list[dict[str, Any]] = []
        all_ok = True
        for step in steps:
            try:
                outcome = await self.execute_step(step)
            except Exception as exc:  # noqa: BLE001 — never raise from here
                logger.exception(
                    "ActionDispatcher.execute_step raised for step "
                    "%s; treating as failure",
                    getattr(step, "id", "<unknown>"),
                )
                outcome = {
                    "step_id": getattr(step, "id", "<unknown>"),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            step_results.append(outcome)
            if not outcome.get("ok", False):
                all_ok = False

        return {
            "plan_id": plan_id,
            "status": "completed" if all_ok else "failed",
            "step_results": step_results,
        }

    async def execute_step(self, step: Any) -> dict[str, Any]:
        """Execute one step. v0: echo ``expected_outcome`` as success.

        Returns ``{"step_id", "ok", "outcome", "action_kind",
        "stub": True, "executed_at": <ts>}``. The ``stub: True`` flag
        is critical — downstream consumers (eventually a
        ``ResultObserver``) can audit which results came from real
        executors vs the v0 echo and refuse to learn from stubs.
        """
        step_id = getattr(step, "id", None) or "<no-step-id>"
        expected = getattr(step, "expected_outcome", "") or ""
        action_kind = getattr(step, "action_kind", "llm_turn") or "llm_turn"
        return {
            "step_id": step_id,
            "ok": True,
            "outcome": expected,
            "action_kind": action_kind,
            "stub": True,
            "executed_at": time.time(),
        }

    async def dispatch(self, step: Any) -> dict[str, Any]:
        """Alias of :meth:`execute_step` for the Planner's contract.

        :meth:`xmclaw.cognition.planner.Planner.execute` drives steps
        through ``await dispatcher.dispatch(step)``. Keeping the alias
        here means a single ``ActionDispatcher`` instance satisfies
        both the Planner's executor protocol and the CognitiveDaemon's
        plan-runner protocol without an adapter shim.
        """
        return await self.execute_step(step)


__all__ = ["ActionDispatcher"]
