"""LLMProvider ABC — every LLM backend subclasses this.

Anti-requirement #11 (same model on XMclaw not worse than on peers): the
provider layer is intentionally thin — we wrap the official SDK and do
minimal transformation. Anything that could degrade output quality should
live above this layer where it's visible and benchable.

Anti-requirement #14 (protocol compat): ``tool_call_shape`` declares the
wire shape; translators in ``translators/`` map it to the internal
``ToolCall`` IR.
"""
from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec

OnChunkCallback = Callable[[str], Awaitable[None]]
# B-91: separate channel for reasoning / extended-thinking deltas.
# Same signature as OnChunkCallback so callers can wire either or
# both. Distinct alias to make the call-site intent obvious.
OnThinkingChunkCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # for role=tool


@dataclass(frozen=True, slots=True)
class LLMChunk:
    """Normalized streaming chunk. Providers convert to this before yielding."""

    delta: str
    seq: int
    raw: Any | None = None  # provider-native chunk for debug


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    # B-229 / B-230: surface the provider's stop reason so the agent loop
    # can detect mid-output truncation and either drop partial tool calls
    # or auto-continue the response. Normalised values: ``"end_turn"`` /
    # ``"max_tokens"`` / ``"tool_use"`` / ``"stop_sequence"`` (Anthropic);
    # ``"stop"`` / ``"length"`` / ``"tool_calls"`` (OpenAI). Empty string
    # = provider didn't report one (some compat shims).
    stop_reason: str = ""


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-million-token USD pricing."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]: ...

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse: ...

    async def complete_streaming(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        on_chunk: OnChunkCallback | None = None,
        on_thinking_chunk: OnThinkingChunkCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> LLMResponse:
        """Stream text deltas to ``on_chunk`` while collecting the final response.

        Default impl falls back to non-streaming ``complete()`` and fires
        ``on_chunk`` once with the full text — providers that don't support
        true streaming still satisfy the contract. Real streaming providers
        (Anthropic, OpenAI) override this to emit per-chunk deltas.

        ``cancel`` (B-39): when set mid-stream, providers that override
        this method break out of their inner streaming loop and return
        whatever's been accumulated so far. The default impl below is
        not interruptible (a single ``complete()`` call can't be split)
        — providers that need real cancellation MUST override.

        ``on_thinking_chunk`` (B-91): optional separate callback for
        reasoning / extended-thinking deltas (distinct from the user-
        visible ``on_chunk`` text stream). Providers that support
        thinking-block emission (Anthropic extended-thinking,
        OpenAI o1/o3 reasoning, MiniMax/Moonshot/DashScope
        ``reasoning_content``) call this for every thinking delta. The
        default impl is a non-streaming complete() so this is a no-op
        — only real streaming overrides have somewhere to source
        thinking deltas from.

        Returns the full ``LLMResponse`` (text + tool_calls + usage).
        Tool-use blocks aren't streamed — they arrive in the final return
        value, since the agent loop needs the whole call before invoking.
        """
        # ``tools`` passed by keyword so providers (and test mocks) that
        # declare it as keyword-only still satisfy the call. The two
        # historical mocks in tests/unit/test_v2_llm_registry.py and
        # tests/unit/test_v2_builtin_tools.py both pre-date Phase 1's
        # streaming wiring; this keyword call keeps them green without
        # forcing every test to re-implement complete_streaming.
        response = await self.complete(messages, tools=tools)
        if on_chunk is not None and response.content:
            await on_chunk(response.content)
        return response

    @property
    @abc.abstractmethod
    def tool_call_shape(self) -> ToolCallShape: ...

    @property
    @abc.abstractmethod
    def pricing(self) -> Pricing: ...
