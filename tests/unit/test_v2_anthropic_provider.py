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
