"""AnthropicLLM — thin wrapper around the official Anthropic SDK.

Design principles (anti-req #11: same model on XMclaw must not be worse):
- Minimal transformation. The wire messages are converted to Anthropic's
  expected shape but no prompt decoration / wrapping happens here. Any
  "make-the-model-smarter" logic belongs above the provider layer where
  it's visible and benchable against a naked SDK run.
- Tool-call decoding goes through ``translators.anthropic_native.decode``,
  which returns a structured ``ToolCall`` or ``None`` — never a string
  that looks like a tool call (anti-req #1).
- Usage tokens are captured via ``stream.get_final_message()`` after the
  streaming loop finishes (method verified in v1 Fix 6 batch).

The interface is the one declared in ``providers.llm.base.LLMProvider``.
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


class AnthropicLLM(LLMProvider):
    """Anthropic Claude provider.

    Parameters
    ----------
    api_key : str
        Anthropic API key.
    model : str
        Model id (e.g. ``"claude-opus-4-7"``, ``"claude-sonnet-4-6"``).
    base_url : str | None
        Optional override (for Anthropic-compatible endpoints).
    pricing : Pricing | None
        Per-million-token USD pricing. Defaults to Opus 4.7 list prices.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-7",
        base_url: str | None = None,
        pricing: Pricing | None = None,
        *,
        context_length: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # B-341 (audit pass-2 #13): keep an explicit-override slot for
        # back-compat — when a caller passes ``pricing=Pricing(...)``
        # we honor it. Otherwise the property reads ``lookup_pricing``
        # so XMclaw has one canonical pricing source post-B-335
        # (analytics + cost-tracker both go through it).
        self._pricing_explicit: Pricing | None = pricing
        # The SDK client is created lazily so tests that don't touch it can
        # run without the anthropic dependency installed.
        self._client: Any = None
        # Wave-27 fix-6: explicit context-window override. See the
        # OpenAILLM counterpart for the rationale — any 3rd-party
        # Anthropic-compatible endpoint (api.minimaxi.com,
        # custom-portal aggregators, self-hosted shims) can declare
        # its true window via config without code edits.
        self.context_length: int | None = (
            int(context_length) if context_length and context_length > 0 else None
        )

    # ── lazy client ──

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from anthropic import AsyncAnthropic
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    # ── message / tool conversion ──

    @staticmethod
    def _messages_to_anthropic(
        messages: list[Message],
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Split out the system prompt and convert other messages.

        Anthropic wants ``system`` as a top-level parameter; everything else
        is in ``messages`` as alternating user/assistant. We emit blocks
        (``type: text`` / ``type: tool_use`` / ``type: tool_result``) so
        callers can round-trip tool-call history without loss.

        B-245: returns ``system`` as a **list of content blocks** when
        non-empty, with ``cache_control: {"type": "ephemeral"}`` on the
        single text block. Anthropic's prompt cache hashes everything
        BEFORE this marker (system + tools, in a single 5-minute TTL
        ephemeral slot). XMclaw's system prompt is ~3500 tokens and
        nearly identical across hops within a turn → cache hit rate
        ≈ 100% after the first request, every following call gets a
        90% discount on the prefix. Anthropic SDK accepts both string
        and list shapes for ``system`` since 2024-08; clients without
        caching support degrade gracefully because the block list is
        a strict superset of the string form.
        """
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            if m.role == "tool":
                # Tool result goes as a user-role message with a tool_result block.
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content,
                    }],
                })
                continue
            # B-Vision: user message with image attachments — emit
            # content blocks containing the text + each image as a
            # native Anthropic ``image`` block (source.type=base64).
            # Anthropic accepts image blocks on user messages, not
            # assistant; we guard on role just to be safe.
            if m.role == "user" and m.images:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for img in m.images:
                    block = _img_to_anthropic_block(img)
                    if block is not None:
                        blocks.append(block)
                converted.append({"role": m.role, "content": blocks})
                continue
            # Prefer the naked-SDK convention: plain string content when
            # there are no tool_calls. Only emit block-shaped content
            # when we actually need tool_use blocks alongside text.
            # (Anti-req #11 non-interference: match what a naked caller
            # would have sent.)
            if not m.tool_calls:
                converted.append({"role": m.role, "content": m.content})
                continue
            blocks = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.args,
                })
            converted.append({"role": m.role, "content": blocks})
        # B-245: emit system as a single text block carrying the
        # cache_control breakpoint. Empty system → empty list (caller
        # checks ``if system:`` and omits the param entirely).
        system_text = "\n\n".join(system_parts).strip()
        if system_text:
            system_blocks: list[dict[str, Any]] = [{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }]
            return system_blocks, converted
        return "", converted

    @staticmethod
    def _tools_to_anthropic(tools: list[ToolSpec] | None) -> list[dict[str, Any]]:
        # B-245: cache the tools array. Marking cache_control on the
        # LAST tool sets a cache breakpoint that includes every
        # preceding tool def in one cache slot. Tool descriptions
        # rarely change within a session — caching saves ~5-15K
        # tokens per call when the agent has 30+ tools (the post-B-238
        # prefilter trims to ~25 but each description is still ~200
        # tokens). Empty list returns empty (no breakpoint).
        if not tools:
            return []
        out = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters_schema,
            }
            for t in tools
        ]
        out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    # ── public API ──

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        system, anthropic_messages = self._messages_to_anthropic(messages)
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
        }
        if system:
            kwargs["system"] = system
        tool_defs = self._tools_to_anthropic(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        seq = 0
        async with client.messages.stream(**kwargs) as stream:
            async for chunk in stream.text_stream:
                if cancel is not None and cancel.is_set():
                    break
                if chunk:
                    yield LLMChunk(delta=chunk, seq=seq, raw=None)
                    seq += 1
        # ``stream.get_final_message()`` gives a single ``Message`` with usage.
        # We don't surface it on the chunk stream — callers get it via
        # ``complete`` if they need totals. The bus subscriber for streaming
        # turns uses a dedicated cost event emitted by the orchestrator.

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        from xmclaw.providers.llm.translators import anthropic_native as translator

        system, anthropic_messages = self._messages_to_anthropic(messages)
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
        }
        if system:
            kwargs["system"] = system
        tool_defs = self._tools_to_anthropic(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        t0 = time.perf_counter()
        response = await client.messages.create(**kwargs)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Extract text + tool calls from the content blocks.
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # B-229: same truncation guard as the streaming path.
        stop_reason = str(getattr(response, "stop_reason", "") or "")
        truncated_partial = 0
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                block_input = getattr(block, "input", {}) or {}
                if stop_reason == "max_tokens" and not block_input:
                    truncated_partial += 1
                    continue
                # Normalize the SDK's block to a dict so the translator can parse.
                block_dict = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": block_input,
                }
                parsed = translator.decode_from_provider(block_dict)
                if parsed is not None:
                    tool_calls.append(parsed)

        if truncated_partial > 0:
            text_parts.append(
                f"\n\n[output truncated by max_tokens limit — "
                f"{truncated_partial} partial tool call(s) dropped. "
                "Ask me to continue and I'll pick up the call.]"
            )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
            # B-245: surface cache stats from Anthropic's usage block.
            cache_creation_input_tokens=getattr(
                usage, "cache_creation_input_tokens", 0,
            ) or 0,
            cache_read_input_tokens=getattr(
                usage, "cache_read_input_tokens", 0,
            ) or 0,
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
        from xmclaw.providers.llm.translators import anthropic_native as translator

        system, anthropic_messages = self._messages_to_anthropic(messages)
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
        }
        if system:
            kwargs["system"] = system
        tool_defs = self._tools_to_anthropic(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs
        # B-216: optionally request extended thinking. Was default-ON
        # in the first cut, but real-data trace (turn 65f9ec, kimi
        # k2.6 via api.kimi.com/coding/) showed hop 0 streaming OK
        # then hops 1-9 ALL hitting the streaming fallback path —
        # the Kimi Coding-Plan endpoint rejects ``thinking`` kwarg
        # once a tool_use block is in the conversation history.
        # Made opt-in via ``self._extended_thinking`` (default
        # False) so streaming always works; users with a real
        # Claude-on-Anthropic-direct endpoint can flip the flag
        # to surface thinking content. The thinking_delta event
        # iteration below is unconditional — if the endpoint
        # ever sends one, we'll catch it with or without the
        # opt-in flag.
        if getattr(self, "_extended_thinking", False):
            kwargs["max_tokens"] = max(int(kwargs.get("max_tokens", 4096)), 8192)
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": 5000,
            }

        text_parts: list[str] = []
        cancelled = False
        t0 = time.perf_counter()
        # B-219: one-shot raw event dump — diagnose "peer sees thinking
        # on same endpoint but we don't". The first 100 events of the
        # NEXT request are written to ``~/.xmclaw/v2/anthropic_dump.json``
        # so we can see the actual SSE frame shape (do thinking_delta
        # events appear? does Kimi-coding use a non-standard event
        # type? etc.). Toggle on by ``touch ~/.xmclaw/v2/dump_next``;
        # toggle file is consumed (deleted) so the dump runs exactly
        # once. Self-removing → no risk of perpetual logging.
        from xmclaw.utils.paths import data_dir as _ddir
        _dump_flag = _ddir() / "v2" / "dump_next"
        _dump_path = _ddir() / "v2" / "anthropic_dump.json"
        _do_dump = _dump_flag.exists()
        _dumped: list[dict[str, Any]] = []
        if _do_dump:
            try:
                _dump_flag.unlink()  # consume the toggle
            except OSError:
                pass

        try:
            async with client.messages.stream(**kwargs) as stream:
                # B-225: watchdog task — close the stream the MOMENT
                # cancel_event fires, instead of waiting for the next
                # event-loop tick to land in `async for event`. Real
                # bug report: user clicked Stop at 29s, daemon got
                # the cancel frame and called set(), but
                # `async for event in stream` was suspended waiting
                # for the SERVER's next chunk (Kimi-coding takes ~30s
                # to inference before sending first event), so the
                # in-loop ``if cancel.is_set()`` check never reached.
                # The watchdog forces stream closure as soon as
                # cancel fires; the consume loop then exits with
                # whatever text accumulated so far.
                from xmclaw.providers.llm.streaming_utils import start_cancel_watchdog
                _cancel_watchdog = start_cancel_watchdog(cancel, stream.close)
                # B-216: iterate the raw event stream (not just
                # ``stream.text_stream``) so we catch
                # ``thinking_delta`` events alongside ``text_delta``.
                async for event in stream:
                    # B-219 raw dump (first 100 events): capture the
                    # full attribute surface as JSON — we want to know
                    # what fields the endpoint actually sends, not
                    # what the SDK chooses to expose as attrs.
                    if _do_dump and len(_dumped) < 100:
                        try:
                            row = {
                                "event_type": getattr(event, "type", None),
                                "model_dump": (
                                    event.model_dump()
                                    if hasattr(event, "model_dump")
                                    else None
                                ),
                                "attrs": [
                                    a for a in dir(event)
                                    if not a.startswith("_")
                                ][:30],
                            }
                            _dumped.append(row)
                        except Exception as _exc:  # noqa: BLE001
                            _dumped.append({"_dump_error": str(_exc)[:200]})
                    if cancel is not None and cancel.is_set():
                        cancelled = True
                        break
                    etype = getattr(event, "type", None)
                    if etype != "content_block_delta":
                        continue
                    delta_obj = getattr(event, "delta", None)
                    if delta_obj is None:
                        continue
                    delta_type = getattr(delta_obj, "type", None)
                    if delta_type == "text_delta":
                        text = getattr(delta_obj, "text", "") or ""
                        if not text:
                            continue
                        text_parts.append(text)
                        if on_chunk is not None:
                            await on_chunk(text)
                    elif delta_type == "thinking_delta":
                        # Extended thinking — yield to the dedicated
                        # callback so the UI shows it in PhaseCard's
                        # "思考过程" slot, not mixed with assistant
                        # text. Field name in the SDK: ``thinking``.
                        thought = getattr(delta_obj, "thinking", "") or ""
                        if thought and on_thinking_chunk is not None:
                            await on_thinking_chunk(thought)
                # B-225: watchdog may have closed the stream when cancel
                # fired — detect that case so the response we synthesise
                # is "cancelled, here's what we got" not "stream
                # finished naturally".
                if cancel is not None and cancel.is_set():
                    cancelled = True
                # Stop the watchdog now that the loop has exited
                # (whether by natural completion or cancel-close).
                from xmclaw.providers.llm.streaming_utils import stop_cancel_watchdog
                await stop_cancel_watchdog(_cancel_watchdog)
                if cancelled:
                    return LLMResponse(
                        content="".join(text_parts),
                        tool_calls=(),
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                final = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            # Some Anthropic-compat shims (MiniMax, Qwen via /anthropic) don't
            # implement the streaming endpoint. Fall back to non-streaming so
            # the user still gets an answer — they just lose live-typing UX.
            # B-216 bugfix: log so silent fallbacks don't blind us. Real-data
            # showed hops 1-9 all silently falling back when thinking kwarg
            # was on; we ONLY noticed by counting llm_chunk events in
            # events.db. Log the type so future regressions surface fast.
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "anthropic.stream_failed → fallback to complete: "
                    "%s: %s",
                    type(exc).__name__, str(exc)[:200],
                )
            except Exception:  # noqa: BLE001
                pass
            return await self.complete(messages, tools)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # B-229: capture stop_reason so the agent loop can detect
        # mid-output truncation. ``max_tokens`` here means the model
        # was cut off mid-stream — partial ``tool_use`` blocks have
        # empty ``input={}`` because the SDK never received a
        # ``content_block_stop`` event with parsed args.
        stop_reason = str(getattr(final, "stop_reason", "") or "")

        tool_calls: list[ToolCall] = []
        truncated_partial = 0
        for block in getattr(final, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                block_input = getattr(block, "input", {}) or {}
                # B-229: drop tool_use blocks whose args never arrived
                # (max_tokens cut-off mid-stream). Distinguishable from
                # a legitimate zero-args call only via stop_reason — a
                # complete tool_use with no params still has
                # input={}, but stop_reason would be "tool_use" or
                # "end_turn", not "max_tokens".
                if stop_reason == "max_tokens" and not block_input:
                    truncated_partial += 1
                    continue
                block_dict = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": block_input,
                }
                parsed = translator.decode_from_provider(block_dict)
                if parsed is not None:
                    tool_calls.append(parsed)

        if truncated_partial > 0:
            text_parts.append(
                f"\n\n[output truncated by max_tokens limit — "
                f"{truncated_partial} partial tool call(s) dropped. "
                "Ask me to continue and I'll pick up the call.]"
            )

        usage = getattr(final, "usage", None)
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
            # B-245: cache stats (streaming path).
            cache_creation_input_tokens=getattr(
                usage, "cache_creation_input_tokens", 0,
            ) or 0,
            cache_read_input_tokens=getattr(
                usage, "cache_read_input_tokens", 0,
            ) or 0,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        """B-341 (audit pass-2 #13): the constructor's ``pricing`` arg
        + this property are pre-B-335 leftovers. Production cost uses
        ``xmclaw.utils.cost.lookup_pricing`` directly (the single
        source of truth post-B-335) so this property had been
        diverging silently — no caller in production code reads it.
        Now delegates to ``lookup_pricing(self.model)`` when the
        caller didn't pass an explicit override at construction
        (the common case). Explicit override still wins so any
        legacy caller passing ``pricing=Pricing(...)`` keeps that
        value.
        """
        if self._pricing_explicit is not None:
            return self._pricing_explicit
        from xmclaw.utils.cost import lookup_pricing
        cost_pricing = lookup_pricing(self.model)
        return Pricing(
            input_per_mtok=cost_pricing.input_per_mtok,
            output_per_mtok=cost_pricing.output_per_mtok,
        )


# ── Image helpers (B-Vision) ───────────────────────────────────────

# Same constants as openai.py — image token cost scales with pixel
# area; 1280 wide is the Anthropic recommendation for "high readability,
# bounded cost".
_VISION_MAX_WIDTH = 1280
_VISION_JPEG_QUALITY = 80


def _img_to_anthropic_block(src: str) -> dict[str, Any] | None:
    """Convert a file path / data: URL to an Anthropic ``image`` block.

    Shape per Anthropic SDK:

        {"type": "image", "source": {"type": "base64",
         "media_type": "image/jpeg", "data": "<b64>"}}

    Returns ``None`` if the file is unreadable — caller drops it.
    Lossy-resizes to ``_VISION_MAX_WIDTH`` to keep image-token cost
    bounded; JPEG q80 trades a few % readability for 3-4× smaller
    payload vs PNG. Mirrors openai.py's _img_to_data_url so vision
    behaviour is identical across both translators.
    """
    if not src:
        return None
    if src.startswith("data:"):
        # data URL — parse media type + base64 payload back out.
        try:
            header, b64 = src.split(",", 1)
            media_type = header.split(";")[0][len("data:"):]
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type or "image/png",
                    "data": b64,
                },
            }
        except Exception:  # noqa: BLE001
            return None
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
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        }
    except Exception:  # noqa: BLE001
        # Last-ditch: ship original bytes, no resize.
        try:
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
        except Exception:  # noqa: BLE001
            return None
