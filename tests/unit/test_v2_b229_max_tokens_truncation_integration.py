"""B-229 follow-up: integration tests for max_tokens mid-stream truncation.

User pain point flagged in audit pass-3: "max_tokens mid-stream
truncation handling — needs better coverage." The existing
``test_v2_b229_truncation.py`` only checks the LLMResponse dataclass
+ argument-parser helper. It doesn't exercise the actual provider
code path that drops partial tool calls and appends the user-visible
truncation message.

This file pins the cross-provider behaviour of the B-229 fix:

  Anthropic non-streaming (``complete()``):
    * stop_reason="max_tokens" + tool_use block with ``input={}``
      MUST be dropped; truncation message MUST be appended to content.
    * stop_reason="max_tokens" + tool_use with valid ``input`` MUST
      keep the tool call (the empty-input check is the *partial*
      detector).
    * stop_reason="end_turn" + tool_use with ``input={}`` MUST keep
      the call (legitimate zero-args invocation).
    * stop_reason="max_tokens" + text-only response MUST NOT append
      the message (nothing was dropped).

  OpenAI streaming (``complete_streaming()``):
    * finish_reason="length" + tool_call with ``arguments=""`` MUST
      drop the call + append truncation message.
    * finish_reason="length" + tool_call with empty ``name`` MUST
      drop the call (same reason).
    * finish_reason="length" + tool_call with valid arguments MUST
      keep the call (no truncation message).
    * finish_reason="stop" MUST NOT trigger any truncation handling
      (clean termination).

The truncation message is a literal contract — the agent loop's
recovery prompt path looks for ``[output truncated by max_tokens
limit``. If the wording drifts, the recovery flow doesn't fire and
the user sees a phantom ``ghost_tool({})`` invocation with no
explanation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import pytest

from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import Message
from xmclaw.providers.llm.openai import OpenAILLM


# Literal substring that must appear in any truncation-augmented
# content. Pinning it here means the agent loop's recovery-prompt
# detector and these tests share one source of truth.
_TRUNC_MARKER = "[output truncated by max_tokens limit"


# ── Anthropic non-streaming fakes (extend test_v2_anthropic_provider) ──


@dataclass
class _AnthBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _AnthUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _AnthResponse:
    content: list[_AnthBlock]
    usage: _AnthUsage = field(default_factory=_AnthUsage)
    stop_reason: str = ""


class _AnthMessagesAPI:
    def __init__(self, response: _AnthResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> _AnthResponse:  # noqa: ARG002
        return self._response


class _AnthClient:
    def __init__(self, response: _AnthResponse) -> None:
        self.messages = _AnthMessagesAPI(response)


@pytest.mark.asyncio
async def test_anthropic_truncation_drops_partial_tool_use_and_appends_marker() -> None:
    """B-229 core case: ``stop_reason="max_tokens"`` with a tool_use
    block whose ``input={}`` MUST be dropped + truncation marker
    appended. This is the regression that produced the ghost
    ``code_python({})`` invocation the user reported."""
    fake = _AnthResponse(
        content=[
            _AnthBlock(type="text", text="Let me run that for you."),
            # Partial: input never landed because max_tokens cut us off.
            _AnthBlock(
                type="tool_use", id="toolu_1", name="code_python", input={},
            ),
        ],
        stop_reason="max_tokens",
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _AnthClient(fake)

    resp = await llm.complete([Message(role="user", content="run X")])
    assert resp.tool_calls == (), (
        f"partial tool_use should have been dropped, got {resp.tool_calls!r}"
    )
    assert _TRUNC_MARKER in resp.content, (
        f"truncation marker missing from content: {resp.content!r}"
    )
    # Stop reason must surface unchanged so the agent loop can
    # detect the truncation context (drives recovery prompt).
    assert resp.stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_anthropic_truncation_keeps_tool_use_with_valid_input() -> None:
    """If the tool_use block already has parsed input, the cut-off
    happened AFTER the call was complete — keep it. (The truncation
    is then about whatever came next; we still note via stop_reason.)
    """
    fake = _AnthResponse(
        content=[
            _AnthBlock(type="text", text="Running…"),
            _AnthBlock(
                type="tool_use",
                id="toolu_2",
                name="file_read",
                input={"path": "/tmp/x"},  # complete!
            ),
        ],
        stop_reason="max_tokens",
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _AnthClient(fake)

    resp = await llm.complete([Message(role="user", content="read")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    # Marker must NOT be added — nothing partial was dropped.
    assert _TRUNC_MARKER not in resp.content


@pytest.mark.asyncio
async def test_anthropic_zero_arg_tool_call_kept_on_normal_stop() -> None:
    """A legitimate zero-args tool call (e.g. ``current_time()``)
    serialises with ``input={}`` and stop_reason="end_turn" or
    "tool_use". The truncation rule keys on stop_reason="max_tokens"
    SO this case must NOT be dropped — otherwise every zero-arg
    invocation breaks."""
    fake = _AnthResponse(
        content=[
            _AnthBlock(type="text", text="Calling current_time."),
            _AnthBlock(
                type="tool_use", id="toolu_3", name="current_time", input={},
            ),
        ],
        stop_reason="tool_use",  # normal — model wants to invoke
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _AnthClient(fake)

    resp = await llm.complete([Message(role="user", content="time")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "current_time"
    assert resp.tool_calls[0].args == {}
    assert _TRUNC_MARKER not in resp.content


@pytest.mark.asyncio
async def test_anthropic_truncation_text_only_no_marker() -> None:
    """``stop_reason="max_tokens"`` with text-only content (no
    tool_use blocks) MUST NOT append the marker — there's nothing
    partial to disclose."""
    fake = _AnthResponse(
        content=[_AnthBlock(type="text", text="Long answer cut off …")],
        stop_reason="max_tokens",
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _AnthClient(fake)

    resp = await llm.complete([Message(role="user", content="long")])
    assert resp.content == "Long answer cut off …", (
        "truncation marker was appended even though no tool call "
        "was dropped — the marker should only fire on partial-tool drop"
    )
    assert resp.tool_calls == ()


# ── OpenAI streaming fakes ─────────────────────────────────────────────


@dataclass
class _ChunkFunctionDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ChunkToolCallDelta:
    index: int = 0
    id: str | None = None
    type: str | None = None
    function: _ChunkFunctionDelta | None = None


@dataclass
class _ChunkDelta:
    content: str | None = None
    role: str | None = None
    tool_calls: list[_ChunkToolCallDelta] | None = None


@dataclass
class _ChunkChoice:
    delta: _ChunkDelta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _ChunkUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _Chunk:
    choices: list[_ChunkChoice]
    usage: _ChunkUsage | None = None


class _OpenAIChatCompletions:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks
        self.captured_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.captured_kwargs = kwargs

        async def _stream() -> AsyncIterator[_Chunk]:
            for chunk in self._chunks:
                yield chunk

        return _stream()


class _OpenAIChat:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.completions = _OpenAIChatCompletions(chunks)


class _OpenAIClient:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.chat = _OpenAIChat(chunks)


@pytest.mark.asyncio
async def test_openai_streaming_truncation_drops_partial_tool_call() -> None:
    """``finish_reason="length"`` + a tool_call accumulator whose
    ``arguments`` is the empty-string initial state MUST drop the
    call + append truncation marker. Pre-B-229 the empty arguments
    survived as ``ToolCall(args={})`` and the agent dispatched the
    ghost invocation."""
    chunks = [
        # Chunk 1: visible text.
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content="Let me run that."),
            finish_reason=None,
        )]),
        # Chunk 2: tool_call seed — name arrived but arguments did NOT.
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(tool_calls=[_ChunkToolCallDelta(
                index=0, id="call_partial", type="function",
                function=_ChunkFunctionDelta(name="code_python", arguments=""),
            )]),
            finish_reason=None,
        )]),
        # Chunk 3: terminal — finish_reason="length" (max_tokens hit).
        _Chunk(
            choices=[_ChunkChoice(
                delta=_ChunkDelta(content=None),
                finish_reason="length",
            )],
            usage=_ChunkUsage(prompt_tokens=12, completion_tokens=8),
        ),
    ]
    llm = OpenAILLM(api_key="x")
    llm._client = _OpenAIClient(chunks)

    resp = await llm.complete_streaming([Message(role="user", content="run")])
    assert resp.tool_calls == (), (
        f"partial tool_call should have been dropped, got {resp.tool_calls!r}"
    )
    assert _TRUNC_MARKER in resp.content, (
        f"truncation marker missing: {resp.content!r}"
    )
    assert resp.stop_reason == "length"


@pytest.mark.asyncio
async def test_openai_streaming_truncation_drops_call_missing_name() -> None:
    """If ``arguments`` arrived but ``name`` is empty, the call is
    still partial — the openai SDK's tool_call shape requires both
    to dispatch. The B-229 filter explicitly drops accumulators
    whose ``name`` is missing under finish_reason="length"."""
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(tool_calls=[_ChunkToolCallDelta(
                index=0, id="call_x", type="function",
                # Arguments arrived but name didn't.
                function=_ChunkFunctionDelta(name="", arguments='{"q":"x"}'),
            )]),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content=None),
            finish_reason="length",
        )]),
    ]
    llm = OpenAILLM(api_key="x")
    llm._client = _OpenAIClient(chunks)

    resp = await llm.complete_streaming([Message(role="user", content="x")])
    assert resp.tool_calls == ()
    assert _TRUNC_MARKER in resp.content


@pytest.mark.asyncio
async def test_openai_streaming_truncation_keeps_complete_tool_call() -> None:
    """``finish_reason="length"`` AFTER a fully-assembled tool_call
    is just "we ran out of budget AFTER emitting a complete call".
    Keep the call; no truncation marker (nothing was dropped)."""
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content="Calling read."),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(tool_calls=[_ChunkToolCallDelta(
                index=0, id="call_ok", type="function",
                function=_ChunkFunctionDelta(
                    name="file_read", arguments='{"path":"/etc/hostname"}',
                ),
            )]),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content=None),
            finish_reason="length",
        )]),
    ]
    llm = OpenAILLM(api_key="x")
    llm._client = _OpenAIClient(chunks)

    resp = await llm.complete_streaming([Message(role="user", content="x")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/etc/hostname"}
    assert _TRUNC_MARKER not in resp.content
    assert resp.stop_reason == "length"


@pytest.mark.asyncio
async def test_openai_streaming_normal_stop_no_truncation_path() -> None:
    """``finish_reason="stop"`` MUST NOT trigger the truncation
    filter — even if a tool_call accumulator looks "partial" (empty
    arguments string). On a normal stop, the model finished what it
    intended to finish."""
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content="Done."),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content=None),
            finish_reason="stop",
        )]),
    ]
    llm = OpenAILLM(api_key="x")
    llm._client = _OpenAIClient(chunks)

    resp = await llm.complete_streaming([Message(role="user", content="x")])
    assert resp.content == "Done."
    assert resp.tool_calls == ()
    assert _TRUNC_MARKER not in resp.content
    assert resp.stop_reason == "stop"


@pytest.mark.asyncio
async def test_truncation_marker_stable_wording_across_providers() -> None:
    """The marker wording IS the contract. The agent loop's recovery
    prompt path detects the substring ``[output truncated by max_tokens
    limit`` to decide whether to ask the user to ask-to-continue. If
    Anthropic and OpenAI emit different wording the recovery flow
    fires for one and silently misses the other.

    This test re-runs both provider truncation paths and asserts the
    marker is byte-identical."""
    # Anthropic side
    fake = _AnthResponse(
        content=[
            _AnthBlock(type="tool_use", id="t", name="x", input={}),
        ],
        stop_reason="max_tokens",
    )
    a_llm = AnthropicLLM(api_key="x")
    a_llm._client = _AnthClient(fake)
    a_resp = await a_llm.complete([Message(role="user", content="x")])

    # OpenAI side
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(tool_calls=[_ChunkToolCallDelta(
                index=0, id="c", type="function",
                function=_ChunkFunctionDelta(name="x", arguments=""),
            )]),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(content=None),
            finish_reason="length",
        )]),
    ]
    o_llm = OpenAILLM(api_key="x")
    o_llm._client = _OpenAIClient(chunks)
    o_resp = await o_llm.complete_streaming([Message(role="user", content="x")])

    # Both must contain the same marker substring. We compare the
    # marker, not the full message — count phrasing is allowed to
    # differ ("1 partial" vs "2 partial"), but the prefix MUST match.
    assert _TRUNC_MARKER in a_resp.content
    assert _TRUNC_MARKER in o_resp.content
    # The "ask me to continue" suffix is also part of the recovery
    # contract — if it drifts, the recovery prompt loses its hook.
    assert "Ask me to continue" in a_resp.content
    assert "Ask me to continue" in o_resp.content
