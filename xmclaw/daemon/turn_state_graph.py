"""Explicit turn-level StateGraph runtime.

This is intentionally lightweight: AgentLoop still owns execution, but each
major phase updates a reducer-backed GraphState so planning, recall, skills,
tools, review, and writeback have a stable state surface.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.cognition.graph_runtime import GraphState, apply_updates


DEFAULT_TURN_PHASES: tuple[str, ...] = (
    "recall",
    "skill_discovery",
    "prompt_pack",
    "hop_loop",
    "memory_writeback",
)


@dataclass(slots=True)
class TurnStateGraph:
    state: GraphState
    phases: tuple[str, ...] = DEFAULT_TURN_PHASES
    enforce_order: bool = False
    _started_at: dict[str, float] = field(default_factory=dict)
    _completed: set[str] = field(default_factory=set)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        phases: tuple[str, ...] = DEFAULT_TURN_PHASES,
        enforce_order: bool = False,
    ) -> "TurnStateGraph":
        state = GraphState(
            thread_id=session_id,
            run_id=run_id,
            goal=(user_message or "")[:500],
        )
        updates: dict[str, Any] = {
            "messages": {"role": "user", "content": (user_message or "")[:4000]},
            "subtasks": [
                {
                    "id": phase,
                    "status": "pending",
                    "kind": "agent_turn_phase",
                    "dependencies": [phases[i - 1]] if i > 0 else [],
                }
                for i, phase in enumerate(phases)
            ],
            "metadata": {
                "runtime": "turn_state_graph",
                "phase_order": list(phases),
            },
        }
        return cls(
            state=apply_updates(state, updates),
            phases=phases,
            enforce_order=enforce_order,
        )

    def start(self, phase: str, **metadata: Any) -> GraphState:
        if self.enforce_order:
            self._assert_can_start(phase)
        self._started_at[phase] = time.perf_counter()
        return self._update_phase(phase, "running", metadata=metadata)

    def complete(self, phase: str, **metadata: Any) -> GraphState:
        elapsed_ms = self._elapsed_ms(phase)
        if elapsed_ms is not None:
            metadata = {**metadata, "elapsed_ms": elapsed_ms}
        self._completed.add(phase)
        return self._update_phase(phase, "completed", metadata=metadata)

    def fail(self, phase: str, error: str, **metadata: Any) -> GraphState:
        elapsed_ms = self._elapsed_ms(phase)
        if elapsed_ms is not None:
            metadata = {**metadata, "elapsed_ms": elapsed_ms}
        self.state = apply_updates(
            self.state,
            {
                "errors": {
                    "kind": "turn_phase_failed",
                    "node_id": phase,
                    "message": error,
                }
            },
        )
        return self._update_phase(
            phase,
            "failed",
            error=error,
            metadata=metadata,
        )

    def _assert_can_start(self, phase: str) -> None:
        if phase not in self.phases:
            raise ValueError(f"unknown StateGraph phase: {phase}")
        idx = self.phases.index(phase)
        missing = [
            prev for prev in self.phases[:idx]
            if prev not in self._completed
        ]
        if missing:
            raise ValueError(
                f"StateGraph phase {phase!r} started before dependencies "
                f"completed: {', '.join(missing)}"
            )

    def finalize(self, final: str, **metadata: Any) -> GraphState:
        self.state = apply_updates(
            self.state,
            {
                "final": final,
                "metadata": {
                    "turn_state_graph_final": final,
                    **metadata,
                },
            },
        )
        return self.state

    def _update_phase(
        self,
        phase: str,
        status: str,
        *,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> GraphState:
        payload: dict[str, Any] = {
            "id": phase,
            "status": status,
            "kind": "agent_turn_phase",
        }
        if error:
            payload["error"] = error
        if metadata:
            payload["metadata"] = metadata
        self.state = apply_updates(self.state, {"subtasks": payload})
        return self.state

    def _elapsed_ms(self, phase: str) -> float | None:
        started = self._started_at.pop(phase, None)
        if started is None:
            return None
        return round((time.perf_counter() - started) * 1000.0, 2)


__all__ = ["DEFAULT_TURN_PHASES", "TurnStateGraph"]
