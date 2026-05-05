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
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # Default Opus 4.7 list prices. Callers override for cheaper models.
        self._pricing = pricing or Pricing(input_per_mtok=15.0, output_per_mtok=75.0)
        # The SDK client is created lazily so tests that don't touch it can
        # run without the anthropic dependency installed.
        self._client: Any = None

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
    ) -> tuple[str, list[dict[str, Any]]]:
        """Split out the system prompt and convert other messages.

        Anthropic wants ``system`` as a top-level parameter; everything else
        is in ``messages`` as alternating user/assistant. We emit blocks
        (``type: text`` / ``type: tool_use`` / ``type: tool_result``) so
        callers can round-trip tool-call history without loss.
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
            # Prefer the naked-SDK convention: plain string content when
            # there are no tool_calls. Only emit block-shaped content
            # when we actually need tool_use blocks alongside text.
            # (Anti-req #11 non-interference: match what a naked caller
            # would have sent.)
            if not m.tool_calls:
                converted.append({"role": m.role, "content": m.content})
                continue
            blocks: list[dict[str, Any]] = []
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
        return "\n\n".join(system_parts), converted

    @staticmethod
    def _tools_to_anthropic(tools: list[ToolSpec] | None) -> list[dict[str, Any]]:
        if not tools:
            return []
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters_schema,
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
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                # Normalize the SDK's block to a dict so the translator can parse.
                block_dict = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
                parsed = translator.decode_from_provider(block_dict)
                if parsed is not None:
                    tool_calls.append(parsed)

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
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
        _dumped: list[dict] = []
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
                if _cancel_watchdog is not None:
                    _cancel_watchdog.cancel()
                    try:
                        await _cancel_watchdog
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
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

        tool_calls: list[ToolCall] = []
        for block in getattr(final, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                block_dict = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
                parsed = translator.decode_from_provider(block_dict)
                if parsed is not None:
                    tool_calls.append(parsed)

        usage = getattr(final, "usage", None)
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return self._pricing
