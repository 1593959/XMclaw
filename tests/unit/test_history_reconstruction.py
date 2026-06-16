"""Unit tests for xmclaw.daemon.history_reconstruction."""
from __future__ import annotations

import pytest

from xmclaw.core.bus.events import EventType
from xmclaw.core.ir import ToolCall
from xmclaw.daemon.history_reconstruction import reconstruct_messages_from_events
from xmclaw.providers.llm.base import Message


class _E:
    def __init__(self, type_: EventType, payload: dict):
        self.type = type_
        self.payload = payload


def test_empty_events() -> None:
    assert reconstruct_messages_from_events([]) == []


def test_user_and_assistant_text() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "hi", "channel": "agent_loop"}),
        _E(EventType.LLM_RESPONSE, {"content": "hello", "ok": True}),
    ]
    msgs = reconstruct_messages_from_events(events)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "hello"


def test_steering_user_message_ignored() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "go left", "channel": "steering"}),
        _E(EventType.USER_MESSAGE, {"content": "hi", "channel": "agent_loop"}),
        _E(EventType.LLM_RESPONSE, {"content": "hello", "ok": True}),
    ]
    msgs = reconstruct_messages_from_events(events)
    assert [m.role for m in msgs] == ["user", "assistant"]


def test_tool_use_pair_reconstructed() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "read it", "channel": "agent_loop"}),
        _E(EventType.TOOL_CALL_EMITTED, {
            "call_id": "c1",
            "name": "file_read",
            "args": {"path": "x.txt"},
            "provenance": "openai",
        }),
        _E(EventType.LLM_RESPONSE, {"content": "", "ok": True}),
        _E(EventType.TOOL_INVOCATION_FINISHED, {
            "call_id": "c1",
            "name": "file_read",
            "ok": True,
            "result": "contents",
        }),
        _E(EventType.LLM_RESPONSE, {"content": "done", "ok": True}),
    ]
    msgs = reconstruct_messages_from_events(events)
    assert [m.role for m in msgs] == [
        "user", "assistant", "tool", "assistant",
    ]
    assert msgs[1].tool_calls[0].name == "file_read"
    assert msgs[1].tool_calls[0].id == "c1"
    assert msgs[2].role == "tool"
    assert msgs[2].tool_call_id == "c1"
    assert msgs[2].content == "contents"


def test_thinking_chunk_aggregated() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "solve", "channel": "agent_loop"}),
        _E(EventType.LLM_THINKING_CHUNK, {"delta": "let me think"}),
        _E(EventType.LLM_THINKING_CHUNK, {"delta": "..."}),
        _E(EventType.LLM_RESPONSE, {"content": "42", "ok": True}),
    ]
    msgs = reconstruct_messages_from_events(events)
    assert msgs[1].thinking == "let me think..."


def test_crashed_mid_tool_gets_stub_result() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "run", "channel": "agent_loop"}),
        _E(EventType.TOOL_CALL_EMITTED, {
            "call_id": "c1", "name": "bash", "args": {"command": "ls"},
            "provenance": "openai",
        }),
        _E(EventType.LLM_RESPONSE, {"content": "", "ok": True}),
        # No TOOL_INVOCATION_FINISHED — daemon crashed.
    ]
    msgs = reconstruct_messages_from_events(events)
    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1].tool_calls[0].id == "c1"
    assert msgs[2].tool_call_id == "c1"
    assert "interrupted" in msgs[2].content


def test_failed_tool_result_becomes_error() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "run", "channel": "agent_loop"}),
        _E(EventType.LLM_RESPONSE, {"content": "", "ok": True}),
        _E(EventType.TOOL_CALL_EMITTED, {
            "call_id": "c1", "name": "bash", "args": {"command": "ls"},
            "provenance": "openai",
        }),
        _E(EventType.TOOL_INVOCATION_FINISHED, {
            "call_id": "c1", "name": "bash", "ok": False,
            "error": "permission denied",
        }),
    ]
    msgs = reconstruct_messages_from_events(events)
    assert msgs[2].role == "tool"
    assert msgs[2].content.startswith("ERROR:")


def test_tail_limit_does_not_split_tool_group() -> None:
    events = [
        _E(EventType.USER_MESSAGE, {"content": "first", "channel": "agent_loop"}),
        _E(EventType.LLM_RESPONSE, {"content": "ok", "ok": True}),
        _E(EventType.USER_MESSAGE, {"content": "second", "channel": "agent_loop"}),
        _E(EventType.TOOL_CALL_EMITTED, {
            "call_id": "c1", "name": "bash", "args": {}, "provenance": "openai",
        }),
        _E(EventType.LLM_RESPONSE, {"content": "", "ok": True}),
        _E(EventType.TOOL_INVOCATION_FINISHED, {
            "call_id": "c1", "name": "bash", "ok": True, "result": "out",
        }),
    ]
    msgs = reconstruct_messages_from_events(events, tail_limit=2)
    # The tail must start at the assistant that emitted the tool call,
    # not the orphaned tool result.
    assert [m.role for m in msgs] == ["assistant", "tool"]
