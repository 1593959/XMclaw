"""Runtime task context injection.

This is not memory. It is the current task's execution state: recent
artifacts and recent strategy-switch signals. The block is injected into
the current turn so the agent can continue from concrete state instead of
guessing or re-searching blindly.
"""
from __future__ import annotations

from typing import Any

from xmclaw.core.bus.events import EventType


def build_task_runtime_context(
    *,
    session_id: str,
    artifact_store: Any | None = None,
    bus: Any | None = None,
    artifact_limit: int = 5,
    strategy_limit: int = 3,
) -> str:
    artifacts = _recent_artifacts(
        artifact_store,
        session_id=session_id,
        limit=artifact_limit,
    )
    strategy_events = _recent_strategy_events(
        bus,
        session_id=session_id,
        limit=strategy_limit,
    )
    if not artifacts and not strategy_events:
        return ""

    lines = [
        "<task-runtime-context>",
        "[System note: This is current task execution state, not user input "
        "and not long-term memory. Use it to continue work without "
        "re-searching blindly.]",
    ]
    if artifacts:
        lines.append("")
        lines.append("Recent artifacts:")
        for idx, item in enumerate(artifacts, 1):
            name = str(item.get("name") or item.get("path") or item.get("url") or "artifact")
            kind = str(item.get("artifact_type") or "file")
            tool = str(item.get("tool_name") or "tool")
            path = str(item.get("path") or "")
            url = str(item.get("url") or "")
            drive = str(item.get("target_drive") or "")
            loc = path or url
            bits = [f"{idx}. [{kind}] {name}", f"tool={tool}"]
            if drive:
                bits.append(f"drive={drive}")
            if loc:
                bits.append(f"location={loc}")
            lines.append(" | ".join(bits))
    if strategy_events:
        lines.append("")
        lines.append("Recent strategy switch signals:")
        for idx, item in enumerate(strategy_events, 1):
            payload = item.get("payload") or {}
            kind = str(payload.get("kind") or "failure")
            decision = str(payload.get("strategy_decision") or "change_plan")
            retry = payload.get("should_retry_same")
            action = str(payload.get("recommended_action") or payload.get("message") or "")
            tool = payload.get("tool")
            err = str(payload.get("error_signature") or "")
            bits = [
                f"{idx}. kind={kind}",
                f"decision={decision}",
                f"retry_same={bool(retry) if retry is not None else 'unknown'}",
            ]
            if tool:
                bits.append(f"tool={tool}")
            if err:
                bits.append(f"error={err[:180]}")
            if action:
                bits.append(f"action={action[:240]}")
            lines.append(" | ".join(str(b) for b in bits))
    lines.append("</task-runtime-context>")
    return "\n".join(lines)


def _recent_artifacts(
    artifact_store: Any | None,
    *,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if artifact_store is None:
        return []
    try:
        rows = artifact_store.list_recent(session_id=session_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _recent_strategy_events(
    bus: Any | None,
    *,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    query = getattr(bus, "query", None)
    if not callable(query):
        return []
    try:
        events = query(
            session_id=session_id,
            types=[EventType.ANTI_REQ_VIOLATION],
            limit=50,
        )
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for event in reversed(events):
        payload = getattr(event, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        if (
            not payload.get("strategy_decision")
            and payload.get("kind") not in {"stuck_loop", "no_progress", "tool_review"}
        ):
            continue
        out.append({
            "id": getattr(event, "id", ""),
            "ts": getattr(event, "ts", 0.0),
            "payload": dict(payload),
        })
        if len(out) >= limit:
            break
    return out


__all__ = ["build_task_runtime_context"]
