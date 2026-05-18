"""AnthropicLLM — pure-transform and lazy-client unit tests.

These tests don't hit the network. The SDK client is monkey-patched for
the one test that exercises ``complete`` end-to-end on a faked response.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import Message


# ── pure transforms ────────────────────────────────────────────────────────

def test_messages_to_anthropic_splits_system() -> None:
    """B-245 update: system is now emitted as a list of content blocks
    with cache_control on the single text block (was: plain string).
    Anthropic SDK accepts both shapes; we always send blocks now to
    enable prompt caching."""
    system, msgs = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
    ])
    assert isinstance(system, list)
    assert system[0]["text"] == "you are a helper"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_messages_to_anthropic_emits_tool_use_blocks() -> None:
    tc = ToolCall(name="foo", args={"k": 1}, provenance="synthetic", id="tc-1")
    msgs = [
        Message(role="assistant", content="let me check", tool_calls=(tc,)),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    blocks = converted[0]["content"]
    assert isinstance(blocks, list)
    assert any(
        b["type"] == "tool_use" and b["name"] == "foo" and b["id"] == "tc-1"
        for b in blocks
    )


def test_messages_to_anthropic_emits_tool_result() -> None:
    msgs = [
        Message(role="tool", content="42", tool_call_id="tc-1"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert converted[0]["role"] == "user"
    assert converted[0]["content"][0]["type"] == "tool_result"
    assert converted[0]["content"][0]["tool_use_id"] == "tc-1"


def test_tools_to_anthropic_format() -> None:
    specs = [
        ToolSpec(name="read", description="read a file",
                 parameters_schema={"type": "object", "properties": {"p": {"type": "string"}}}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    assert out[0]["name"] == "read"
    assert "input_schema" in out[0]
    assert out[0]["input_schema"]["type"] == "object"


def test_empty_tools_emits_empty_list() -> None:
    assert AnthropicLLM._tools_to_anthropic(None) == []
    assert AnthropicLLM._tools_to_anthropic([]) == []


def test_system_cache_breakpoint_marker_splits_into_blocks() -> None:
    """Wave-30 prompt-cache fix: the CACHE_BREAKPOINT_MARKER sentinel
    inside ``Message(role="system").content`` splits into independent
    text blocks. Every block EXCEPT the last gets cache_control —
    the trailing mutable tail (time block) stays out of the cached
    prefix so it doesn't invalidate the cache every turn."""
    from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER

    sys_text = (
        "you are a helper"
        + f"\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        + "## What I remember about you\n* name: He"
        + f"\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        + "## 当前时刻\n\n2026-05-18 02:30:00"
    )
    system, _ = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content=sys_text),
        Message(role="user", content="hi"),
    ])
    # Three blocks because we put two markers in.
    assert isinstance(system, list)
    assert len(system) == 3
    # First two are cacheable, last (mutable time tail) is not.
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[2]
    # Content order preserved + marker stripped.
    assert "you are a helper" in system[0]["text"]
    assert "What I remember" in system[1]["text"]
    assert "当前时刻" in system[2]["text"]
    for block in system:
        assert CACHE_BREAKPOINT_MARKER not in block["text"]


