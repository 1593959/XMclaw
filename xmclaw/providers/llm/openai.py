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
import time
from collections.abc import AsyncIterator
from typing import Any

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    OnChunkCallback,
    OnThinkingChunkCallback,
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
        # B-229: forward finish_reason so callers can detect truncation.
        finish_reason = str(getattr(choices[0], "finish_reason", "") or "")

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
            stop_reason=finish_reason,
        )

    async def complete_streaming(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        on_chunk: OnChunkCallback | None = None,
        on_thinking_chunk: OnThinkingChunkCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> LLMResponse:
        from xmclaw.providers.llm.translators import openai_tool_shape as translator

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_openai(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tool_defs = self._tools_to_openai(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        text_parts: list[str] = []
        # Tool-call assembly: deltas arrive index-by-index, accumulate by index.
        tool_acc: dict[int, dict[str, Any]] = {}
        prompt_tokens = 0
        completion_tokens = 0
        cancelled = False
        # B-229: capture finish_reason of the last chunk so we can detect
        # max_tokens truncation and drop partial tool calls.
        finish_reason = ""
        t0 = time.perf_counter()
        try:
            stream = await client.chat.completions.create(**kwargs)
        except TypeError:
            # Older SDK without stream_options kwarg — retry without it.
            kwargs.pop("stream_options", None)
            stream = await client.chat.completions.create(**kwargs)
        except Exception:
            return await self.complete(messages, tools)

        # B-225: watchdog — close the stream the MOMENT cancel_event
        # fires. Without this, ``async for chunk in stream`` was
        # suspended waiting for the server's next chunk (a slow LLM
        # could keep us waiting 30+ seconds before any chunk lands)
        # and the in-loop ``cancel.is_set()`` check never reached.
        # Stop button now actually works mid-call.
        _cancel_watchdog: asyncio.Task | None = None
        if cancel is not None:
            async def _watch_cancel():
                try:
                    await cancel.wait()
                    try:
                        await stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                except asyncio.CancelledError:
                    pass
            _cancel_watchdog = asyncio.create_task(_watch_cancel())

        async for chunk in stream:
            # B-39: bail mid-stream when the WS-side cancel event fires.
            # We close the SDK's underlying iterator by returning early.
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = getattr(choices[0], "delta", None)
                if delta is not None:
                    # B-91 / B-214: surface reasoning / extended-thinking
                    # deltas before the visible content. Three field
                    # names in the wild:
                    #   * ``reasoning_content`` — MiniMax M2 / Moonshot /
                    #     DashScope / Qwen / GLM (most "reasoning" Chinese
                    #     providers settled on this)
                    #   * ``reasoning`` — OpenAI o1 / o3 / o4 native
                    #   * ``thinking`` — some forks
                    # Pre-B-214 we only used getattr(delta, ...). The
                    # openai SDK's ChatCompletionChunk is a pydantic
                    # model that DOESN'T expose unknown fields as
                    # attributes — they land in ``model_extra``
                    # (pydantic v2) / ``__fields_set__`` extras. Audit
                    # showed 0 thinking events ever fired across 1024
                    # MiniMax requests despite the provider streaming
                    # reasoning_content. Fix: also probe the extras
                    # bag.
                    if on_thinking_chunk is not None:
                        # Build a single lookup dict from both attr and
                        # extras so the precedence stays explicit.
                        extra_bag: dict = {}
                        try:
                            me = getattr(delta, "model_extra", None)
                            if isinstance(me, dict):
                                extra_bag.update(me)
                        except Exception:  # noqa: BLE001
                            pass
                        for attr in ("reasoning_content", "reasoning", "thinking"):
                            think_delta = getattr(delta, attr, None)
                            if not (isinstance(think_delta, str) and think_delta):
                                think_delta = extra_bag.get(attr)
                            if isinstance(think_delta, str) and think_delta:
                                await on_thinking_chunk(think_delta)
                                break
                    content = getattr(delta, "content", None)
                    if content:
                        text_parts.append(content)
                        if on_chunk is not None:
                            await on_chunk(content)
                    raw_tcs = getattr(delta, "tool_calls", None) or []
                    for tc in raw_tcs:
                        idx = getattr(tc, "index", 0) or 0
                        bucket = tool_acc.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if getattr(tc, "id", None):
                            bucket["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                bucket["function"]["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                bucket["function"]["arguments"] += fn.arguments
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or prompt_tokens
                completion_tokens = (
                    getattr(usage, "completion_tokens", 0) or completion_tokens
                )
            # B-229: capture finish_reason of the LAST chunk that carries
            # one. The OpenAI streaming spec puts it on the final delta;
            # OpenAI-compat shims sometimes attach it earlier when no
            # more chunks will follow.
            if choices:
                fr = getattr(choices[0], "finish_reason", None)
                if fr:
                    finish_reason = str(fr)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # B-225: detect watchdog-driven cancel (stream closed BEFORE
        # the in-loop check could fire) and stop the watchdog cleanly.
        if cancel is not None and cancel.is_set():
            cancelled = True
        if _cancel_watchdog is not None:
            _cancel_watchdog.cancel()
            try:
                await _cancel_watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # B-39: when cancelled mid-stream, return what we accumulated
        # without parsing any partial tool-call deltas (a half-built
        # tool call would crash the agent loop's invocation step).
        if cancelled:
            return LLMResponse(
                content="".join(text_parts),
                tool_calls=(),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                stop_reason=finish_reason or "cancelled",
            )

        # B-229: when finish_reason is "length" (max_tokens), drop any
        # tool_acc entry whose ``arguments`` is empty STRING — that's
        # the initial accumulator state and only persists when the
        # stream truncated before ANY argument chunk landed. A
        # legitimate zero-args call serialises as ``"{}"`` not ``""``.
        # Without this filter the translator emits ``ToolCall(args={})``
        # and the agent loop dispatches a malformed invocation
        # (the ``code_python({})`` ghost call the user reported).
        truncated_partial = 0
        if finish_reason == "length":
            for idx in list(tool_acc.keys()):
                fn = tool_acc[idx].get("function") or {}
                args_str = fn.get("arguments", "")
                name = fn.get("name", "")
                if not args_str or not name:
                    del tool_acc[idx]
                    truncated_partial += 1
            if truncated_partial > 0:
                text_parts.append(
                    f"\n\n[output truncated by max_tokens limit — "
                    f"{truncated_partial} partial tool call(s) dropped. "
                    "Ask me to continue and I'll pick up the call.]"
                )

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_acc):
            parsed = translator.decode_from_provider(tool_acc[idx])
            if parsed is not None:
                tool_calls.append(parsed)

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            stop_reason=finish_reason,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.OPENAI_TOOL

    @property
    def pricing(self) -> Pricing:
        return self._pricing
