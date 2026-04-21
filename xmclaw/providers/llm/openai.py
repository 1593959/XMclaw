"""OpenAILLM — thin wrapper. Supports OpenAI + OpenAI-compat endpoints.

Anti-requirement #14: any OpenAI-compat API (MiMo, GLM, Kimi, Ollama, etc.)
plugs in by constructing with a different ``base_url``.

Phase 1: stub.
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


class OpenAILLM(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def stream(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        *,
        cancel: asyncio.Event | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError("Phase 2 — migrate from xmclaw/llm/openai_client.py")
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
        return ToolCallShape.OPENAI_TOOL

    @property
    def pricing(self) -> Pricing:
        return Pricing(input_per_mtok=0.0, output_per_mtok=0.0)