def test_tools_cache_breakpoint_skips_prefilter_skills() -> None:
    """Wave-30 prompt-cache fix: the tools-array cache_control marker
    moves to the LAST STABLE tool (the one just before the first
    ``skill_*``) so the per-turn prefilter output doesn't invalidate
    the cache every turn. Stable tools (bash, file_read, etc.) live
    at indices [0..N); skills at [N..end)."""
    specs = [
        ToolSpec(name="file_read", description="read",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="bash", description="run",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="skill_git-commit", description="commit",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="skill_review", description="review",
                 parameters_schema={"type": "object"}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    # Breakpoint should be on ``bash`` (last stable), NOT on the
    # final ``skill_review`` (which is per-turn volatile).
    assert out[1]["name"] == "bash"
    assert out[1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in out[3]  # skill_review — no marker


def test_tools_cache_breakpoint_falls_back_to_last_when_no_skills() -> None:
    """When the tool list has no ``skill_*`` entries (small setup
    below the B-238 prefilter threshold) the breakpoint stays on
    the last tool — preserves the pre-Wave-30 behaviour."""
    specs = [
        ToolSpec(name="file_read", description="read",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="bash", description="run",
                 parameters_schema={"type": "object"}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    assert out[-1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in out[0]


def test_history_cache_breakpoint_marks_last_message() -> None:
    """Wave-30 follow-up (2026-05-18): the 4th cache breakpoint goes
    on the LAST message so prior history is cached too. Pre-fix a
    multi-turn chat re-billed ~28K tokens of prior history on every
    LLM call because Anthropic does NOT auto-cache messages — only
    positions explicitly marked with cache_control. Verified
    empirically against Kimi K2.6 (Anthropic-compat): a 2611-token
    conversation now bills 0 fresh input + 2611 cache_read on the
    second call within the cache window."""
    msgs = [
        Message(role="system", content="be terse"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="continue"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert len(converted) == 3
    # First two messages keep plain-string content (anti-req #11
    # non-interference — match what a naked SDK caller would send).
    assert converted[0]["content"] == "hi"
    assert converted[1]["content"] == "hello"
    # Last message's content is now a block list with cache_control.
    last = converted[-1]
    assert isinstance(last["content"], list)
    assert last["content"][0]["text"] == "continue"
    assert last["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_history_cache_breakpoint_tags_existing_block_content() -> None:
    """When the last message already has block-shape content (tool_use
    or image attachment), tag the trailing block in place rather than
    wrapping anew."""
    tc = ToolCall(name="read", args={}, provenance="synthetic", id="t1")
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="checking", tool_calls=(tc,)),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    last = converted[-1]
    assert isinstance(last["content"], list)
    # text block first, tool_use second → tool_use is the trailing
    # block and gets the cache_control marker.
    assert last["content"][-1]["type"] == "tool_use"
    assert last["content"][-1].get("cache_control") == {"type": "ephemeral"}


def test_history_cache_breakpoint_skips_empty_messages() -> None:
    """If the message list is empty, no marker work. (Pre-fix would
    have IndexError'd on converted[-1].)"""
    _, converted = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content="x"),
    ])
    assert converted == []


# ── properties ────────────────────────────────────────────────────────────

def test_tool_call_shape_is_anthropic_native() -> None:
    llm = AnthropicLLM(api_key="x")
    assert llm.tool_call_shape == ToolCallShape.ANTHROPIC_NATIVE


def test_default_pricing_non_zero() -> None:
    llm = AnthropicLLM(api_key="x")
    assert llm.pricing.input_per_mtok > 0
    assert llm.pricing.output_per_mtok > 0


# ── complete() with a faked client ────────────────────────────────────────

@dataclass
class _FakeBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage


class _FakeMessagesAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs):  # noqa: ANN003, ARG002
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessagesAPI(response)


@pytest.mark.asyncio
async def test_complete_parses_text_and_tool_use_blocks() -> None:
    fake_response = _FakeResponse(
        content=[
            _FakeBlock(type="text", text="Here is the summary."),
            _FakeBlock(type="tool_use", id="toolu_1", name="file_read",
                       input={"path": "/tmp/x"}),
        ],
        usage=_FakeUsage(input_tokens=42, output_tokens=17),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)

    resp = await llm.complete([Message(role="user", content="summarize X")])
    assert resp.content == "Here is the summary."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    assert resp.prompt_tokens == 42
    assert resp.completion_tokens == 17
    assert resp.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_complete_with_no_tool_calls() -> None:
    fake_response = _FakeResponse(
        content=[_FakeBlock(type="text", text="plain text")],
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == "plain text"
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_rejects_malformed_tool_use_block() -> None:
    """Anti-req #1: a malformed tool_use block does NOT produce a ToolCall."""
    fake_response = _FakeResponse(
        content=[
            # Missing 'input' as dict — translator returns None, we drop it.
            _FakeBlock(type="tool_use", id="t", name="foo", input="not-a-dict"),
        ],
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)
    resp = await llm.complete([Message(role="user", content="x")])
    # No tool calls — malformed block silently dropped (translator returned None).
    assert resp.tool_calls == ()
