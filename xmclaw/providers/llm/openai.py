"""OpenAILLM — thin wrapper around the official openai SDK.

Handles OpenAI proper AND any OpenAI-compatible endpoint (GLM, Kimi,
MiMo, MiniMax's /v1 endpoint, Ollama, vLLM, LiteLLM, etc.) — the only
difference is ``base_url``. Anti-req #14 (protocol compat) in concrete
form.

Design principles (same as AnthropicLLM — anti-req #11):
* Minimal transformation. System prompt stays in the messages array
  (OpenAI convention), role names pass through unchanged.
* Tool-call decoding is delegated to the ``openai_tool_shape``
  translator; a malformed ``tool_calls`` entry produces ``None`` and is
  silently dropped (anti-req #1).
* Usage tokens captured from ``response.usage``; latency measured here.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


class OpenAILLM(LLMProvider):
    """OpenAI / OpenAI-compat provider.

    Parameters
    ----------
    api_key : str
        API key for the endpoint.
    model : str
        Model id. Value depends on endpoint: ``"gpt-4o"`` for OpenAI,
        ``"glm-4"``, ``"kimi-k2"``, etc. for compatible providers.
    base_url : str | None
        Override when using an OpenAI-compat endpoint. If None, the SDK's
        default (``https://api.openai.com/v1``) is used.
    pricing : Pricing | None
        Per-million-token USD pricing. Pass explicitly for compat
        endpoints; OpenAI-proper defaults are the published GPT-4o rates.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str | None = None,
        pricing: Pricing | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # gpt-4o list prices (Dec 2024). Callers override for other models.
        self._pricing = pricing or Pricing(input_per_mtok=2.5, output_per_mtok=10.0)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import AsyncOpenAI
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = AsyncOpenAI(**kwargs)
        return self._client

    # ── message / tool conversion ──

    @staticmethod
    def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal Messages to OpenAI chat-completions shape.

        OpenAI convention: ``system`` stays in the messages array (unlike
        Anthropic which moves it to a top-level parameter). ``tool_calls``
        on assistant messages ride alongside text content. ``tool`` role
        messages carry ``tool_call_id`` to reference their caller.
        """
        from xmclaw.providers.llm.translators import openai_tool_shape as translator

        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                out.append({
                    "role": "tool",
                    "content": m.content,
                    "tool_call_id": m.tool_call_id or "",
                })
                continue

            entry: dict[str, Any] = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                entry["tool_calls"] = [
                    translator.encode_to_provider(tc) for tc in m.tool_calls
                ]
                # Assistant messages with tool_calls may have empty content;
                # OpenAI allows this so long as tool_calls is present.
            out.append(entry)
        return out

    @staticmethod
    def _tools_to_openai(tools: list[ToolSpec] | None) -> list[dict[str, Any]]:
        if not tools:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_schema,
                },
            }
            for t in tools
        ]

    # ── public API ──

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_openai(messages),
            "stream": True,
        }
        tool_defs = self._tools_to_openai(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        seq = 0
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if cancel is not None and cancel.is_set():
                break
            # chunk.choices[0].delta.content may be None for non-text deltas
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                yield LLMChunk(delta=content, seq=seq, raw=None)
                seq += 1

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        from xmclaw.providers.llm.translators import openai_tool_shape as translator

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_openai(messages),
        }
        tool_defs = self._tools_to_openai(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        t0 = time.perf_counter()
        response = await client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        choices = getattr(response, "choices", None) or []
        if not choices:
            return LLMResponse(content="", tool_calls=(), latency_ms=latency_ms)
        msg = choices[0].message
        text = getattr(msg, "content", "") or ""

        tool_calls: list[ToolCall] = []
        raw_tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in raw_tool_calls:
            # SDK objects expose attributes; normalize to dict for the translator.
            fn = getattr(tc, "function", None)
            item = {
                "id": getattr(tc, "id", ""),
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": getattr(fn, "name", "") if fn else "",
                    "arguments": getattr(fn, "arguments", "") if fn else "",
                },
            }
            parsed = translator.decode_from_provider(item)
            if parsed is not None:
                tool_calls.append(parsed)

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=text,
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.OPENAI_TOOL

    @property
    def pricing(self) -> Pricing:
        return self._pricing
