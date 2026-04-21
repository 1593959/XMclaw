"""AnthropicLLM — thin wrapper around the official Anthropic SDK.

Phase 1: stub that re-exposes the usage-capture logic from
``xmclaw/llm/anthropic_client.py`` (v1). Migration in Phase 2.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


class AnthropicLLM(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-opus-4-7") -> None:
        self.api_key = api_key
        self.model = model

    async def stream(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        *,
        cancel: asyncio.Event | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError("Phase 2 — migrate from xmclaw/llm/anthropic_client.py")
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        raise NotImplementedError("Phase 2")

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        # Opus 4.7 list prices — update when Anthropic revises.
        return Pricing(input_per_mtok=15.0, output_per_mtok=75.0)
