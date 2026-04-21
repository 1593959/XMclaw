"""Anti-req #11 — provider non-interference (CI-6 source of truth).

The claim: the same model used through XMclaw v2 must not produce worse
output than the same model used through a naked vendor SDK. Since our
providers are deliberately thin wrappers (no prompt decoration, no
hidden temperature, no system-prompt injection), the claim reduces to
a mechanical check:

    For every (messages, tools) input, v2 sends the SAME API call body
    that a naked SDK user would have sent.

Any future regression that adds a hidden parameter (e.g. ``temperature
= 0.3``), injects a system prefix, or mutates the user's tool schema
will flip these tests to red before it reaches production.

Stochastic live comparison (temperature=0, compare outputs) lives in
``tests/bench/phase2_same_model_live.py`` once we wire it in; this file
is the deterministic ci-blocking version.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolSpec
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import Message
from xmclaw.providers.llm.openai import OpenAILLM


# ── test double: captures what .create() was called with ──────────────────

@dataclass
class _CapturedCall:
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _AnthFakeBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _AnthFakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _AnthFakeResponse:
    content: list = field(default_factory=list)
    usage: _AnthFakeUsage = field(default_factory=_AnthFakeUsage)


class _AnthFakeMessagesAPI:
    def __init__(self, captured: _CapturedCall) -> None:
        self._captured = captured

    async def create(self, **kwargs: Any) -> _AnthFakeResponse:
        self._captured.kwargs = kwargs
        return _AnthFakeResponse(content=[_AnthFakeBlock(type="text", text="ok")])


class _AnthFakeClient:
    def __init__(self) -> None:
        self.captured = _CapturedCall()
        self.messages = _AnthFakeMessagesAPI(self.captured)


@dataclass
class _OAIFakeMessage:
    content: str = "ok"
    tool_calls: list | None = None


@dataclass
class _OAIFakeChoice:
    message: _OAIFakeMessage = field(default_factory=_OAIFakeMessage)


@dataclass
class _OAIFakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _OAIFakeResponse:
    choices: list = field(default_factory=lambda: [_OAIFakeChoice()])
    usage: _OAIFakeUsage = field(default_factory=_OAIFakeUsage)


class _OAIFakeCompletionsAPI:
    def __init__(self, captured: _CapturedCall) -> None:
        self._captured = captured

    async def create(self, **kwargs: Any) -> _OAIFakeResponse:
        self._captured.kwargs = kwargs
        return _OAIFakeResponse()


class _OAIFakeChatAPI:
    def __init__(self, captured: _CapturedCall) -> None:
        self.completions = _OAIFakeCompletionsAPI(captured)


class _OAIFakeClient:
    def __init__(self) -> None:
        self.captured = _CapturedCall()
        self.chat = _OAIFakeChatAPI(self.captured)


# ── Anthropic: allowed top-level parameters ───────────────────────────────

_ANTH_ALLOWED_KWARGS = {"model", "messages", "max_tokens", "system", "tools"}


@pytest.mark.asyncio
async def test_anthropic_simple_user_message_sends_minimal_body() -> None:
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x", model="claude-opus-4-7")
    llm._client = client
    await llm.complete([Message(role="user", content="hello")])
    kw = client.captured.kwargs
    assert set(kw.keys()) <= _ANTH_ALLOWED_KWARGS, (
        f"Anthropic call body leaked hidden kwargs: {set(kw.keys()) - _ANTH_ALLOWED_KWARGS}"
    )
    assert kw["model"] == "claude-opus-4-7"
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    assert "system" not in kw   # no hidden system prompt
    assert "tools" not in kw    # no tools injected
    assert "temperature" not in kw  # anti-req #11: no sampling override


@pytest.mark.asyncio
async def test_anthropic_system_prompt_hoisted_verbatim() -> None:
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x")
    llm._client = client
    await llm.complete([
        Message(role="system", content="you are precise"),
        Message(role="user", content="hi"),
    ])
    kw = client.captured.kwargs
    assert kw["system"] == "you are precise"
    # messages should only contain the user turn
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_tool_schema_passthrough() -> None:
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x")
    llm._client = client
    tools = [ToolSpec(
        name="read_file",
        description="read a file's contents",
        parameters_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )]
    await llm.complete([Message(role="user", content="x")], tools=tools)
    kw = client.captured.kwargs
    assert kw["tools"] == [{
        "name": "read_file",
        "description": "read a file's contents",
        "input_schema": tools[0].parameters_schema,
    }]


@pytest.mark.asyncio
async def test_anthropic_assistant_tool_call_history_roundtrips() -> None:
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x")
    llm._client = client
    tc = ToolCall(name="f", args={"k": 1}, provenance="anthropic", id="toolu_1")
    await llm.complete([
        Message(role="user", content="do it"),
        Message(role="assistant", content="ok", tool_calls=(tc,)),
        Message(role="tool", content="42", tool_call_id="toolu_1"),
    ])
    kw = client.captured.kwargs
    # assistant turn has both text and tool_use blocks
    assistant_msg = kw["messages"][1]
    assert assistant_msg["role"] == "assistant"
    blocks = assistant_msg["content"]
    assert {"type": "text", "text": "ok"} in blocks
    assert any(
        b["type"] == "tool_use" and b["name"] == "f" and b["input"] == {"k": 1}
        for b in blocks
    )
    # tool result comes back as user-role with tool_result block
    tool_msg = kw["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "toolu_1"


# ── OpenAI: allowed top-level parameters ──────────────────────────────────

_OAI_ALLOWED_KWARGS = {"model", "messages", "tools", "stream"}


@pytest.mark.asyncio
async def test_openai_simple_user_message_sends_minimal_body() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x", model="gpt-4o")
    llm._client = client
    await llm.complete([Message(role="user", content="hello")])
    kw = client.captured.kwargs
    assert set(kw.keys()) <= _OAI_ALLOWED_KWARGS, (
        f"OpenAI call body leaked hidden kwargs: {set(kw.keys()) - _OAI_ALLOWED_KWARGS}"
    )
    assert kw["model"] == "gpt-4o"
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    assert "tools" not in kw
    assert "temperature" not in kw
    assert "top_p" not in kw
    assert "presence_penalty" not in kw
    assert "frequency_penalty" not in kw


@pytest.mark.asyncio
async def test_openai_system_stays_in_messages() -> None:
    """OpenAI convention: system is a role in messages (unlike Anthropic)."""
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x")
    llm._client = client
    await llm.complete([
        Message(role="system", content="you are precise"),
        Message(role="user", content="hi"),
    ])
    kw = client.captured.kwargs
    assert kw["messages"][0] == {"role": "system", "content": "you are precise"}
    assert kw["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_openai_tool_schema_passthrough() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x")
    llm._client = client
    tools = [ToolSpec(
        name="write_file",
        description="write text to a file",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    )]
    await llm.complete([Message(role="user", content="x")], tools=tools)
    kw = client.captured.kwargs
    assert kw["tools"] == [{
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "write text to a file",
            "parameters": tools[0].parameters_schema,
        },
    }]


@pytest.mark.asyncio
async def test_openai_assistant_tool_call_history_roundtrips() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x")
    llm._client = client
    tc = ToolCall(name="f", args={"k": 1}, provenance="openai", id="call_1")
    await llm.complete([
        Message(role="user", content="do it"),
        Message(role="assistant", content="", tool_calls=(tc,)),
        Message(role="tool", content="42", tool_call_id="call_1"),
    ])
    kw = client.captured.kwargs
    assistant_msg = kw["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "call_1"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "f"
    assert json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"]) == {"k": 1}
    tool_msg = kw["messages"][2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["content"] == "42"


# ── regression guards ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_no_system_prompt_injected_for_empty_system() -> None:
    """If the caller provided no system message, v2 must not invent one."""
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x")
    llm._client = client
    await llm.complete([Message(role="user", content="hi")])
    assert "system" not in client.captured.kwargs


@pytest.mark.asyncio
async def test_openai_no_system_prompt_injected_for_empty_system() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x")
    llm._client = client
    await llm.complete([Message(role="user", content="hi")])
    # No "system" role message should appear
    roles = [m["role"] for m in client.captured.kwargs["messages"]]
    assert "system" not in roles


@pytest.mark.asyncio
async def test_anthropic_passes_user_content_unchanged() -> None:
    """Anti-req #11: v2 MUST NOT mutate user content (no wrapping in
    "Please consider..." / no quoting / no prompt-engineering tricks)."""
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x")
    llm._client = client
    original = "What is 2+2? Be concise.\nAlso:   \t  whitespace!"
    await llm.complete([Message(role="user", content=original)])
    kw = client.captured.kwargs
    assert kw["messages"][0]["content"] == original


@pytest.mark.asyncio
async def test_openai_passes_user_content_unchanged() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x")
    llm._client = client
    original = "What is 2+2? Be concise.\nAlso:   \t  whitespace!"
    await llm.complete([Message(role="user", content=original)])
    kw = client.captured.kwargs
    assert kw["messages"][0]["content"] == original


@pytest.mark.asyncio
async def test_anthropic_model_string_not_normalized() -> None:
    """Regression guard: if someone adds a "model_alias_map" that translates
    model names, this test fails — forcing an explicit conversation about
    whether the alias is actually what the user wanted."""
    client = _AnthFakeClient()
    llm = AnthropicLLM(api_key="x", model="claude-haiku-4-5-20251001")
    llm._client = client
    await llm.complete([Message(role="user", content="x")])
    assert client.captured.kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_openai_model_string_not_normalized() -> None:
    client = _OAIFakeClient()
    llm = OpenAILLM(api_key="x", model="gpt-4.1-mini")
    llm._client = client
    await llm.complete([Message(role="user", content="x")])
    assert client.captured.kwargs["model"] == "gpt-4.1-mini"
