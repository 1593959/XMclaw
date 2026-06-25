"""Turn-level GraphState helpers for AgentLoop."""
from __future__ import annotations

from typing import Any

from xmclaw.cognition.graph_runtime import GraphState, apply_updates


def build_turn_graph_state(
    *,
    session_id: str,
    run_id: str,
    user_message: str,
    artifact_store: Any | None = None,
    prompt_memory_pack_present: bool = False,
    skill_discovery: Any | None = None,
    memory_decisions: list[dict[str, Any]] | None = None,
    tool_reviews: list[dict[str, Any]] | None = None,
) -> GraphState:
    """Build the initial reducer-backed graph state for one agent turn."""
    state = GraphState(
        thread_id=session_id,
        run_id=run_id,
        goal=(user_message or "")[:500],
    )
    metadata: dict[str, Any] = {
        "prompt_memory_pack_present": bool(prompt_memory_pack_present),
    }
    skill_meta = _skill_discovery_metadata(skill_discovery)
    if skill_meta:
        metadata["skill_discovery"] = skill_meta
    if memory_decisions:
        metadata["memory_decisions"] = list(memory_decisions)[-8:]
    if tool_reviews:
        metadata["tool_reviews"] = list(tool_reviews)[-8:]

    updates: dict[str, Any] = {
        "messages": {
            "role": "user",
            "content": (user_message or "")[:4000],
        },
        "subtasks": {
            "id": "turn",
            "status": "running",
            "kind": "agent_turn",
        },
        "metadata": metadata,
    }
    artifacts = _recent_artifacts(artifact_store, session_id=session_id)
    if artifacts:
        updates["artifacts"] = artifacts
    return apply_updates(state, updates)


def _skill_discovery_metadata(decision: Any | None) -> dict[str, Any]:
    if decision is None:
        return {}
    try:
        candidates = [
            c.to_event_payload() if hasattr(c, "to_event_payload") else dict(c)
            for c in list(getattr(decision, "candidates", ()) or ())[:8]
        ]
        required_action = str(getattr(decision, "required_action", "") or "")
        if not required_action:
            required_action = (
                "call_skill_decision_then_use_or_skip"
                if candidates else
                "call_skill_decision_browse_then_skill_browse"
            )
        return {
            "matched": bool(getattr(decision, "matched", False)),
            "candidate_count": len(getattr(decision, "candidates", ()) or ()),
            "candidates": candidates,
            "skip_reasons": list(getattr(decision, "skip_reasons", ()) or ()),
            "recommended_browse_query": str(
                getattr(decision, "recommended_browse_query", "") or "",
            ),
            "required_action": required_action,
            "must_browse_catalog": bool(
                getattr(decision, "must_browse_catalog", False),
            ),
        }
    except Exception:  # noqa: BLE001
        return {}


def graph_state_event_payload(
    state: GraphState,
    *,
    phase: str,
) -> dict[str, Any]:
    snap = state.snapshot()
    return {
        "plan_id": f"turn:{state.run_id}",
        "goal_id": state.thread_id,
        "run_id": state.run_id,
        "phase": phase,
        "step_id": "turn",
        "step_index": 0,
        "final": snap.get("final"),
        "subtasks": len(snap.get("subtasks") or []),
        "tool_results": len(snap.get("tool_results") or []),
        "memory_hits": len(snap.get("memory_hits") or []),
        "artifacts": len(snap.get("artifacts") or []),
        "errors": len(snap.get("errors") or []),
        "metadata": dict(snap.get("metadata") or {}),
    }


def _recent_artifacts(
    artifact_store: Any | None,
    *,
    session_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if artifact_store is None:
        return []
    try:
        rows = artifact_store.list_recent(session_id=session_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        out.append({
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "artifact_type": row.get("artifact_type", "file"),
            "path": row.get("path", ""),
            "url": row.get("url", ""),
            "target_drive": row.get("target_drive", ""),
            "tool_name": row.get("tool_name", ""),
            "created_at": row.get("created_at", 0.0),
        })
    return out


__all__ = [
    "build_turn_graph_state",
    "graph_state_event_payload",
]
