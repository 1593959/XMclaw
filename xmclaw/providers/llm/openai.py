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
import base64
import time
from collections.abc import AsyncIterator
from pathlib import Path
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


def _model_supports_vision(model: str | None, base_url: str | None) -> bool:
    """Return True only when the (model, endpoint) pair is known to
    accept ``image_url`` blocks on user messages.

    Default OFF — sending image blocks to a text-only model (e.g.
    ``kimi-k2-0905-preview``, ``deepseek-chat``) makes the model emit
    a canned "I can't see images" refusal trained into its base, even
    though the API silently accepted the request. The user saw this
    bug repeatedly with Kimi K2 in chat-b3c614bc (2026-05-25); prior
    fixes patched the upload path, never the capability gate.

    Conservative allow-list — better to occasionally miss a
    vision-capable model than to confidently lie to a user that the
    model "couldn't see" their picture because the LLM emitted its
    canned refusal.
    """
    mdl = (model or "").lower()
    base = (base_url or "").lower()
    if not mdl:
        return False
    # OpenAI vision-capable: 4o / 4-turbo / 4.1+ / o-series. Reject
    # 3.5 + the explicit non-vision o1-mini-2024-09-12 family.
    if "gpt-4o" in mdl or "gpt-4-turbo" in mdl or "gpt-4.1" in mdl:
        return True
    if "gpt-5" in mdl:  # forward-compat — assume modern OAI flagships ship vision.
        return True
    # Anthropic — every Claude 3+ accepts image blocks.
    if mdl.startswith("claude-3") or mdl.startswith("claude-opus") or mdl.startswith("claude-sonnet") or mdl.startswith("claude-haiku"):
        return True
    # Moonshot vision-preview (NOT kimi-k2-*-preview, which is the
    # coding model — the user's exact case in chat-b3c614bc).
    if "moonshot-v1" in mdl and "vision" in mdl:
        return True
    # Qwen-VL series.
    if "qwen-vl" in mdl or "qwen2-vl" in mdl or "qwen2.5-vl" in mdl or "qwen3-vl" in mdl:
        return True
    # GLM-4V (the V suffix is the vision variant; plain glm-4 / glm-4-plus is text-only).
    if "glm-4v" in mdl:
        return True
    # Gemini vision-capable (pro-vision, 1.5+ all multimodal).
    if "gemini-1.5" in mdl or "gemini-2" in mdl or "gemini-pro-vision" in mdl:
        return True
    # LLaVA / Pixtral / InternVL — open-weights multimodal commonly
    # exposed via OpenAI-compat shims.
    if "llava" in mdl or "pixtral" in mdl or "internvl" in mdl:
        return True
    # Route-by-base-url last-resort: an OpenRouter / together / fireworks
    # path with one of the model names above ANYWHERE in the slug.
    if "openrouter" in base or "together" in base or "fireworks" in base:
        if any(t in mdl for t in ("vl", "vision", "4o", "claude-3", "claude-opus", "claude-sonnet")):
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
        context_length: int | None = None,
        supports_vision: bool | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # 2026-06-15: config-level vision override. ``_model_supports_vision``
        # is a conservative allow-list heuristic that can't possibly know
        # every 3rd-party portal/model slug (e.g. ``agnes-2.0-flash``). When
        # the profile config explicitly sets ``supports_vision``, that wins —
        # so a user who knows their endpoint takes image_url blocks can turn
        # it on without us guessing. None = fall back to the heuristic.
        self._supports_vision_override: bool | None = supports_vision
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
        # Wave-27 fix-6: explicit context-window override so any
        # endpoint (including unknown 3rd-party portals / self-hosted
        # vLLM / niche models) can declare its window via config
        # without needing to be added to the static lookup table.
        # The compressor reads ``getattr(llm, "context_length", None)``
        # as the highest-priority signal.
        self.context_length: int | None = (
            int(context_length) if context_length and context_length > 0 else None
        )

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
        model: str | None = None,
        base_url: str | None = None,
        supports_vision_override: bool | None = None,
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
            # Wave-30 (2026-05-18): respect the CACHE_BREAKPOINT_MARKER
            # so the per-turn mutable tail (time block) doesn't poison
            # the cached prefix. See base.py:CACHE_BREAKPOINT_MARKER
            # docstring. Same shape as the anthropic-side split.
            if i == last_system_idx and m.content:
                from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER
                if CACHE_BREAKPOINT_MARKER in m.content:
                    raw_parts = [
                        p.strip("\n")
                        for p in m.content.split(CACHE_BREAKPOINT_MARKER)
                    ]
                    raw_parts = [p for p in raw_parts if p]
                    if len(raw_parts) >= 2:
                        content_blocks_sys: list[dict[str, Any]] = []
                        for j, part in enumerate(raw_parts):
                            blk: dict[str, Any] = {"type": "text", "text": part}
                            if j < len(raw_parts) - 1:
                                blk["cache_control"] = {"type": "ephemeral"}
                            content_blocks_sys.append(blk)
                        out.append({"role": "system", "content": content_blocks_sys})
                        continue
                    # Fewer than 2 parts after strip — fall through to
                    # the legacy single-block path below.
                    m_content_clean = (
                        raw_parts[0] if raw_parts else m.content
                    )
                else:
                    m_content_clean = m.content
                out.append({
                    "role": "system",
                    "content": [{
                        "type": "text",
                        "text": m_content_clean,
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
                continue

            # B-Vision: user message with image attachments — encode as
            # multimodal content list. Kimi K2.6 / GPT-4o / Claude all
            # accept image_url blocks on user messages. Tool messages
            # don't support images on the OpenAI shape (Anthropic does
            # but we route those through anthropic_native), so we never
            # multimodal-encode a non-user message.
            # 2026-06-08: vision capability gate. Pre-fix we emitted
            # ``image_url`` blocks for ANY user message with images,
            # regardless of the target model. Switching an existing chat
            # (which has image messages in history) from a vision model
            # (Kimi) to a text-only one (DeepSeek-V4-Pro) then 400'd the
            # WHOLE turn: ``unknown variant `image_url`, expected `text```
            # — one stale image block poisoned every subsequent request.
            # When the model can't see images, degrade them to a text
            # placeholder so history stays valid instead of exploding.
            # Config override wins over the heuristic allow-list.
            _vision_ok = (
                supports_vision_override
                if supports_vision_override is not None
                else _model_supports_vision(model, base_url)
            )
            if m.role == "user" and m.images and _vision_ok:
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                image_blocks: list[dict[str, Any]] = []
                for img in m.images:
                    data_url = _img_to_data_url(img)
                    if data_url is None:
                        continue
                    image_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })
                # 2026-05-24 anti-self-poisoning defense — see
                # anthropic.py _messages_to_anthropic for the full
                # story. Short: a prior model hallucinated "I can't
                # see chat images", LanceDB stored that as a high-
                # confidence fact, every new session inherited the
                # lie. Plant ground truth right next to the image
                # blocks so the input itself overrides any prior
                # claim from memory/facts/persona. Kept inside the
                # user message (not the system) to preserve the
                # cached system prefix on image-free turns.
                if image_blocks:
                    content_blocks.append({
                        "type": "text",
                        "text": (
                            "[Ground truth — image(s) attached below "
                            "are visible to you in this turn. You DO "
                            "see them. Disregard any prior claim "
                            "(in memory, facts, or earlier turns) "
                            "that you cannot see pasted/uploaded "
                            "images — those claims are stale and "
                            "wrong.]"
                        ),
                    })
                    content_blocks.extend(image_blocks)
                entry = {"role": m.role, "content": content_blocks}
                out.append(entry)
                continue

            content_str = m.content or ""
            # 2026-06-08: image degradation. A user message that carried
            # images but is NOT going to a vision model (gate above)
            # falls through here as plain text. If it had no text body,
            # substitute a placeholder so the message isn't empty (some
            # endpoints reject empty user content) and the model has a
            # hint that an image was present but isn't visible to it.
            if m.role == "user" and m.images and not _vision_ok:
                _n = len(m.images)
                _placeholder = f"[图片 ×{_n}（当前模型不支持图像，未传入）]" if _n else ""
                content_str = (content_str + ("\n" if content_str else "") + _placeholder).strip()
            # Wave-30: strip CACHE_BREAKPOINT_MARKER for endpoints that
            # don't honor cache_control. Without this strip, system
            # messages on standard OpenAI / DeepSeek / unknown shims
            # would surface the literal sentinel to the model. (When
            # prompt_cache_enabled=True, the system branch above
            # consumes the marker by splitting on it; this is the
            # cache-disabled fallback path.)
            if m.role == "system" and content_str:
                from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER
                if CACHE_BREAKPOINT_MARKER in content_str:
                    # Replace marker with a plain double-newline so
                    # the visible text reads the same as it would
                    # without cache support.
                    content_str = content_str.replace(
                        CACHE_BREAKPOINT_MARKER, "\n\n",
                    )
            # 2026-06-10: do NOT echo thinking on the OpenAI shape.
            # The 2026-05-26 fix emitted a ``{type: thinking}`` content
            # block + top-level ``reasoning_content`` on assistant
            # messages, chasing a "must be passed back" 400 that actually
            # came from an Anthropic-shaped endpoint (fixed there since).
            # On the real OpenAI chat-completions shape this is doubly
            # wrong: DeepSeek /v1 rejects the block with
            # ``messages[N]: unknown variant `thinking`, expected `text```
            # (one assistant turn with thinking in history then poisons
            # every subsequent request), and DeepSeek's docs explicitly
            # forbid passing ``reasoning_content`` back in the input.
            # Reasoning is a response-only artifact here — serialize the
            # assistant turn as plain text.
            entry = {"role": m.role, "content": content_str}
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
        # B-320: cache the tool array via a marker. Wave-30
        # (2026-05-18): mirror the anthropic-side fix — place the
        # breakpoint on the LAST STABLE tool (the one just before
        # the first ``skill_*`` prefilter output), not on the very
        # last entry. The B-238 prefilter shuffles top-K
        # ``skill_*`` tools per turn; marking that boundary
        # invalidates the cache every single turn even though every
        # workhorse tool definition is unchanged.
        if prompt_cache_enabled:
            first_skill_idx: int | None = None
            for i, t in enumerate(tools):
                if (t.name or "").startswith("skill_"):
                    first_skill_idx = i
                    break
            boundary = (
                first_skill_idx - 1
                if first_skill_idx is not None and first_skill_idx > 0
                else len(out) - 1
            )
            if 0 <= boundary < len(out):
                out[boundary]["cache_control"] = {"type": "ephemeral"}
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
                messages,
                prompt_cache_enabled=self._prompt_cache_enabled,
                model=self.model,
                base_url=self.base_url,
                supports_vision_override=self._supports_vision_override,
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
                messages,
                prompt_cache_enabled=self._prompt_cache_enabled,
                model=self.model,
                base_url=self.base_url,
                supports_vision_override=self._supports_vision_override,
            ),
        }
        tool_defs = self._tools_to_openai(
            tools, prompt_cache_enabled=self._prompt_cache_enabled,
        )
        if tool_defs:
            kwargs["tools"] = tool_defs

        t0 = time.perf_counter()
        # Exponential backoff for transient API errors (audit 2026-06-11).
        _max_retries = 3
        _base_delay = 1.0
        for _attempt in range(_max_retries + 1):
            try:
                response = await client.chat.completions.create(**kwargs)
                break
            except Exception as _e:
                _etype = type(_e).__name__
                _msg = str(_e)[:200]
                if (
                    "429" not in _msg
                    and "529" not in _msg
                    and "overloaded" not in _msg.lower()
                    and "rate" not in _msg.lower()
                    and "capacity" not in _msg.lower()
                    and _attempt >= _max_retries
                ):
                    raise
                _delay = _base_delay * (2 ** _attempt)
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "openai.retry attempt=%d/%d delay=%.1fs err=%s",
                    _attempt + 1, _max_retries, _delay, _etype,
                )
                await asyncio.sleep(_delay)
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
        # 2026-05-26 (hotfix): hop_loop always passes on_tool_block
        # (Wave-32+ speculation cache). Anthropic + base class accept
        # it; OpenAI shape was missing it → ``TypeError: got an
        # unexpected keyword argument 'on_tool_block'`` for every
        # OpenAI-compat provider (DeepSeek, Kimi via openai-shim,
        # Zhipu, etc.). User hit it switching from Kimi-anthropic
        # to deepseek-v4-pro. Degenerate impl fires the callback
        # AFTER the full response — same as the base-class fallback;
        # OpenAI-shape streaming doesn't surface tool-block lifecycle
        # events the way Anthropic does, so speculation gets the
        # tool calls in one batch rather than as soon as the model
        # finishes each block.
        on_tool_block: Any | None = None,
        # 2026-05-30: hop_loop wires this for Anthropic risk-reject
        # banners. OpenAI-shape providers don't have an equivalent
        # "stream available but rejected" path — their streaming
        # either works or the whole request 4xxs — so this is accepted
        # for signature parity and left unused.
        on_stream_fallback: Any | None = None,
        cancel: asyncio.Event | None = None,
        # 2026-06-14: per-call thinking override (anthropic uses it). OpenAI
        # shape has no equivalent stream-time budget knob — accepted for
        # signature parity, left unused. (Reasoning-effort wiring is a
        # separate future hook.)
        extended_thinking: bool | None = None,
    ) -> LLMResponse:
        from xmclaw.providers.llm.translators import openai_tool_shape as translator

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_openai(
                messages,
                prompt_cache_enabled=self._prompt_cache_enabled,
                model=self.model,
                base_url=self.base_url,
                supports_vision_override=self._supports_vision_override,
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
        # 2026-05-26: accumulate thinking/reasoning chunks so the
        # final LLMResponse carries them. Previously discarded after
        # the on_thinking_chunk callback — DeepSeek V4 thinking mode
        # then 400'd on the next hop ("thinking must be passed back").
        thinking_parts: list[str] = []
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

        # 2026-05-26: defensive resolver. Some OpenAI-compat SDK
        # versions (DeepSeek's pinned older fork in particular) return
        # an AsyncStream that lacks ``aclose`` — accessing the attr
        # raised AttributeError BEFORE the watchdog even started,
        # killing the whole turn with no useful diagnostic. Fall back
        # in order:
        #   1. .aclose()  (modern openai-python)
        #   2. .close()   (older shim)
        #   3. response.close() if exposed
        #   4. no-op coroutine — degrade to "stop button might lag
        #      by one chunk" rather than crash the turn.
        async def _close_stream() -> None:
            for attr in ("aclose", "close"):
                fn = getattr(stream, attr, None)
                if fn is not None:
                    res = fn()
                    if hasattr(res, "__await__"):
                        await res
                    return
            # Last-ditch: try the underlying response object.
            resp_obj = getattr(stream, "response", None)
            if resp_obj is not None:
                close_fn = getattr(resp_obj, "aclose", None) or getattr(resp_obj, "close", None)
                if close_fn is not None:
                    res = close_fn()
                    if hasattr(res, "__await__"):
                        await res

        _cancel_watchdog = start_cancel_watchdog(cancel, _close_stream)

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
                                thinking_parts.append(think_delta)
                                await on_thinking_chunk(think_delta)
                                break
                    else:
                        # 2026-05-26: even when no on_thinking_chunk
                        # callback is wired we still need to accumulate
                        # thinking so the LLMResponse can echo it
                        # back. DeepSeek V4 thinking mode hard-
                        # requires this on subsequent hops.
                        extra_bag2: dict[str, Any] = {}
                        try:
                            me2 = getattr(delta, "model_extra", None)
                            if isinstance(me2, dict):
                                extra_bag2.update(me2)
                        except Exception:  # noqa: BLE001
                            pass
                        for attr in ("reasoning_content", "reasoning", "thinking"):
                            d = getattr(delta, attr, None)
                            if not (isinstance(d, str) and d):
                                d = extra_bag2.get(attr)
                            if isinstance(d, str) and d:
                                thinking_parts.append(d)
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

        # 2026-05-26 (hotfix): fire on_tool_block once per parsed
        # tool call. Matches the base-class fallback semantic.
        # Anthropic fires this DURING streaming (per content-block
        # close), enabling speculation prefetch. OpenAI-shape
        # streaming doesn't surface that lifecycle, so speculation
        # gets the batch in one go — still useful (cache is
        # populated by the time hop_loop dispatches), just less
        # latency-hidden.
        if on_tool_block is not None and tool_calls:
            for _tc in tool_calls:
                try:
                    on_tool_block(_tc)
                except Exception:  # noqa: BLE001 — callback failure
                    # must not corrupt the response. Speculation
                    # is an optimisation; downstream invoke still
                    # fires.
                    pass

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
            # 2026-05-26: thinking accumulated from the stream so
            # hop_loop can echo it back on the next assistant turn.
            thinking="".join(thinking_parts),
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


# ── Image helpers (B-Vision) ───────────────────────────────────────

# Max width we feed to the model. PRIOR ATTEMPT (1280 wide): images
# were resized, agent saw resized image, but mouse_click expects
# ORIGINAL screen pixels — coord-space mismatch caused stale clicks
# on any "look at image, read coord, click" path. NOW: 1920 cap so a
# 2560×1600 screen sees a 1920×1200 view with the SAME aspect; agent's
# coords are scaled cleanly by 4/3 (which most vision-capable LLMs
# handle correctly) or we just don't resize for screens at-or-below
# this. Kimi K2.6 / GPT-4o / Sonnet all accept 1920 wide images.
_VISION_MAX_WIDTH = 1920
_VISION_JPEG_QUALITY = 85


def _img_to_data_url(src: str) -> str | None:
    """Convert a file path or pass-through data URL into a data: URL the
    OpenAI-compat API accepts. Returns ``None`` on any failure — the
    caller drops the attachment silently rather than aborting the turn.

    File-path inputs are LOSSY-RESIZED to ``_VISION_MAX_WIDTH`` to keep
    image-token cost bounded. Re-encoded as JPEG for size; PNG goes in
    at 2-4× the byte count with no readability win for screenshots.
    """
    if not src:
        return None
    if src.startswith("data:"):
        return src
    p = Path(src)
    if not p.is_file():
        return None
    try:
        from PIL import Image  # type: ignore
        from io import BytesIO

        img = Image.open(str(p))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        if img.width > _VISION_MAX_WIDTH:
            ratio = _VISION_MAX_WIDTH / img.width
            img = img.resize(
                (_VISION_MAX_WIDTH, int(img.height * ratio)),
                Image.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=_VISION_JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001 — never abort a turn over image decode
        try:
            # Last-ditch: ship the original bytes without resize.
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except Exception:  # noqa: BLE001
            return None
