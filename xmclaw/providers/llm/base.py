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

OnChunkCallback = Callable[[str], Awaitable[None]]

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec


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
    ) -> LLMResponse:
        """Stream text deltas to ``on_chunk`` while collecting the final response.

        Default impl falls back to non-streaming ``complete()`` and fires
        ``on_chunk`` once with the full text — providers that don't support
        true streaming still satisfy the contract. Real streaming providers
        (Anthropic, OpenAI) override this to emit per-chunk deltas.

        Returns the full ``LLMResponse`` (text + tool_calls + usage).
        Tool-use blocks aren't streamed — they arrive in the final return
        value, since the agent loop needs the whole call before invoking.
        """
        response = await self.complete(messages, tools)
        if on_chunk is not None and response.content:
            await on_chunk(response.content)
        return response

    @property
    @abc.abstractmethod
    def tool_call_shape(self) -> ToolCallShape: ...

    @property
    @abc.abstractmethod
    def pricing(self) -> Pricing: ...
