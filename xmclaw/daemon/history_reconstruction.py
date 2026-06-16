"""Rebuild a session's LLM message history from the durable event log.

Unlike the legacy ``_reconstruct_history_from_events`` which only recovered
user + assistant text, this module restores full tool-use pairs:

- ``llm_response`` → assistant message (with content + thinking)
- ``tool_call_emitted`` → ``ToolCall`` attached to the parent assistant
- ``tool_invocation_finished`` → ``role="tool"`` result message

The result can be fed straight back into ``AgentLoop`` as ``prior`` history,
so a crashed or hard-killed session resumes from the last completed hop
instead of restarting from scratch.
"""
from __future__ import annotations

from typing import Any

from xmclaw.core.bus.events import EventType
from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message


def _event_type_value(event: Any) -> str:
    t = getattr(event, "type", None)
    if t is None:
        return ""
    return t.value if hasattr(t, "value") else str(t)


def _payload(event: Any) -> dict[str, Any]:
    return getattr(event, "payload", {}) or {}


def _make_tool_call(payload: dict[str, Any]) -> ToolCall:
    """Build a ToolCall from a TOOL_CALL_EMITTED payload."""
    return ToolCall(
        name=payload.get("name") or "unknown",
        args=payload.get("args") or {},
        provenance=payload.get("provenance") or "synthetic",
        id=payload.get("call_id") or "",
        raw_snippet=None,
        session_id=None,
    )


def _make_tool_result_message(payload: dict[str, Any]) -> Message:
    """Build a role=tool message from a TOOL_INVOCATION_FINISHED payload."""
    ok = payload.get("ok", True)
    if ok:
        content = payload.get("result", "")
        if not isinstance(content, str):
            content = str(content)
    else:
        err = payload.get("error") or "tool failed without an error message"
        if isinstance(err, str) and err.startswith("NEEDS_APPROVAL:"):
            content = err
        else:
            content = f"ERROR: {err}"
    return Message(
        role="tool",
        content=content,
        tool_call_id=payload.get("call_id") or "",
    )


def _sanitize_tool_pairs(messages: list[Message]) -> list[Message]:
    """Ensure every tool_call has a matching result and vice-versa.

    Mirrors ``ContextCompressor._sanitize_tool_pairs`` but as a pure
    function so the reconstruction module doesn't need to import the
    compressor.
    """
    surviving_call_ids: set[str] = set()
    for m in messages:
        if m.role == "assistant":
            for tc in m.tool_calls or ():
                cid = getattr(tc, "id", "") or ""
                if cid:
                    surviving_call_ids.add(cid)

    result_call_ids: set[str] = set()
    for m in messages:
        if m.role == "tool":
            cid = m.tool_call_id or ""
            if cid:
                result_call_ids.add(cid)

    # Drop orphaned results.
    orphaned = result_call_ids - surviving_call_ids
    if orphaned:
        messages = [
            m for m in messages
            if not (m.role == "tool" and (m.tool_call_id or "") in orphaned)
        ]

    # Add stub results for orphaned calls.
    missing = surviving_call_ids - result_call_ids
    if missing:
        patched: list[Message] = []
        for m in messages:
            patched.append(m)
            if m.role == "assistant":
                for tc in m.tool_calls or ():
                    cid = getattr(tc, "id", "") or ""
                    if cid in missing:
                        patched.append(Message(
                            role="tool",
                            content=(
                                "[interrupted] 该工具调用未完成"
                                "（会话恢复时缺少结果）。"
                            ),
                            tool_call_id=cid,
                        ))
        messages = patched

    return messages


