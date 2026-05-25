"""JarvisOrchestrator — top-level dispatch layer.

2026-05-25 architectural decision (user request — see CLAUDE.md
开发纪律 Phase note in JARVIS_PLAN): the dual-path
"trivial AgentLoop vs complex PlanEngine→WorkerSwarm" design
caused two independent execution paths to publish to the same
parent session, producing UI races (worker status rows stacked
under another path's final reply). The complex path is now
*disabled* — every turn goes through ``AgentLoop.run_turn`` and
the LLM decides whether to fan out via the ``parallel_subagents``
tool (which is in the catalogue and obeys a single, observable
event timeline in the parent session).

PlanEngine + WorkerSwarm remain wired but are no longer reachable
from ``handle()``. They are kept in-tree for one release so that
external callers (tests, future cognition pathways) can still
import them; a follow-up release will remove them entirely.

The ``force_complex`` parameter is retained for tests but routed
back to ``run_turn`` with a logged warning so the surface area
stays stable.
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
        """Route a user message to ``AgentLoop.run_turn``.

        The complex (PlanEngine→WorkerSwarm) branch is disabled —
        see module docstring. ``force_complex`` is honoured only to
        the extent of logging an audit line so callers know it was
        ignored.
        """
        start = time.monotonic()

        if force_complex:
            _log.info(
                "orchestrator.force_complex_ignored session=%s "
                "(complex path retired 2026-05-25; LLM should fan out "
                "via parallel_subagents)",
                session_id,
            )

        result = await self._agent_loop.run_turn(
            session_id=session_id,
            user_message=user_message,
            llm_profile_id=llm_profile_id,
            tools_allowlist=tools_allowlist,
            user_correlation_id=user_correlation_id,
            user_images=user_images,
        )
        text = (
            getattr(result, "content", "")
            or getattr(result, "output", "")
            or ""
        )
        return OrchestratorResult(
            ok=True,
            output=str(text),
            elapsed_seconds=time.monotonic() - start,
            path="trivial",
        )

    # ── classification (kept for tests; no longer wired) ──

    def _is_complex(self, message: str) -> bool:  # pragma: no cover
        """Retained for back-compat with tests that introspect the
        classifier. Always returns False now — the LLM owns the
        fanout decision via the ``parallel_subagents`` tool.
        """
        return False
