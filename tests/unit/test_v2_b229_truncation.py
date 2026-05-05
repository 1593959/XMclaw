"""B-229: stream truncation handling — empty-args tool call repro.

The ``code_python({})`` ghost-call regression: when the LLM stream is
cut off by ``max_tokens`` BEFORE any tool-call argument chunk arrives,
the OpenAI tool_acc accumulator stores ``arguments=""`` (empty string,
the initial state). Pre-B-229 the translator parsed that as
``args={}`` and the agent loop dispatched the malformed call.

These tests pin the fix:
  * Translator: empty-string raw_args → ``None`` (drop the call)
  * Translator: legitimate ``"{}"`` raw_args → ToolCall with args={}
  * LLMResponse: ``stop_reason`` round-trips through both providers
"""
from __future__ import annotations

from xmclaw.providers.llm.base import LLMResponse
from xmclaw.providers.llm.translators import openai_tool_shape


def test_empty_string_args_returns_none() -> None:
    """B-229 core: streaming-truncated tool call has arguments='' which
    used to parse as args={} but now correctly drops the call."""
    item = {
        "id": "call_abc123",
        "type": "function",
        "function": {"name": "code_python", "arguments": ""},
    }
    assert openai_tool_shape.decode_from_provider(item) is None


def test_empty_dict_string_args_returns_call_with_empty_args() -> None:
    """A model legitimately calling a no-args tool serialises arguments
    as the literal JSON string ``"{}"`` — that should NOT be dropped."""
    item = {
        "id": "call_xyz",
        "type": "function",
        "function": {"name": "agent_status", "arguments": "{}"},
    }
    tc = openai_tool_shape.decode_from_provider(item)
    assert tc is not None
    assert tc.name == "agent_status"
    assert tc.args == {}
    assert tc.id == "call_xyz"


def test_dict_args_passes_through() -> None:
    """Some compat shims send ``arguments`` as a dict directly — accept
    that path too (existing behavior)."""
    item = {
        "id": "call_q",
        "type": "function",
        "function": {"name": "bash", "arguments": {"command": "ls"}},
    }
    tc = openai_tool_shape.decode_from_provider(item)
    assert tc is not None
    assert tc.args == {"command": "ls"}


def test_malformed_json_args_returns_none() -> None:
    """Anti-req #1 still holds: bad JSON → drop, no soft-parse."""
    item = {
        "id": "call_q",
        "type": "function",
        "function": {"name": "bash", "arguments": '{"command": '},
    }
    assert openai_tool_shape.decode_from_provider(item) is None


def test_real_args_parse_normally() -> None:
    item = {
        "id": "call_real",
        "type": "function",
        "function": {
            "name": "file_read",
            "arguments": '{"path": "/tmp/x.py", "offset": 1}',
        },
    }
    tc = openai_tool_shape.decode_from_provider(item)
    assert tc is not None
    assert tc.name == "file_read"
    assert tc.args == {"path": "/tmp/x.py", "offset": 1}


def test_llm_response_default_stop_reason() -> None:
    """LLMResponse defaults stop_reason to '' for backward compat."""
    r = LLMResponse(content="hi")
    assert r.stop_reason == ""


def test_llm_response_carries_stop_reason() -> None:
    r = LLMResponse(content="hi", stop_reason="max_tokens")
    assert r.stop_reason == "max_tokens"


def test_llm_response_remains_frozen() -> None:
    """LLMResponse is frozen — mutation raises (the existing contract)."""
    r = LLMResponse(content="hi")
    import dataclasses
    try:
        r.content = "bye"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("LLMResponse should be frozen")