def reconstruct_messages_from_events(
    events: list[Any],
    *,
    tail_limit: int = 120,
) -> list[Message]:
    """Reconstruct a ``list[Message]`` from a chronological event list.

    Args:
        events: BehavioralEvent objects sorted oldest → newest.
        tail_limit: max messages to keep from the tail. The cut is
            aligned so tool-call / result pairs aren't split.

    Returns:
        A sanitized message list ready to use as conversation history.
    """
    out: list[Message] = []

    # Buffers for the current hop.
    thinking_buffer: str = ""
    pending_calls: dict[str, ToolCall] = {}
    current_response: dict[str, Any] | None = None
    current_tool_call_ids: list[str] = []
    response_finalized: bool = True

    def _flush_response(force: bool = False) -> None:
        """Append the in-progress assistant message + any results.

        Called when we see the first TOOL_INVOCATION_FINISHED for a hop,
        or at the very end of the event stream. ``force=True`` means we
        finalize even if no result arrived (crashed mid-tool).
        """
        nonlocal current_response, current_tool_call_ids, response_finalized
        if current_response is None:
            response_finalized = True
            return

        # Gather tool calls that belong to this response. Include both
        # calls seen before the LLM_RESPONSE and any emitted after it.
        tool_calls = [
            pending_calls[cid]
            for cid in current_tool_call_ids
            if cid in pending_calls
        ]

        out.append(Message(
            role="assistant",
            content=current_response.get("content", ""),
            tool_calls=tuple(tool_calls),
            thinking=current_response.get("thinking", ""),
        ))

        # Remove the calls we just consumed so they don't leak into a
        # future response if their results never arrive.
        for cid in current_tool_call_ids:
            pending_calls.pop(cid, None)

        current_response = None
        current_tool_call_ids = []
        response_finalized = True

    def _maybe_finalize_response() -> None:
        """If a response was buffered but not yet finalized, finalize it."""
        if current_response is not None and not response_finalized:
            _flush_response(force=True)

    for event in events:
        et = _event_type_value(event)
        payload = _payload(event)

        if et == EventType.USER_MESSAGE.value:
            channel = payload.get("channel")
            if channel == "steering":
                continue
            content = payload.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            _maybe_finalize_response()
            out.append(Message(role="user", content=content))
            continue

        if et == EventType.LLM_THINKING_CHUNK.value:
            delta = payload.get("delta") or ""
            thinking_buffer += delta
            continue

        if et == EventType.TOOL_CALL_EMITTED.value:
            tc = _make_tool_call(payload)
            if tc.id:
                pending_calls[tc.id] = tc
                if current_response is not None:
                    current_tool_call_ids.append(tc.id)
            continue

        if et == EventType.TOOL_INVOCATION_FINISHED.value:
            call_id = payload.get("call_id") or ""
            # First result for this hop means the assistant message is now
            # complete (all tool calls have been emitted).
            if current_response is not None and not response_finalized:
                _flush_response()
                response_finalized = True
                current_response = None
            out.append(_make_tool_result_message(payload))
            # Remove from pending so it isn't double-attached later.
            pending_calls.pop(call_id, None)
            continue

        if et == EventType.LLM_RESPONSE.value:
            content = payload.get("text") or payload.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            error = payload.get("error") or ""
            ok = payload.get("ok", True)
            if not content and error:
                content = f"ERROR: {error}"

            _maybe_finalize_response()

            current_response = {
                "content": content,
                "thinking": thinking_buffer,
            }
            thinking_buffer = ""
            # Any tool calls already emitted belong to this response.
            current_tool_call_ids = list(pending_calls.keys())
            response_finalized = False
            continue

    # End of stream: if a response never got any result, flush it anyway
    # so the next turn sees the assistant tool_call and can add stubs.
    if current_response is not None:
        _flush_response(force=True)

    out = _sanitize_tool_pairs(out)

    # Keep the tail, aligned to message boundaries so we don't split a
    # tool-use group.
    if len(out) > tail_limit:
        out = out[-tail_limit:]
        # If the first kept message is an orphaned tool result, slide
        # forward to the parent assistant.
        start = 0
        while start < len(out) and out[start].role == "tool":
            start += 1
        if start > 0:
            out = out[start:]

    return out


def reconstruct_history_from_event_bus(
    session_id: str,
    *,
    bus: Any | None = None,
    db_path: str | None = None,
    event_limit: int = 5000,
    tail_limit: int = 120,
) -> list[Message]:
    """Convenience wrapper that queries ``SqliteEventBus`` then reconstructs.

    Exactly one of ``bus`` or ``db_path`` should be supplied. If ``bus``
    is given it is used directly and left open; if ``db_path`` is given
    a temporary connection is opened and closed.
    """
    if bus is None:
        from xmclaw.core.bus.sqlite import SqliteEventBus
        bus = SqliteEventBus(db_path)
        try:
            events = bus.query(session_id=session_id, limit=event_limit)
        finally:
            bus.close()
    else:
        events = bus.query(session_id=session_id, limit=event_limit)

    # Events from SqliteEventBus.query are already sorted oldest→newest,
    # but be defensive.
    events = sorted(events, key=lambda e: (getattr(e, "ts", 0) or 0, getattr(e, "id", "") or ""))
    return reconstruct_messages_from_events(events, tail_limit=tail_limit)
