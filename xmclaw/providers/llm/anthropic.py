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
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


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
        max_tokens: int | None = None,
        extended_thinking: bool = False,
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
        # Wave-27 fix-6: explicit context-window override.
        self.context_length: int | None = (
            int(context_length) if context_length and context_length > 0 else None
        )
        # Operational params.
        self.max_tokens: int = (
            int(max_tokens) if max_tokens and max_tokens > 0 else 8192
        )
        # Fix audit 2026-06-11: ``_extended_thinking`` was previously
        # never initialised; the ``getattr`` guard in complete_streaming
        # always returned False, making the feature unreachable.
        self._extended_thinking = extended_thinking

    # ── lazy client ──

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from anthropic import AsyncAnthropic
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        # 2026-06-07 ROOT CAUSE of the chronic "APIConnectionError:
        # Connection error." that failed turn after turn:
        # the anthropic SDK reads ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY
        # from the environment. On this host those vars exist but are
        # EMPTY (set by another tool). An empty AUTH_TOKEN makes the SDK
        # emit ``Authorization: Bearer `` (empty value), which httpx
        # rejects at the protocol layer with
        # ``LocalProtocolError: Illegal header value b'Bearer '`` — the
        # SDK wraps that as APIConnectionError, masquerading as a network
        # failure. A raw httpx POST with just x-api-key succeeds in ~1s,
        # which is what made this look like "the network is fine but the
        # daemon can't connect".
        #
        # Fix: scrub EMPTY anthropic auth env vars before constructing the
        # client. The SDK reads these at init; an empty (but present)
        # ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY makes it emit the
        # malformed empty-Bearer header. An empty value has no legitimate
        # use, so deleting it in-process is safe and surgical — we then
        # authenticate purely via the api_key we pass (x-api-key header).
        import os as _os
        for _ev in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if _ev in _os.environ and not _os.environ[_ev].strip():
                del _os.environ[_ev]
        kwargs["auth_token"] = None
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
                attached_images: list[dict[str, Any]] = []
                for img in m.images:
                    block = _img_to_anthropic_block(img)
                    if block is not None:
                        attached_images.append(block)
                # 2026-05-24 anti-self-poisoning defense. Real-data:
                # an earlier model hallucinated "I can't see images
                # pasted in chat" → daily memory log captured it →
                # LLMFactExtractor wrote 9 high-confidence facts
                # claiming that limitation → every new session's
                # system prompt asserted the lie → every model
                # parroted it (even when actually receiving images).
                # The structural fix: when image blocks ARE present
                # in THIS user message, plant a tiny ground-truth
                # note RIGHT NEXT to the images. Ground truth at
                # the input boundary beats any prior claim in
                # system / memory / facts. Kept inside the user
                # message (not system) so the cached system prefix
                # stays byte-stable → no cache miss on image-free
                # turns. Worded so it's a hard fact, not advice the
                # model can soft-override.
                if attached_images:
                    blocks.append({
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
                    blocks.extend(attached_images)
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
        # cache_control breakpoint. Wave-30 (2026-05-18): respect the
        # CACHE_BREAKPOINT_MARKER sentinel so agent_loop can isolate
        # the per-turn mutable tail (time block) from the stable
        # prefix (system prompt + persona-derived persona / autobio
        # block). Pre-fix: the whole 3500-token system became one
        # cache block and the trailing time string changed every
        # second → 0% prompt-cache hit across turns within a session.
        # Post-fix: each stable part becomes its own text block with
        # cache_control set; the trailing mutable part gets no
        # cache_control so it doesn't poison the cached prefix.
        #
        # Anthropic 4-breakpoint budget: each ``cache_control`` token
        # in the request counts as one breakpoint. Current usage:
        #   * 2 in system (frozen / autobio, time-tail unmarked)
        #   * 1 in tools (last stable tool, before prefilter skills)
        #   * 1 in messages (last message — added by
        #     ``_mark_history_cache_breakpoint`` below)
        # Total = 4, exactly at budget. This covers prior history
        # so a 28K-token chat doesn't re-bill its prefix every hop.
        # 2026-06-10: enforce Anthropic's tool_use/tool_result pairing
        # invariant before returning. Strict endpoints (DeepSeek's
        # anthropic-compat) 400 the whole request on any violation:
        # ``tool_use ids were found without tool_result blocks
        # immediately after``. Violations enter history legitimately —
        # turns made under the OpenAI provider then replayed here after
        # a model switch, a turn that crashed between tool_use and
        # result, or history pruning dropping one side. One bad pair
        # then poisons every subsequent request in the session.
        converted = AnthropicLLM._repair_tool_pairing(converted)
        system_text = "\n\n".join(system_parts).strip()
        if not system_text:
            AnthropicLLM._mark_history_cache_breakpoint(converted)
            return "", converted

        from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER
        if CACHE_BREAKPOINT_MARKER in system_text:
            raw_parts = [
                p.strip("\n") for p in system_text.split(CACHE_BREAKPOINT_MARKER)
            ]
            raw_parts = [p for p in raw_parts if p]
            if len(raw_parts) >= 2:
                # 4-breakpoint budget validation (audit 2026-06-11).
                # Anthropic's API allows ≤4 cache_control blocks total
                # across tools + system + messages. Excess breakpoints
                # cause cache misses without API errors.
                # budget: (len-1) system + 1 tools = len total.
                _MAX_BREAKPOINTS = 4
                _sys_bp_count = len(raw_parts) - 1
                if _sys_bp_count + 1 > _MAX_BREAKPOINTS:
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).warning(
                        "anthropic.cache_breakpoints_over_budget "
                        "system=%d tools=1 max=%d — excess will be "
                        "silently ignored by the API",
                        _sys_bp_count, _MAX_BREAKPOINTS,
                    )
                system_blocks: list[dict[str, Any]] = []
                for i, part in enumerate(raw_parts):
                    block: dict[str, Any] = {"type": "text", "text": part}
                    if i < len(raw_parts) - 1 and i < _MAX_BREAKPOINTS - 1:  # -1 for tools
                        block["cache_control"] = {"type": "ephemeral"}
                    system_blocks.append(block)
                AnthropicLLM._mark_history_cache_breakpoint(converted)
                return system_blocks, converted
            # Fewer than 2 effective parts after strip — fall through.
            system_text = raw_parts[0] if raw_parts else system_text

        system_blocks = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]
        AnthropicLLM._mark_history_cache_breakpoint(converted)
        return system_blocks, converted

    @staticmethod
    def _repair_tool_pairing(
        converted: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Make ``converted`` satisfy Anthropic's pairing rules:

        1. Every ``tool_use`` id in an assistant message must have a
           matching ``tool_result`` in the IMMEDIATELY following user
           message. (Our converter emits each tool result as its own
           user message, so multi-tool turns need merging first.)
        2. A ``tool_result`` may only appear right after the assistant
           message that issued its ``tool_use``.

        Repairs, in order:
        * merge runs of consecutive user messages that are pure
          tool_result lists into one user message;
        * synthesize placeholder tool_results for tool_use ids with no
          result (crashed turn / pruned history / provider switch);
        * downgrade orphan tool_results (no matching tool_use right
          before them) to plain text blocks.
        """
        # Pass 1: merge consecutive pure-tool_result user messages.
        def _is_result_msg(msg: dict[str, Any]) -> bool:
            c = msg.get("content")
            return (
                msg.get("role") == "user"
                and isinstance(c, list) and bool(c)
                and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in c
                )
            )

        merged: list[dict[str, Any]] = []
        for msg in converted:
            if merged and _is_result_msg(msg) and _is_result_msg(merged[-1]):
                merged[-1] = {
                    "role": "user",
                    "content": list(merged[-1]["content"]) + list(msg["content"]),
                }
            else:
                merged.append(msg)

        # Pass 2: walk pairs; fix both directions.
        out: list[dict[str, Any]] = []
        pending_use_ids: list[str] = []  # ids issued by the assistant msg just appended
        for msg in merged:
            content = msg.get("content")
            if msg.get("role") == "user" and isinstance(content, list):
                fixed_blocks: list[dict[str, Any]] = []
                consumed: set[str] = set()
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id") or ""
                        if tid in pending_use_ids and tid not in consumed:
                            consumed.add(tid)
                            fixed_blocks.append(b)
                        else:
                            # Orphan result — downgrade to text.
                            raw = b.get("content")
                            txt = raw if isinstance(raw, str) else str(raw)
                            fixed_blocks.append({
                                "type": "text",
                                "text": "[tool result]\n" + (txt or "(empty)"),
                            })
                    else:
                        fixed_blocks.append(b)
                # Synthesize placeholders for any tool_use the result
                # message failed to cover.
                missing = [t for t in pending_use_ids if t not in consumed]
                if missing:
                    fixed_blocks = [{
                        "type": "tool_result",
                        "tool_use_id": t,
                        "content": (
                            "[tool result unavailable — lost from "
                            "history (interrupted turn or model "
                            "switch). Re-run the tool if needed.]"
                        ),
                    } for t in missing] + fixed_blocks
                pending_use_ids = []
                out.append({**msg, "content": fixed_blocks})
            else:
                # Non-result message while tool_use ids are pending →
                # the results are missing entirely; insert a synthetic
                # user message carrying placeholders BEFORE this one.
                if pending_use_ids:
                    out.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": t,
                            "content": (
                                "[tool result unavailable — lost from "
                                "history (interrupted turn or model "
                                "switch). Re-run the tool if needed.]"
                            ),
                        } for t in pending_use_ids],
                    })
                    pending_use_ids = []
                out.append(msg)
                if msg.get("role") == "assistant" and isinstance(content, list):
                    pending_use_ids = [
                        b.get("id") for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("id")
                    ]
        # Trailing assistant tool_use with no following message at all.
        if pending_use_ids:
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": t,
                    "content": (
                        "[tool result unavailable — lost from history "
                        "(interrupted turn or model switch). Re-run the "
                        "tool if needed.]"
                    ),
                } for t in pending_use_ids],
            })
        return out

    @staticmethod
    def _mark_history_cache_breakpoint(
        converted: list[dict[str, Any]],
    ) -> None:
        """Wave-30 (2026-05-18): add a cache_control breakpoint to the
        LAST message so the whole ``prior history`` prefix is cached.

        Pre-fix: cache_control was only on system + tools (~10K tokens).
        prior history (~28K tokens on a 5-turn session) was re-sent
        in full on every LLM call. Anthropic does NOT auto-cache
        messages — it only caches positions explicitly marked with
        cache_control. So the bulk of every request was billed at
        full input rate.

        Anthropic budgets 4 cache breakpoints per request. We use 3
        on system + tools (system frozen, system autobio, tools
        boundary); this fourth one covers the entire message
        history up through the most recent turn.

        Marker mechanics: cache_control on a content block means
        "the cumulative prefix INCLUDING this block is potentially
        cached." So marking the LAST message captures everything
        before it as a cache write on the first hop, and every
        subsequent hop (whose prefix is the previous hop's
        messages + new assistant/tool entries) gets a partial
        cache hit on the unchanged prefix.

        Implementation note: cache_control rides on a content
        BLOCK, not on the message envelope. When the message's
        content is a plain string we wrap it; when it's already
        a block list we tag the last block.
        """
        if not converted:
            return
        last = converted[-1]
        content = last.get("content")
        if isinstance(content, str):
            if not content:
                return  # empty content can't carry cache_control
            last["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
            return
        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict):
                # tool_result / image / text — Anthropic accepts
                # cache_control on any of them.
                last_block["cache_control"] = {"type": "ephemeral"}

    @staticmethod
    def _tools_to_anthropic(tools: list[ToolSpec] | None) -> list[dict[str, Any]]:
        # B-245: cache the tools array. Marking cache_control on a
        # tool sets a cache breakpoint that includes every preceding
        # tool def in one cache slot. Empty list returns empty (no
        # breakpoint).
        #
        # Wave-30 fix (2026-05-18): pre-fix marked the LAST tool,
        # which was wrong post-B-238. The B-238 skill prefilter
        # appends a fresh top-K subset of ``skill_*`` tools to the
        # END of the array on every turn, keyed by the user's
        # message. So the last tool was ALWAYS the most volatile —
        # cache slot got invalidated every single turn. Move the
        # breakpoint to the boundary BEFORE the first ``skill_*``:
        # the stable workhorses (file_read, bash, web_fetch,
        # browser_*, todo_*, etc.) sit at indices [0..N), the
        # prefilter skills at [N..end). Cache up through N-1.
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
        # First index where a skill_* tool appears. Everything before
        # it is stable across turns; everything from there on is the
        # per-turn prefilter output.
        first_skill_idx: int | None = None
        for i, t in enumerate(tools):
            if (t.name or "").startswith("skill_"):
                first_skill_idx = i
                break
        # Place breakpoint on the LAST stable tool (or the last tool
        # overall when there are no skill_* tools).
        boundary = (
            first_skill_idx - 1 if first_skill_idx is not None and first_skill_idx > 0
            else len(out) - 1
        )
        if 0 <= boundary < len(out):
            out[boundary]["cache_control"] = {"type": "ephemeral"}
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
            "max_tokens": self.max_tokens,
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
            "max_tokens": self.max_tokens,
        }
        if system:
            kwargs["system"] = system
        tool_defs = self._tools_to_anthropic(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs

        t0 = time.perf_counter()
        # Exponential backoff for transient API errors (audit 2026-06-11).
        # 429 rate-limit and 529 overload are retried up to 3 times;
        # non-transient errors (auth, bad request, model_not_found)
        # propagate immediately.
        _max_retries = 3
        _base_delay = 1.0
        for _attempt in range(_max_retries + 1):
            try:
                response = await client.messages.create(**kwargs)
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
                _log.warning(
                    "anthropic.retry attempt=%d/%d delay=%.1fs err=%s",
                    _attempt + 1, _max_retries, _delay, _etype,
                )
                await asyncio.sleep(_delay)
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
        on_tool_block: Any | None = None,  # Wave-32+ Speculation
        on_stream_fallback: Any | None = None,  # 2026-05-30
        cancel: asyncio.Event | None = None,
    ) -> LLMResponse:
        from xmclaw.providers.llm.translators import anthropic_native as translator
        # Wave-32+ Speculation: per-stream cache of partial tool_use
        # blocks. Keyed by content-block index → {id, name, json_buf}.
        # Finalised + fired through ``on_tool_block`` on each
        # ``content_block_stop`` event.
        _spec_blocks: dict[int, dict[str, Any]] = {}

        system, anthropic_messages = self._messages_to_anthropic(messages)
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": self.max_tokens,
        }
        if system:
            kwargs["system"] = system
        tool_defs = self._tools_to_anthropic(tools)
        if tool_defs:
            kwargs["tools"] = tool_defs
        # B-216: optionally request extended thinking (audit 2026-06-11:
        # was dead code; _extended_thinking is now initialised in __init__).
        if self._extended_thinking:
            kwargs["max_tokens"] = max(int(kwargs.get("max_tokens", 4096)), 8192)
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": 5000,
            }

        text_parts: list[str] = []
        cancelled = False
        t0 = time.perf_counter()

        # B-270 (reverted 2026-05-24): the prefix-based heuristic
        # separator was too fragile — any LLM reply opening with
        # "用户/我需要/让我..." was misrouted to thinking channel
        # and the user saw an empty bubble. The right fix for Kimi's
        # plain-text reasoning leak is the system-prompt-side ``think``
        # tool guidance added in prompt_builder._default_system_prompt
        # (which tells the model to route reasoning through the tool
        # instead of inlining it). Heuristic removed; structural fix
        # carries the day.
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
                    # Wave-32+ Speculation: track tool_use blocks as
                    # they arrive so we can fire on_tool_block on
                    # content_block_stop. Three event types matter:
                    #   * content_block_start with type=tool_use →
                    #     stash {id, name} keyed by index
                    #   * content_block_delta with type=input_json_delta
                    #     → append partial_json to the buffer
                    #   * content_block_stop → finalise + fire callback
                    if etype == "content_block_start" and on_tool_block is not None:
                        block_obj = getattr(event, "content_block", None)
                        if block_obj is not None and getattr(block_obj, "type", None) == "tool_use":
                            idx = getattr(event, "index", -1)
                            _spec_blocks[idx] = {
                                "id": getattr(block_obj, "id", "") or "",
                                "name": getattr(block_obj, "name", "") or "",
                                "json_buf": "",
                            }
                    if etype == "content_block_stop" and on_tool_block is not None:
                        idx = getattr(event, "index", -1)
                        block_state = _spec_blocks.pop(idx, None)
                        if block_state and block_state["name"]:
                            try:
                                import json as _json
                                args = (
                                    _json.loads(block_state["json_buf"])
                                    if block_state["json_buf"].strip()
                                    else {}
                                )
                                from xmclaw.core.ir import ToolCall as _TC
                                _tc = _TC(
                                    id=block_state["id"],
                                    name=block_state["name"],
                                    args=args,
                                    provenance="llm",
                                )
                                on_tool_block(_tc)
                            except Exception:  # noqa: BLE001 — never break streaming
                                pass
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
                    elif delta_type == "input_json_delta" and on_tool_block is not None:
                        # Append to the per-block JSON buffer; the
                        # finalise step on content_block_stop parses it.
                        idx = getattr(event, "index", -1)
                        block_state = _spec_blocks.get(idx)
                        if block_state is not None:
                            block_state["json_buf"] += (
                                getattr(delta_obj, "partial_json", "") or ""
                            )
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
            # 2026-05-30: tag the fallback so agent_loop can publish a
            # UI notice — "considered high risk" / shim-no-stream looks
            # like a hang to the user otherwise (no token drip for 30s+).
            _msg = str(exc)[:300]
            _reason = (
                "risk_reject" if "high risk" in _msg
                else "shim_no_stream"
            )
            # Fire the immediate UI notice BEFORE blocking on complete() —
            # that's the whole point: tell the user "no token drip this
            # turn" while the non-streaming reply is still in flight, not
            # after it finishes 30s later.
            if on_stream_fallback is not None:
                try:
                    await on_stream_fallback(_reason)
                except Exception:  # noqa: BLE001 — never break the fallback path
                    pass
            _fallback = await self.complete(messages, tools)
            try:
                import dataclasses as _dc
                return _dc.replace(
                    _fallback,
                    stream_fallback=True,
                    stream_fallback_reason=_reason,
                )
            except Exception:  # noqa: BLE001
                return _fallback
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
        _log.debug("_img_to_anthropic_block: empty src")
        return None
    if src.startswith("data:"):
        # data URL — parse media type + base64 payload back out.
        try:
            header, b64 = src.split(",", 1)
            media_type = header.split(";")[0][len("data:"):]
            _log.debug("_img_to_anthropic_block: data URL media_type=%s", media_type)
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type or "image/png",
                    "data": b64,
                },
            }
        except Exception as _exc:  # noqa: BLE001
            _log.debug("_img_to_anthropic_block: data URL parse failed: %s", _exc)
            return None
    p = Path(src)
    if not p.is_file():
        _log.debug("_img_to_anthropic_block: not a file: %s", src)
        return None
    _log.debug("_img_to_anthropic_block: processing %s", src)
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
        _log.debug("_img_to_anthropic_block: resized %s → %d bytes b64", src, len(b64))
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        }
    except Exception as _exc:  # noqa: BLE001
        _log.debug("_img_to_anthropic_block: PIL failed for %s: %s", src, _exc)
        # Last-ditch: ship original bytes, no resize.
        try:
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            _log.debug("_img_to_anthropic_block: fallback raw bytes %s → %d bytes", src, len(raw))
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
        except Exception as _exc2:  # noqa: BLE001
            _log.warning("_img_to_anthropic_block: fallback failed for %s: %s", src, _exc2)
            return None
