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

B-320 prompt cache parity:

* ``prompt_cache_enabled`` (default False, opt-in) decorates the LAST
  system message and the LAST tool definition with
  ``cache_control: {"type": "ephemeral"}`` — the Anthropic-style cache
  marker. Moonshot Kimi K2.6 and Zhipu GLM-4.6 both adopted this
  convention via their OpenAI-compat shim, giving us the same prefix
  caching as :class:`AnthropicLLM` B-245 (~10% list price for cache
  reads, ~125% for cache creation; system + tools are the static
  prefix, hash-stable between hops in the same turn). Default OFF
  because OpenAI proper, DeepSeek, and most strict-schema compat
  servers reject the unknown field — the factory flips it on for
  models known to honor it (see ``_default_prompt_cache_enabled``).
* Cache stats are surfaced regardless of the flag —
  ``usage.prompt_tokens_details.cached_tokens`` is the OpenAI /
  DeepSeek standard (automatic caching, no opt-in, returns hit
  counts on every request). We map it onto
  :attr:`LLMResponse.cache_read_input_tokens` so the analytics /
  COST_TICK pipeline reports cache hits uniformly across providers.
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


def _default_prompt_cache_enabled(model: str, base_url: str | None) -> bool:
    """Return True for endpoints known to honor the Anthropic-style
    ``cache_control`` marker on the OpenAI-compat shim.

    Conservative allow-list — unknown providers default OFF since
    strict-schema servers (and many self-hosted vLLM / LiteLLM
    deployments) reject unknown body fields with 400 instead of
    silently dropping them. New providers can be added here as we
    confirm they accept the marker without erroring.

    OpenAI proper, DeepSeek, Ollama, vLLM, LiteLLM → False.
    Moonshot (Kimi), Zhipu (GLM) → True.
    """
    base = (base_url or "").lower()
    mdl = (model or "").lower()
    # Moonshot / Kimi family.
    if "moonshot" in base or "kimi" in mdl or "kimi" in base:
        return True
    # Zhipu / GLM family.
    if "bigmodel" in base or "z.ai" in base or "glm" in mdl:
        return True
    return False


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
    prompt_cache_enabled : bool | None
        B-320: when True, decorates the last system message + last tool
        definition with ``cache_control: {"type": "ephemeral"}``. When
        ``None`` (default), :func:`_default_prompt_cache_enabled` picks
        based on ``model`` / ``base_url`` — Moonshot / Zhipu turn it on,
        OpenAI / DeepSeek / unknown shims leave it off.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str | None = None,
        pricing: Pricing | None = None,
        *,
        prompt_cache_enabled: bool | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # B-341 (audit pass-2 #13): explicit override wins; the
        # ``.pricing`` property otherwise delegates to
        # ``xmclaw.utils.cost.lookup_pricing`` so XMclaw has one
        # canonical pricing source (post-B-335). Callers passing
        # ``pricing=Pricing(...)`` still get exactly that value.
        self._pricing_explicit: Pricing | None = pricing
        self._client: Any = None
        # B-320: explicit override > auto-detect.
        if prompt_cache_enabled is None:
            prompt_cache_enabled = _default_prompt_cache_enabled(model, base_url)
        self._prompt_cache_enabled: bool = prompt_cache_enabled

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
    def _messages_to_openai(
        messages: list[Message],
        *,
        prompt_cache_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Convert internal Messages to OpenAI chat-completions shape.

        OpenAI convention: ``system`` stays in the messages array (unlike
        Anthropic which moves it to a top-level parameter). ``tool_calls``
        on assistant messages ride alongside text content. ``tool`` role
        messages carry ``tool_call_id`` to reference their caller.

        When ``prompt_cache_enabled`` is True (B-320), the LAST system
        message gets converted to the Anthropic-style content-block
        form with ``cache_control: ephemeral`` on its single text
        block. Moonshot Kimi K2.6 and Zhipu GLM-4.6 honor this on
        their OpenAI-compat shim and treat it as a cache breakpoint.
        Other compat servers ignore unknown content fields.
        """
        from xmclaw.providers.llm.translators import openai_tool_shape as translator

        # B-320: locate the index of the last system message so we can
        # decorate it with cache_control. We pin only the LAST one
        # because the cache slot covers everything *up to* the marker;
        # marking earlier system messages would add wasted cache
        # breakpoints.
        last_system_idx = -1
        if prompt_cache_enabled:
            for i, m in enumerate(messages):
                if m.role == "system":
                    last_system_idx = i

        out: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            if m.role == "tool":
                out.append({
                    "role": "tool",
                    "content": m.content,
                    "tool_call_id": m.tool_call_id or "",
                })
                continue

            # B-320: decorate the last system message with cache_control.
            if i == last_system_idx and m.content:
                out.append({
                    "role": "system",
                    "content": [{
                        "type": "text",
                        "text": m.content,
                        "cache_control": {"type": "ephemeral"},
                    }],
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
    def _tools_to_openai(
        tools: list[ToolSpec] | None,
        *,
        prompt_cache_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        if not tools:
            return []
        out = [
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
        # B-320: cache the tool array via a marker on the last entry —
        # mirror of AnthropicLLM._tools_to_anthropic. One breakpoint
        # covers every preceding tool def.
        if prompt_cache_enabled:
            out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    @staticmethod
    def _extract_cache_tokens(usage: Any) -> tuple[int, int]:
        """B-320: pull (cache_creation_input_tokens, cache_read_input_tokens)
        out of an OpenAI-shaped ``usage`` object.

        Two source shapes in the wild:

        * **OpenAI / DeepSeek (automatic caching):**
          ``usage.prompt_tokens_details.cached_tokens`` carries the
          read count; no creation count is reported (caching is
          deterministic so creation isn't billed separately).
        * **Moonshot / Zhipu (explicit cache_control):** mirror
          Anthropic's field names — ``cache_creation_input_tokens``
          and ``cache_read_input_tokens`` exposed flat on usage. We
          probe both attribute and ``model_extra`` (pydantic v2 puts
          unknown fields there) since the openai SDK strips unknown
          attrs.

        Returns ``(creation, read)``; either may be 0.
        """
        if usage is None:
            return 0, 0
        # Flat fields (Anthropic-style on compat shims).
        creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        read_flat = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        extras: dict[str, Any] = {}
        try:
            me = getattr(usage, "model_extra", None)
            if isinstance(me, dict):
                extras.update(me)
        except Exception:  # noqa: BLE001
            pass
        if not creation:
            creation = int(extras.get("cache_creation_input_tokens", 0) or 0)
        if not read_flat:
            read_flat = int(extras.get("cache_read_input_tokens", 0) or 0)

        # Nested OpenAI shape: usage.prompt_tokens_details.cached_tokens.
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            details = extras.get("prompt_tokens_details")
        cached_nested = 0
        if details is not None:
            # SDK object exposes attrs; dict shape (compat shim) exposes keys.
            cached_nested = int(
                getattr(details, "cached_tokens", None)
                or (details.get("cached_tokens", 0) if isinstance(details, dict) else 0)
                or 0,
            )

        return creation, max(read_flat, cached_nested)

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
            "messages": self._messages_to_openai(
                messages, prompt_cache_enabled=self._prompt_cache_enabled,
            ),
            "stream": True,
        }
        tool_defs = self._tools_to_openai(
            tools, prompt_cache_enabled=self._prompt_cache_enabled,
        )
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
            "messages": self._messages_to_openai(
                messages, prompt_cache_enabled=self._prompt_cache_enabled,
            ),
        }
        tool_defs = self._tools_to_openai(
            tools, prompt_cache_enabled=self._prompt_cache_enabled,
        )
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
        cache_creation, cache_read = self._extract_cache_tokens(usage)
        return LLMResponse(
            content=text,
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            stop_reason=finish_reason,
            # B-320: surface cache stats for analytics / COST_TICK parity
            # with AnthropicLLM. 0 when the provider doesn't report them.
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
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
            "messages": self._messages_to_openai(
                messages, prompt_cache_enabled=self._prompt_cache_enabled,
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tool_defs = self._tools_to_openai(
            tools, prompt_cache_enabled=self._prompt_cache_enabled,
        )
        if tool_defs:
            kwargs["tools"] = tool_defs

        text_parts: list[str] = []
        # Tool-call assembly: deltas arrive index-by-index, accumulate by index.
        tool_acc: dict[int, dict[str, Any]] = {}
        prompt_tokens = 0
        completion_tokens = 0
        # B-320: cache stat accumulators — captured from usage chunks
        # the same way prompt/completion are. The OpenAI streaming
        # spec surfaces usage on the *last* chunk when
        # ``stream_options.include_usage`` is set.
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
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
        except Exception:  # noqa: BLE001
            return await self.complete(messages, tools)

        # B-225: watchdog — close the stream the MOMENT cancel_event
        # fires. Without this, ``async for chunk in stream`` was
        # suspended waiting for the server's next chunk (a slow LLM
        # could keep us waiting 30+ seconds before any chunk lands)
        # and the in-loop ``cancel.is_set()`` check never reached.
        # Stop button now actually works mid-call.
        from xmclaw.providers.llm.streaming_utils import start_cancel_watchdog
        _cancel_watchdog = start_cancel_watchdog(cancel, stream.aclose)

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
                        extra_bag: dict[str, Any] = {}
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
                # B-320: capture cache stats — Anthropic-style flat
                # fields (Moonshot / Zhipu) and OpenAI-style nested
                # ``prompt_tokens_details.cached_tokens`` are both
                # handled by the helper.
                cc, cr = self._extract_cache_tokens(usage)
                if cc:
                    cache_creation_input_tokens = cc
                if cr:
                    cache_read_input_tokens = cr
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
        from xmclaw.providers.llm.streaming_utils import stop_cancel_watchdog
        await stop_cancel_watchdog(_cancel_watchdog)

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
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
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
            # B-320: cache stats from the final usage chunk (or 0
            # when the provider doesn't surface them).
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.OPENAI_TOOL

    @property
    def pricing(self) -> Pricing:
        """See ``AnthropicLLM.pricing`` for the B-341 rationale —
        same delegation: explicit override wins, otherwise the
        canonical lookup_pricing result is returned."""
        if self._pricing_explicit is not None:
            return self._pricing_explicit
        from xmclaw.utils.cost import lookup_pricing
        cost_pricing = lookup_pricing(self.model)
        return Pricing(
            input_per_mtok=cost_pricing.input_per_mtok,
            output_per_mtok=cost_pricing.output_per_mtok,
        )
