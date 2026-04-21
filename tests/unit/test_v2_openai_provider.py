"""OpenAILLM — pure-transform and faked-client unit tests.

Same shape as test_v2_anthropic_provider.py — tests live offline; the
one ``complete()`` test exercises the full extraction path against a
fake ``AsyncOpenAI`` response.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import Message
from xmclaw.providers.llm.openai import OpenAILLM


# ── pure transforms ───────────────────────────────────────────────────────

def test_messages_to_openai_keeps_system_inline() -> None:
    """OpenAI convention: system prompt stays in the messages array
    (unlike Anthropic which moves it out)."""
    msgs = OpenAILLM._messages_to_openai([
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
    ])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "you are a helper"


def test_messages_to_openai_emits_tool_calls_on_assistant() -> None:
    tc = ToolCall(name="foo", args={"k": 1}, provenance="synthetic", id="call-1")
    msgs = [Message(role="assistant", content="", tool_calls=(tc,))]
    out = OpenAILLM._messages_to_openai(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" in out[0]
    assert out[0]["tool_calls"][0]["function"]["name"] == "foo"
    # arguments round-trips as JSON string
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"k": 1}


def test_messages_to_openai_emits_tool_result_with_tool_call_id() -> None:
    msgs = [Message(role="tool", content="42", tool_call_id="call-1")]
    out = OpenAILLM._messages_to_openai(msgs)
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call-1"
    assert out[0]["content"] == "42"


def test_tools_to_openai_format() -> None:
    specs = [
        ToolSpec(name="read", description="read a file",
                 parameters_schema={"type": "object", "properties": {"p": {"type": "string"}}}),
    ]
    out = OpenAILLM._tools_to_openai(specs)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "read"
    assert "parameters" in out[0]["function"]


def test_empty_tools() -> None:
    assert OpenAILLM._tools_to_openai(None) == []
    assert OpenAILLM._tools_to_openai([]) == []


# ── properties ────────────────────────────────────────────────────────────

def test_tool_call_shape_is_openai_tool() -> None:
    assert OpenAILLM(api_key="x").tool_call_shape == ToolCallShape.OPENAI_TOOL


def test_default_pricing_is_non_zero_for_openai_proper() -> None:
    p = OpenAILLM(api_key="x").pricing
    assert p.input_per_mtok > 0
    assert p.output_per_mtok > 0


def test_base_url_stored_for_compat_endpoints() -> None:
    llm = OpenAILLM(api_key="x", base_url="https://compat.example/v1")
    assert llm.base_url == "https://compat.example/v1"


# ── complete() against a faked client ─────────────────────────────────────

@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    type: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str
    tool_calls: list | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    choices: list
    usage: _FakeUsage


class _FakeChatCompletionsAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> _FakeResponse:  # noqa: ARG002
        return self._response


class _FakeChatAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeChatCompletionsAPI(response)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.chat = _FakeChatAPI(response)


@pytest.mark.asyncio
async def test_complete_parses_text_and_tool_calls() -> None:
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="Here is the summary.",
                tool_calls=[_FakeToolCall(
                    id="call_1",
                    type="function",
                    function=_FakeFunction(
                        name="file_read",
                        arguments=json.dumps({"path": "/tmp/x"}),
                    ),
                )],
            ),
        )],
        usage=_FakeUsage(prompt_tokens=42, completion_tokens=17),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)

    resp = await llm.complete([Message(role="user", content="summarize")])
    assert resp.content == "Here is the summary."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    assert resp.prompt_tokens == 42
    assert resp.completion_tokens == 17
    assert resp.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_complete_with_no_tool_calls() -> None:
    fake = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content="just text"))],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == "just text"
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_drops_malformed_tool_call() -> None:
    """Anti-req #1: malformed arguments → translator returns None → dropped."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="",
                tool_calls=[_FakeToolCall(
                    id="call_x",
                    type="function",
                    function=_FakeFunction(name="f", arguments="{not json"),
                )],
            ),
        )],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_handles_empty_choices() -> None:
    fake = _FakeResponse(choices=[], usage=_FakeUsage(prompt_tokens=0, completion_tokens=0))
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == ""
    assert resp.tool_calls == ()
