"""B-245: pin Anthropic prompt-cache wiring.

Anthropic's prompt cache requires explicit ``cache_control: {"type":
"ephemeral"}`` markers. Pre-B-245 the provider sent ``system`` as a
plain string (no cache marker) and ``tools`` without any cache_control,
leaving the 90% cost discount on the table for the static system
prompt + tool schema. These tests verify the marker placement at the
shape-conversion layer so a refactor doesn't accidentally drop it.
"""
from __future__ import annotations

from xmclaw.core.ir import ToolSpec
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import LLMResponse, Message


def _spec(name: str = "bash") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters_schema={"type": "object", "properties": {}},
    )


# ── system field ────────────────────────────────────────────────────


def test_system_emitted_as_blocks_with_cache_control() -> None:
    """B-245 core: system prompt becomes a single text block carrying
    ``cache_control: ephemeral``. The Anthropic SDK accepts both
    string and block-list shapes for ``system``; we always send blocks
    when caching is desired."""
    msgs = [
        Message(role="system", content="You are XMclaw."),
        Message(role="user", content="hi"),
    ]
    system, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "You are XMclaw."
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # User msg passes through unchanged.
    assert converted == [{"role": "user", "content": "hi"}]


def test_empty_system_returns_empty_string() -> None:
    """No system content → return ``""`` (caller checks ``if system``
    and omits the param). Anthropic SDK rejects an empty ``system``
    block list."""
    msgs = [Message(role="user", content="hi")]
    system, _ = AnthropicLLM._messages_to_anthropic(msgs)
    assert system == ""


def test_multiple_system_messages_concat_into_one_block() -> None:
    """Multiple system messages get joined; cache_control still on the
    single resulting block."""
    msgs = [
        Message(role="system", content="part 1"),
        Message(role="system", content="part 2"),
        Message(role="user", content="hi"),
    ]
    system, _ = AnthropicLLM._messages_to_anthropic(msgs)
    assert isinstance(system, list)
    assert system[0]["text"] == "part 1\n\npart 2"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


# ── tools field ─────────────────────────────────────────────────────


def test_tools_last_entry_carries_cache_control() -> None:
    """Tools array gets cache_control on the LAST entry — sets a single
    breakpoint that caches the entire tool list."""
    tools = [_spec("bash"), _spec("file_read"), _spec("web_fetch")]
    out = AnthropicLLM._tools_to_anthropic(tools)
    assert len(out) == 3
    # Earlier tools don't have cache_control.
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    # Last tool does.
    assert out[2]["cache_control"] == {"type": "ephemeral"}


def test_empty_tools_returns_empty_list() -> None:
    """No tools → no breakpoint to set."""
    assert AnthropicLLM._tools_to_anthropic(None) == []
    assert AnthropicLLM._tools_to_anthropic([]) == []


def test_single_tool_carries_cache_control() -> None:
    """Single-tool case: that one tool IS the last → carries marker."""
    out = AnthropicLLM._tools_to_anthropic([_spec("bash")])
    assert out[0]["cache_control"] == {"type": "ephemeral"}


# ── LLMResponse cache stat fields ──────────────────────────────────


def test_llm_response_cache_stat_fields_default_zero() -> None:
    """Cache stats default to 0 (no caching used / provider didn't
    report). Backwards-compatible with all existing call sites."""
    r = LLMResponse(content="hi")
    assert r.cache_creation_input_tokens == 0
    assert r.cache_read_input_tokens == 0


def test_llm_response_cache_stats_preserved() -> None:
    r = LLMResponse(
        content="hi",
        cache_creation_input_tokens=3500,
        cache_read_input_tokens=12000,
    )
    assert r.cache_creation_input_tokens == 3500
    assert r.cache_read_input_tokens == 12000
