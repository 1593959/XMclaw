"""JarvisOrchestrator — top-level dispatch layer.

Receives every inbound user message (or proactive intent) and decides:
  * trivial  → direct AgentLoop.run_turn()   (today's behaviour)
  * complex  → PlanEngine → WorkerSwarm      (Jarvis J2)

Design constraints (from JARVIS_ROADMAP):
  * AgentLoop is NOT deleted — it becomes the execution engine.
  * Orchestrator only adds routing + planning; all event contracts stay.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from xmclaw.orchestrator.plan_engine import ExecutionPlan, PlanEngine
from xmclaw.orchestrator.worker_swarm import SwarmResult, WorkerSwarm
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass(slots=True)
class OrchestratorResult:
    """Unified result shape regardless of trivial or complex path."""

    ok: bool
    output: str = ""
    plan: ExecutionPlan | None = None
    swarm_result: SwarmResult | None = None
    elapsed_seconds: float = 0.0
    path: str = "trivial"  # "trivial" | "complex"


class JarvisOrchestrator:
    """Main entry point for agent turns in Jarvis mode.

    Parameters
    ----------
    agent_loop :
        The primary AgentLoop (used directly for trivial, and as the
        engine backing WorkerSwarm for complex).
    plan_engine :
        PlanEngine instance (optional — when None, complex path falls
        back to trivial with a warning).
    worker_swarm :
        WorkerSwarm instance (optional — when None, complex path falls
        back to trivial with a warning).
    classify_threshold :
        Length heuristic: messages longer than this many characters are
        considered potentially complex.  LLM-based classifier is a
        future upgrade (see JARVIS_ROADMAP §5.4).
    """

    def __init__(
        self,
        *,
        agent_loop: Any,
        plan_engine: PlanEngine | None = None,
        worker_swarm: WorkerSwarm | None = None,
        classify_threshold: int = 120,
    ) -> None:
        self._agent_loop = agent_loop
        self._plan_engine = plan_engine
        self._worker_swarm = worker_swarm
        self._classify_threshold = classify_threshold

    async def handle(
        self,
        session_id: str,
        user_message: str,
        *,
        llm_profile_id: str | None = None,
        tools_allowlist: set[str] | frozenset[str] | None = None,
        force_complex: bool = False,
        user_correlation_id: str | None = None,
        user_images: tuple[str, ...] | None = None,
    ) -> OrchestratorResult:
        """Route a user message to the appropriate execution path."""
        start = time.monotonic()

        is_complex = force_complex or self._is_complex(user_message)

        if not is_complex:
            # Trivial path — behave exactly like today's AgentLoop.
            result = await self._agent_loop.run_turn(
                session_id=session_id,
                user_message=user_message,
                llm_profile_id=llm_profile_id,
                tools_allowlist=tools_allowlist,
                user_correlation_id=user_correlation_id,
                user_images=user_images,
            )
            text = getattr(result, "content", "") or getattr(result, "output", "") or ""
            return OrchestratorResult(
                ok=True,
                output=str(text),
                elapsed_seconds=time.monotonic() - start,
                path="trivial",
            )

        # Complex path — Plan → Worker Swarm.
        if self._plan_engine is None or self._worker_swarm is None:
            _log.warning(
                "orchestrator.complex_path_unwired: falling back to trivial"
            )
            result = await self._agent_loop.run_turn(
                session_id=session_id,
                user_message=user_message,
                llm_profile_id=llm_profile_id,
                tools_allowlist=tools_allowlist,
                user_correlation_id=user_correlation_id,
                user_images=user_images,
            )
            text = getattr(result, "content", "") or getattr(result, "output", "") or ""
            return OrchestratorResult(
                ok=True,
                output=str(text),
                elapsed_seconds=time.monotonic() - start,
                path="trivial",
            )

        plan = await self._plan_engine.create_plan(user_message)
        if plan is None or not plan.tasks:
            _log.warning("orchestrator.plan_failed: falling back to trivial")
            result = await self._agent_loop.run_turn(
                session_id=session_id,
                user_message=user_message,
                llm_profile_id=llm_profile_id,
                tools_allowlist=tools_allowlist,
                user_correlation_id=user_correlation_id,
                user_images=user_images,
            )
            text = getattr(result, "content", "") or getattr(result, "output", "") or ""
            return OrchestratorResult(
                ok=True,
                output=str(text),
                elapsed_seconds=time.monotonic() - start,
                path="trivial",
            )

        swarm = await self._worker_swarm.execute_plan(
            plan, parent_session_id=session_id,
        )
        return OrchestratorResult(
            ok=swarm.ok,
            output=swarm.synthesized_output,
            plan=plan,
            swarm_result=swarm,
            elapsed_seconds=time.monotonic() - start,
            path="complex",
        )

    # ── classification ──

    def _is_complex(self, message: str) -> bool:
        """Heuristic classifier.  Returns True when the message looks
        like a multi-step goal.

        Current heuristic (cheap, no LLM call):
          * message length > threshold, OR
          * contains conjunction words like '然后', '接着', '并且', '再',
            'first', 'then', 'and also', 'refactor X to Y'
        """
        text = message.lower()
        if len(message) > self._classify_threshold:
            return True
        indicators = (
            "然后", "接着", "并且", "再", "同时", "先", "后",
            "first ", "then ", "and also", "refactor", "rewrite",
            "migrate", "upgrade", "implement", "create a",
        )
        return any(ind in text for ind in indicators)
