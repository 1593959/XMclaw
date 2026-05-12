"""Hop loop mixin for AgentLoop.

Extracted from agent_loop.py to reduce module size.
Contains the LLM ↔ tool hop loop execution logic.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.cognition.goal_anchor import (
    GoalAnchorState,
    GoalAnchorTracker,
)
from xmclaw.core.bus import BehavioralEvent, EventType
from xmclaw.core.ir.toolcall import ToolSpec
from xmclaw.daemon.history_utils import _is_transient_tool_error
from xmclaw.daemon.turn_types import AgentTurnResult, _log_memory_failure
from xmclaw.providers.llm.base import Message
from xmclaw.security import SOURCE_TOOL_RESULT, apply_policy
from xmclaw.utils.cost import BudgetExceeded


class HopLoopMixin:
    """Provides the LLM ↔ tool hop loop."""

    async def _invoke_single_tool(
        self, call: Any, effective_tools: Any, session_id: str,
    ) -> Any:
        """Invoke one tool with defensive error handling and retry.

        Returns the raw ``ToolResult``. Event publishing and loop-state
        mutation are the caller's responsibility so that multiple calls
        can be executed in parallel.
        """
        import dataclasses as _dc
        call_with_sid = _dc.replace(call, session_id=session_id)
        try:
            result = await effective_tools.invoke(call_with_sid)
        except Exception as _invoke_exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger as _gl
            _gl(__name__).warning(
                "tool.invoke_uncaught_exception tool=%s err=%s",
                call.name, _invoke_exc,
            )
            from xmclaw.core.ir import ToolResult as _ToolResult
            result = _ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=(
                    f"{type(_invoke_exc).__name__}: {_invoke_exc} "
                    f"(uncaught — ToolProvider contract violation; "
                    f"the tool's ``invoke`` should have returned "
                    f"a failed ToolResult instead of raising)"
                ),
            )
        # B-17: retry once on transient failures.
        if not result.ok and result.error and _is_transient_tool_error(result.error):
            import asyncio as _asyncio
            await _asyncio.sleep(0.5)
            try:
                retry = await effective_tools.invoke(call_with_sid)
            except Exception as _retry_exc:  # noqa: BLE001
                from xmclaw.core.ir import ToolResult as _ToolResult
                retry = _ToolResult(
                    call_id=call.id,
                    ok=False,
                    content=None,
                    error=(
                        f"{type(_retry_exc).__name__}: {_retry_exc} "
                        f"(retry also raised uncaught — "
                        f"ToolProvider contract violation)"
                    ),
                )
            if retry.ok:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).info(
                    "tool.retry_succeeded tool=%s first_error=%s",
                    call.name, (result.error or "")[:120],
                )
                result = retry
        return result

    async def _run_hop_loop(
        self, *,
        session_id: str,
        user_message: str,
        llm_profile_id: str | None,
        cancel_event: asyncio.Event,
        effective_tools: "Any | None",
        llm: Any,
        messages: list[Message],
        tool_specs: list[ToolSpec],
        publish: "Callable[..., Awaitable[BehavioralEvent]]",
        events: list[BehavioralEvent],
        tool_calls_made: list[dict[str, Any]],
        turn_uuid: str,
    ) -> AgentTurnResult | None:
        """Execute the LLM ↔ tool hop loop.

        Returns a result when the loop terminates naturally
        (assistant text without tool calls, cancelled, stuck, etc).
        Returns ``None`` when max_hops is reached — caller must
        synthesise the truncation message.
        """
        _stuck_loop_deque: list[tuple[str, str]] = []
        _STUCK_LOOP_THRESHOLD = 3
        _NO_PROGRESS_THRESHOLD = 5
        _no_progress_counter = 0

        # 2026-05-12 Batch A.1: GoalAnchor — runtime trick to make
        # weak/short-context models do long tool chains. Every N hops
        # we inject a synthesised reminder of the original goal + tools
        # called so far + remaining hop budget. Pulled from Kimi K2.6
        # agent mode's "200-300 steps without drift" pattern — moved
        # from model weights to runtime scaffolding so any LLM benefits.
        # Tracker is stateless; per-call snapshot built fresh each anchor.
        _goal_anchor_tracker = GoalAnchorTracker(
            anchor_every=int(getattr(self, "_goal_anchor_every", 5)),
        )
        # 2026-05-12 Batch C.2: StepValidator — optional per-step
        # "did this advance the goal" check. Off by default (opt-in
        # via config tools.step_validator.enabled). When on, each
        # successful tool call emits an INNER_MONOLOGUE verdict chip.
        _step_validator = getattr(self, "_step_validator", None)

        for hop in range(self._max_hops):
            hop_corr = f"{turn_uuid}-{hop}"
            # B-38: cancel fence — if the user clicked Stop, bail out
            # cleanly before doing more LLM/tool work. Checked AT
            # HOP BOUNDARIES (cheap, doesn't interrupt in-flight
            # streams). The event is cleared by run_turn's outer
            # try/finally so subsequent turns start fresh.
            if cancel_event.is_set():
                await publish(EventType.ANTI_REQ_VIOLATION, {
                    "message": "turn cancelled by user",
                    "kind": "cancelled",
                    "hop": hop,
                })
                return AgentTurnResult(
                    ok=False, text="", hops=hop,
                    tool_calls=tool_calls_made,
                    events=events,
                    error="cancelled",
                )
            # Anti-req #6: check the hard budget cap BEFORE the LLM call.
            # If we've already exceeded, abort with an
            # ANTI_REQ_VIOLATION event — never swallow, never partial.
            if self._cost_tracker is not None:
                try:
                    self._cost_tracker.check_budget()
                except BudgetExceeded as exc:
                    await publish(EventType.ANTI_REQ_VIOLATION, {
                        "message": f"budget exceeded: {exc}",
                        "kind": "budget_exceeded",
                        "spent_usd": self._cost_tracker.spent_usd,
                        "budget_usd": self._cost_tracker.budget_usd,
                        "hop": hop,
                    })
                    return AgentTurnResult(
                        ok=False, text="", hops=hop,
                        tool_calls=tool_calls_made,
                        events=events,
                        error=f"budget_exceeded: {exc}",
                    )

            # 2026-05-12 Batch A.1: GoalAnchor injection. Every N hops
            # (default 5), append a synthesised reminder of the original
            # goal + progress so the LLM doesn't drift on long chains.
            # Carries the ``[GOAL-ANCHOR]`` marker that turn_context.py's
            # sanitiser strips before history-to-disk persistence.
            if _goal_anchor_tracker.should_anchor(hop):
                anchor_text = _goal_anchor_tracker.format(GoalAnchorState(
                    original_goal=user_message,
                    hop=hop,
                    max_hops=self._max_hops,
                    tool_calls_made=tool_calls_made,
                    plan_steps=getattr(self, "_active_plan_steps", None),
                    completed_step_indices=getattr(
                        self, "_active_plan_completed", None,
                    ),
                    open_errors=[
                        str(tc.get("error", ""))
                        for tc in tool_calls_made[-10:]
                        if not tc.get("ok", True) and tc.get("error")
                    ] or None,
                ))
                messages.append(Message(role="user", content=anchor_text))
                await publish(EventType.INNER_MONOLOGUE, {
                    "hop": hop,
                    "kind": "goal_anchor_injected",
                    "anchor_len": len(anchor_text),
                }, correlation_id=hop_corr)

            # 2. LLM request event (messages_hash is a cheap fingerprint
            # so the bus consumer can distinguish different hops).
            await publish(EventType.LLM_REQUEST, {
                "model": getattr(llm, "model", None),
                "hop": hop,
                "messages_count": len(messages),
                "tools_count": len(tool_specs) if tool_specs else 0,
                "llm_profile_id": llm_profile_id,
            })

            # Streaming: each text delta becomes an LLM_CHUNK so the WS
            # client can render the assistant text token-by-token. Tool-use
            # blocks aren't streamed; they arrive in the final response.
            chunk_seq = 0
            think_seq = 0

            async def _emit_chunk(delta: str) -> None:
                nonlocal chunk_seq
                await publish(EventType.LLM_CHUNK, {
                    "hop": hop,
                    "delta": delta,
                    "seq": chunk_seq,
                }, correlation_id=hop_corr)
                chunk_seq += 1

            # B-91: separate channel for reasoning / extended-thinking
            # deltas. PhaseCard accumulates these into ``message.thinking``
            # and shows them in its body when expanded. Distinct event
            # type from LLM_CHUNK so the chat reducer can route them to
            # the right slot without sniffing content.
            async def _emit_thinking_chunk(delta: str) -> None:
                nonlocal think_seq
                await publish(EventType.LLM_THINKING_CHUNK, {
                    "hop": hop,
                    "delta": delta,
                    "seq": think_seq,
                }, correlation_id=hop_corr)
                think_seq += 1

            t0 = time.perf_counter()
            try:
                # B-39: pass the per-session cancel event so streaming
                # providers (Anthropic / OpenAI) can bail mid-chunk
                # when the user clicks Stop, instead of waiting for
                # the next hop boundary. Falls back gracefully on
                # providers that ignore the kwarg.
                # B-91: also pass the thinking-chunk callback. Providers
                # that don't support reasoning streams ignore the kwarg
                # via the base-class default impl.
                # B-189: wall-clock timeout. Without this a hung
                # provider call (network stall / model loop) blocks
                # the turn forever — chat-59bb7a7a went silent for 10
                # minutes after a hop-6 stall before the user nudged.
                # B-227: classify-and-retry around LLM call. Pre-B-227
                # any provider exception killed the turn outright;
                # ~10% of real-data failures were transient
                # rate_limit / overloaded that succeed on retry.
                # Reasons that should be retried get a per-reason
                # backoff schedule from ``backoff_schedule``.
                from xmclaw.utils.error_classifier import (
                    classify_api_error, backoff_schedule,
                )
                # P0-1: proactive compression — once per hop, BEFORE
                # the LLM call. Cheap when threshold not breached
                # (token estimate + comparison). When breached it
                # runs the 5-phase pipeline (prune → head/tail
                # protect → LLM summary → assemble + sanitize).
                # Replaces the simple greedy-drop in _persist_history
                # for the "context too long" case while keeping that
                # path as the fallback in case compression fails.
                _did_compress, _did_emit = False, False
                try:
                    _new_msgs, _did_compress = await self._maybe_compress_messages(
                        messages, session_id,
                    )
                    if _did_compress:
                        messages = _new_msgs
                        await publish(EventType.CONTEXT_COMPRESSED, {
                            "hop": hop,
                            "trigger": "proactive_threshold",
                            "session_id": session_id,
                        })
                        _did_emit = True
                except Exception:  # noqa: BLE001
                    pass

                _b227_attempts = 0
                _b227_last_classified: Any = None
                # B-230: auto-continue when ``stop_reason=max_tokens``
                # cuts the response mid-output. Without this the user
                # has to type "继续" and the LLM tends to RESTART from
                # scratch instead of appending — losing the partial
                # output and burning tokens. We append the partial
                # assistant text + a continuation prompt and re-call
                # up to N times before giving up.
                _B230_MAX_CONTINUES = 3
                _b230_continue_count = 0
                _b230_acc_content = ""
                while True:  # outer = B-230 auto-continue
                    while True:  # inner = B-227 classify-and-retry
                        try:
                            response = await asyncio.wait_for(
                                llm.complete_streaming(
                                    messages, tools=tool_specs, on_chunk=_emit_chunk,
                                    on_thinking_chunk=_emit_thinking_chunk,
                                    cancel=cancel_event,
                                ),
                                timeout=self._llm_timeout_s,
                            )
                            break  # inner: success
                        except asyncio.TimeoutError:
                            # Re-raise into the original timeout handler
                            # below (separate path with its own user msg).
                            raise
                        except Exception as _exc:  # noqa: BLE001
                            ce = classify_api_error(
                                _exc,
                                provider=getattr(llm, "__class__", type(llm)).__name__,
                                model=getattr(llm, "model", "") or "",
                            )
                            _b227_last_classified = ce
                            schedule = backoff_schedule(ce.reason)
                            if (
                                not ce.retryable
                                or _b227_attempts >= len(schedule)
                                or cancel_event.is_set()
                            ):
                                # Out of retries (or non-retryable) — let
                                # the outer except path surface the error
                                # in LLM_RESPONSE with the classified
                                # reason as category.
                                raise
                            sleep_ms = schedule[_b227_attempts]
                            try:
                                from xmclaw.utils.log import get_logger
                                get_logger(__name__).warning(
                                    "agent_loop.llm_retry hop=%d reason=%s "
                                    "attempt=%d sleep_ms=%d msg=%s",
                                    hop, ce.reason.value, _b227_attempts + 1,
                                    sleep_ms, ce.message[:120],
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            # P0-1 + P0-2 wire-up: when classifier flagged
                            # context_overflow / payload_too_large /
                            # long_context_tier (all set should_compress),
                            # actually run the compressor before sleeping
                            # + retrying. Without this the retry sends the
                            # same too-big payload again and just dies the
                            # same way. Force=True bypasses the threshold
                            # check — provider already told us it's too big.
                            if ce.should_compress:
                                try:
                                    _new_msgs, _did_force = await self._maybe_compress_messages(
                                        messages, session_id, force=True,
                                    )
                                    if _did_force:
                                        messages = _new_msgs
                                        await publish(EventType.CONTEXT_COMPRESSED, {
                                            "hop": hop,
                                            "trigger": "reactive_" + ce.reason.value,
                                            "session_id": session_id,
                                        })
                                except Exception:  # noqa: BLE001
                                    pass
                            await asyncio.sleep(sleep_ms / 1000.0)
                            _b227_attempts += 1
                            # Loop and retry with same kwargs.

                    # B-230: did the response get truncated by max_tokens?
                    # Anthropic emits ``stop_reason="max_tokens"``;
                    # OpenAI emits ``finish_reason="length"`` (forwarded
                    # as stop_reason by our wrapper). Auto-continue ONLY
                    # when there are no tool_calls (those finish a
                    # different way) and content is non-trivial (>50
                    # chars — small partial likely indicates a different
                    # failure mode).
                    _stop = (response.stop_reason or "").lower()
                    truncated = _stop in ("max_tokens", "length")
                    if (
                        truncated
                        and not response.tool_calls
                        and response.content
                        and len(response.content) > 50
                        and _b230_continue_count < _B230_MAX_CONTINUES
                        and not cancel_event.is_set()
                    ):
                        _b230_acc_content += response.content
                        messages = list(messages) + [
                            Message(role="assistant", content=_b230_acc_content),
                            Message(
                                role="user",
                                content=(
                                    "[B-230 auto-continue] Your previous "
                                    "reply was truncated by max_tokens. "
                                    "Continue from EXACTLY where you "
                                    "stopped — do NOT repeat anything "
                                    "you've already written, just append "
                                    "the rest."
                                ),
                            ),
                        ]
                        _b230_continue_count += 1
                        _b227_attempts = 0  # reset per-call retry budget
                        try:
                            from xmclaw.utils.log import get_logger
                            get_logger(__name__).info(
                                "agent_loop.b230_auto_continue "
                                "session=%s hop=%d count=%d acc_chars=%d",
                                session_id, hop, _b230_continue_count,
                                len(_b230_acc_content),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        continue  # outer: re-issue LLM call

                    # Done. Merge accumulated content (if any) into the
                    # final response so persisted history reflects the
                    # whole answer rather than just the last chunk.
                    if _b230_acc_content:
                        import dataclasses as _dc
                        response = _dc.replace(
                            response,
                            content=_b230_acc_content + response.content,
                        )
                    break  # outer: real success
            except asyncio.TimeoutError:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                # Tell the bus + the user clearly. The ANTI_REQ event
                # surfaces in events.db / Trace; the LLM_RESPONSE
                # carries the visible error text the chat UI renders.
                await publish(EventType.ANTI_REQ_VIOLATION, {
                    "message": (
                        f"LLM provider call exceeded "
                        f"{self._llm_timeout_s:.0f}s wall-clock at hop {hop} "
                        "— aborting turn rather than blocking forever."
                    ),
                    "hop": hop,
                    "category": "llm_timeout",
                })
                err = (
                    f"LLM call timed out after {self._llm_timeout_s:.0f}s "
                    "(hop {hop}). Provider may be overloaded or stuck."
                ).format(hop=hop)
                await publish(EventType.LLM_RESPONSE, {
                    "hop": hop, "ok": False, "error": err,
                    "latency_ms": latency_ms,
                }, correlation_id=hop_corr)
                return AgentTurnResult(
                    ok=False, text="", hops=hop + 1,
                    tool_calls=tool_calls_made, events=events, error=err,
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.perf_counter() - t0) * 1000.0
                await publish(EventType.LLM_RESPONSE, {
                    "hop": hop,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": latency_ms,
                }, correlation_id=hop_corr)
                return AgentTurnResult(
                    ok=False, text="", hops=hop + 1,
                    tool_calls=tool_calls_made,
                    events=events,
                    error=f"{type(exc).__name__}: {exc}",
                )

            latency_ms = (time.perf_counter() - t0) * 1000.0
            await publish(EventType.LLM_RESPONSE, {
                "hop": hop,
                "ok": True,
                # ``content`` carries the model's actual text. Emitted in
                # every LLM_RESPONSE so the WS client (e.g. the chat
                # REPL) can render the assistant text without a second
                # round-trip. Intermediate-hop content (before a tool
                # call) is usually short or empty; terminal hops carry
                # the full answer.
                "content": response.content,
                "content_length": len(response.content),
                "tool_calls_count": len(response.tool_calls),
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "latency_ms": latency_ms,
            }, correlation_id=hop_corr)

            # B-233: feed the GROUND-TRUTH prompt_tokens to the
            # compressor's per-session state. The threshold check then
            # uses ``max(estimate, last_actual)`` so kimi sessions whose
            # CJK content under-counts via chars/4 still trigger
            # proactive compression at the right moment. Best-effort —
            # the compressor isn't required for the loop to function.
            if response.prompt_tokens > 0 and self._compressor is not None:
                try:
                    self._compressor.update_from_response(
                        response.prompt_tokens, session_id=session_id,
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Anti-req #6 cont'd: record the call's usage against the
            # budget right after we see it. check_budget on the NEXT
            # hop will block if we crossed the cap during this one.
            if self._cost_tracker is not None:
                cost = self._cost_tracker.record(
                    provider=getattr(llm, "__class__", type(llm)).__name__,
                    model=getattr(llm, "model", "") or "",
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
                # B-316: surface Anthropic prompt-cache stats on
                # COST_TICK so the UI can show hit-rate (and so users
                # see whether B-245 is actually saving them money).
                # Falls back to 0 for OpenAI / Kimi / etc. (no cache
                # support yet) — UI distinguishes via numeric > 0.
                _cache_creation = int(getattr(
                    response, "cache_creation_input_tokens", 0,
                ) or 0)
                _cache_read = int(getattr(
                    response, "cache_read_input_tokens", 0,
                ) or 0)
                await publish(EventType.COST_TICK, {
                    "hop": hop,
                    "cost_usd": cost,
                    "spent_usd": self._cost_tracker.spent_usd,
                    "budget_usd": self._cost_tracker.budget_usd,
                    "remaining_usd": self._cost_tracker.remaining_usd,
                    # B-107: surface per-call token counts so the Web UI
                    # can render a live "tokens this turn" widget without
                    # synthesising it from chunk events.
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "model": getattr(llm, "model", "") or "",
                    # B-316: cache stats (Anthropic only; 0 elsewhere).
                    "cache_creation_input_tokens": _cache_creation,
                    "cache_read_input_tokens": _cache_read,
                })

            # 3. If the model made tool calls, execute them and feed
            # results back into the conversation.
            if response.tool_calls:
                if effective_tools is None:
                    # Model hallucinated a tool call but we have no
                    # provider — record as anti-req violation and end.
                    await publish(EventType.ANTI_REQ_VIOLATION, {
                        "message": "model emitted tool_calls but no ToolProvider wired",
                        "hop": hop,
                    })
                    return AgentTurnResult(
                        ok=False, text=response.content,
                        hops=hop + 1, tool_calls=tool_calls_made,
                        events=events,
                        error="tool call without provider",
                    )

                # Record the assistant turn (text + tool_calls together).
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                # Phase A: emit start events (serial, lightweight).
                for call in response.tool_calls:
                    await publish(EventType.TOOL_CALL_EMITTED, {
                        "call_id": call.id,
                        "name": call.name,
                        "args": call.args,
                        "provenance": call.provenance,
                    })
                    await publish(EventType.TOOL_INVOCATION_STARTED, {
                        "call_id": call.id, "name": call.name,
                    })

                # Phase B: invoke all tools in parallel.
                _invoke_tasks = [
                    self._invoke_single_tool(call, effective_tools, session_id)
                    for call in response.tool_calls
                ]
                _invoke_results = await asyncio.gather(*_invoke_tasks)

                # Phase C: process results in original order (serial).
                _had_success_this_hop = False
                # B-Vision: collect ``metadata.attach_image`` paths from
                # any tools that produced screenshots, so we can inject
                # a single multimodal user message AFTER the batch (the
                # OpenAI API requires all tool_call responses back-to-
                # back; interleaving user messages breaks the contract).
                _vision_attachments: list[str] = []
                for call, result in zip(response.tool_calls, _invoke_results):
                    # After todo tool runs, surface TODO_UPDATED so the UI
                    # can live-render the panel. We detect this here to
                    # keep BuiltinTools decoupled from the bus.
                    if call.name == "todo_write" and result.ok:
                        items = call.args.get("items")
                        if isinstance(items, list):
                            await publish(EventType.TODO_UPDATED, {
                                "items": items,
                                "count": len(items),
                            })
                    finished_event = await publish(
                        EventType.TOOL_INVOCATION_FINISHED, {
                            "call_id": result.call_id,
                            "name": call.name,
                            "result": result.content,
                            "error": result.error,
                            "latency_ms": result.latency_ms,
                            "expected_side_effects": list(result.side_effects),
                            "ok": result.ok,
                        },
                    )

                    # Epic #24 Phase 1: HonestGrader runs on the
                    # finished event and publishes a paired
                    # GRADER_VERDICT for downstream subscribers
                    # (EvolutionAgent observer aggregates per
                    # (skill_id, version) and proposes promotions).
                    # Failures here MUST NOT block the tool loop —
                    # the agent's main path keeps going regardless.
                    #
                    # Phase 1.5: when the tool is a skill bridged
                    # through SkillToolProvider (name prefix
                    # ``skill_``, with ``__`` reversed back to ``.``
                    # for the namespace separator), pull the skill_id
                    # + HEAD version off the orchestrator's registry
                    # and stamp them on the verdict — without this,
                    # the observer's `_ingest` immediately returns and
                    # the entire evolution feedback loop is silently
                    # empty. Non-skill tools (bash / file_read / etc.)
                    # still emit the verdict but skip the registry
                    # lookup; observer treats them as unkeyed and
                    # ignores them, which is the correct semantics
                    # (no skill version to evolve).
                    try:
                        verdict = await self._grader.grade(finished_event)
                        verdict_payload: dict[str, Any] = {
                            "call_id": result.call_id,
                            "tool_name": call.name,
                            "score": verdict.score,
                            "ran": verdict.ran,
                            "returned": verdict.returned,
                            "type_matched": verdict.type_matched,
                            "side_effect_observable": verdict.side_effect_observable,
                            "evidence": list(verdict.evidence),
                        }
                        # B-299: ``skill_browse`` is the synthesised
                        # meta-discovery tool, NOT a registry-backed
                        # skill — its invocations carry no skill_id
                        # signal. If we let the generic skill_-prefix
                        # branch below stamp ``skill_id="browse"`` on
                        # the verdict, EvolutionAgent's _ingest would
                        # accumulate plays/EWMA for a phantom
                        # "browse" arm, and VariantSelector would
                        # eventually try to UCB1-select a version
                        # for a skill that doesn't exist. Skip the
                        # stamping; the verdict still publishes
                        # (other observers may want it) but lands
                        # un-keyed so EvolutionAgent's early-return-
                        # on-missing-skill_id path drops it.
                        if (
                            call.name.startswith("skill_")
                            and call.name != "skill_browse"
                        ):
                            # Reverse SkillToolProvider's mapping
                            # (xmclaw/skills/tool_bridge.py:_to_tool_name).
                            # ``__`` was the namespace-separator escape
                            # for ``.`` — restore it. Other invalid
                            # chars were squashed to ``_`` and aren't
                            # reversible, but skill_ids that survive
                            # the round-trip 1:1 are the common case
                            # (snake_case + dotted namespace).
                            sid = call.name[len("skill_"):].replace("__", ".")
                            verdict_payload["skill_id"] = sid
                            # B-295: read the actual dispatched version from
                            # the ToolResult.metadata side-channel SkillToolProvider
                            # populates. With VariantSelector wired this is the
                            # version UCB1 picked for THIS call (HEAD or candidate);
                            # without selector wired it's still HEAD's version
                            # number (vs the legacy hardcoded 0). EvolutionAgent +
                            # VariantSelector both bucket by (skill_id, version)
                            # so this is what closes the bandit feedback loop.
                            md = getattr(result, "metadata", {}) or {}
                            verdict_payload["version"] = int(md.get("skill_version", 0))
                        await publish(EventType.GRADER_VERDICT, verdict_payload)
                    except Exception:  # noqa: BLE001 — observability
                        # never blocks execution; bus subscribers see
                        # gaps instead of crashes.
                        pass

                    # Jarvisification: record tool usage for evolution.
                    _evo_loop = getattr(self, "_evolution_loop", None)
                    if _evo_loop is not None:
                        try:
                            _evo_loop.record_tool_call(call.name)
                            if not result.ok:
                                _evo_loop.record_failure(
                                    context=f"tool:{call.name}",
                                    error=result.error or "unknown",
                                    recovery="retry_or_fallback",
                                )
                        except Exception:  # noqa: BLE001
                            pass

                    tool_calls_made.append({
                        "name": call.name,
                        "args": call.args,
                        "ok": result.ok,
                        "error": result.error,
                        "side_effects": list(result.side_effects),
                    })
                    # B-397 anti-loop guard: track consecutive identical
                    # tool failures. ``error_signature`` is the first
                    # 80 chars of the error so transient differences
                    # (line numbers, timestamps) don't reset the streak,
                    # but qualitatively-different errors do.
                    if result.ok:
                        _stuck_loop_deque.clear()
                        _had_success_this_hop = True
                        # Batch C.2: validate this successful step.
                        # Verdict published as INNER_MONOLOGUE so the UI
                        # think pane shows the advancement chip. Never
                        # blocks the hop loop.
                        if (
                            _step_validator is not None
                            and _step_validator.enabled
                        ):
                            try:
                                _result_preview = (
                                    result.content
                                    if isinstance(result.content, str)
                                    else str(result.content)
                                )
                                verdict = await _step_validator.validate(
                                    goal=user_message,
                                    plan_steps=getattr(
                                        self, "_active_plan_steps", None,
                                    ),
                                    tool_name=call.name,
                                    tool_args=dict(call.args or {}),
                                    tool_result=_result_preview,
                                )
                                if verdict is not None:
                                    await publish(
                                        EventType.INNER_MONOLOGUE, {
                                            "kind": "step_verdict",
                                            "tool": call.name,
                                            "verdict": verdict.verdict,
                                            "confidence": verdict.confidence,
                                            "reason": verdict.reason,
                                            "elapsed_ms": round(
                                                verdict.elapsed_ms, 1,
                                            ),
                                            "hop": hop,
                                        },
                                    )
                            except Exception as exc:  # noqa: BLE001
                                from xmclaw.utils.log import get_logger as _gl
                                _gl(__name__).debug(
                                    "step_validator.hook_failed err=%s",
                                    exc,
                                )
                    else:
                        sig = (result.error or "")[:80]
                        key = (call.name, sig)
                        if _stuck_loop_deque and _stuck_loop_deque[-1] == key:
                            _stuck_loop_deque.append(key)
                        else:
                            _stuck_loop_deque = [key]
                    # B-397: break early when the agent is clearly stuck
                    # making the same failed call. Without this, real
                    # users hit max_hops=40 with 40 identical
                    # ``apply_patch.old_text not found`` errors and
                    # XMclaw burned ~$0.50 of LLM budget per stuck turn.
                    # 3 consecutive identical failures is conservative —
                    # genuine retries on transient errors typically vary
                    # by error string OR succeed within 1-2 retries.
                    if len(_stuck_loop_deque) >= _STUCK_LOOP_THRESHOLD:
                        stuck_tool, stuck_err = _stuck_loop_deque[-1]
                        await publish(EventType.ANTI_REQ_VIOLATION, {
                            "message": (
                                f"agent stuck — same tool error "
                                f"{_STUCK_LOOP_THRESHOLD}x in a row"
                            ),
                            "tool": stuck_tool,
                            "error_signature": stuck_err,
                            "hop": hop,
                            "kind": "stuck_loop",
                        })
                        truncation_text = (
                            f"⚠️ I appear to be stuck in a loop calling "
                            f"`{stuck_tool}` with the same error "
                            f"{_STUCK_LOOP_THRESHOLD} times in a row:\n"
                            f"  {stuck_err}\n\n"
                            f"Stopping early to avoid burning the rest "
                            f"of the {self._max_hops}-hop budget. The "
                            f"tool's error message likely tells you what "
                            f"to do differently — please re-read it and "
                            f"try a different approach next turn."
                        )
                        return AgentTurnResult(
                            ok=False, text=truncation_text,
                            hops=hop + 1,
                            tool_calls=tool_calls_made,
                            events=events,
                            error=f"stuck_loop tool={stuck_tool}",
                        )
                    # Tool result message content: on success pass through
                    # the content; on failure pass the structured error
                    # string so the LLM can tell the user what actually
                    # happened. Previously a failure landed as ``str(None)``
                    # == "None" here, which made the model hallucinate
                    # "the file is empty" or "got None back" instead of
                    # surfacing the real reason (permission denied, file
                    # not found, etc.).
                    if result.ok:
                        tool_msg_content = (
                            result.content if isinstance(result.content, str)
                            else str(result.content)
                        )
                    else:
                        err = result.error or "tool failed without an error message"
                        # Epic #3: render NEEDS_APPROVAL as a user-actionable
                        # prompt rather than a raw error string.
                        if err.startswith("NEEDS_APPROVAL:"):
                            request_id = err.split(":", 1)[1]
                            from xmclaw.utils.i18n import _

                            tool_msg_content = _(
                                "agent.needs_approval_prompt",
                                tool_name=call.name,
                                request_id=request_id,
                            )
                        else:
                            tool_msg_content = f"ERROR: {err}"
                    # Epic #14: scan the tool output for prompt-injection
                    # attempts before it lands in the conversation history.
                    # Apply the configured policy (detect / redact / block).
                    decision = apply_policy(
                        tool_msg_content,
                        policy=self._injection_policy,
                        source=SOURCE_TOOL_RESULT,
                        extra={
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                        },
                    )
                    if decision.event is not None:
                        await publish(
                            EventType.PROMPT_INJECTION_DETECTED,
                            decision.event,
                        )
                    if decision.blocked:
                        tool_msg_content = (
                            "ERROR: tool output blocked by prompt-injection "
                            "policy. Categories: "
                            + ", ".join(decision.scan.categories())
                        )
                    else:
                        tool_msg_content = decision.content
                    messages.append(Message(
                        role="tool",
                        content=tool_msg_content,
                        tool_call_id=call.id,
                    ))
                    # B-Vision: harvest any vision attachment the tool
                    # produced. ``ToolResult.metadata["attach_image"]``
                    # is set by ``screen_capture`` / ``screen_region_capture``
                    # — see content.py / computer_use.py. Skipped when
                    # the prompt-injection policy blocked the tool
                    # output (the tool didn't actually run safely).
                    if (
                        result.ok
                        and not decision.blocked
                        and isinstance(getattr(result, "metadata", None), dict)
                    ):
                        img_path = result.metadata.get("attach_image")
                        if isinstance(img_path, str) and img_path:
                            _vision_attachments.append(img_path)
                    if decision.blocked:
                        await publish(EventType.ANTI_REQ_VIOLATION, {
                            "message": "tool output blocked by prompt-injection policy",
                            "kind": "prompt_injection_blocked",
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "hop": hop,
                        })
                        return AgentTurnResult(
                            ok=False, text="",
                            hops=hop + 1,
                            tool_calls=tool_calls_made,
                            events=events,
                            error="prompt_injection_blocked",
                        )

                # B-Vision: inject a single user-role message that
                # actually CARRIES the screenshot(s) the tool batch
                # produced. Kimi K2.6 / GPT-4o / Claude all accept
                # ``image_url`` content blocks on user messages; the
                # OpenAI translator (xmclaw/providers/llm/openai.py
                # _img_to_data_url) resizes + base64-encodes from path.
                # This is the SINGLE point in the agent loop where
                # vision enters context — the OCR detour is now a
                # fallback, not the primary channel.
                if _vision_attachments:
                    messages.append(Message(
                        role="user",
                        content=(
                            "(screenshots from the previous tool batch "
                            "— look at the images to choose pixel "
                            "coordinates instead of guessing from OCR "
                            "text. The first image is from the first "
                            "screenshot/region tool in the batch, etc.)"
                        ),
                        images=tuple(_vision_attachments),
                    ))
                    await publish(EventType.INNER_MONOLOGUE, {
                        "kind": "vision_attached",
                        "hop": hop,
                        "image_count": len(_vision_attachments),
                    })

                # Meta-cognitive no-progress guard: if we haven't made a
                # successful tool call for N consecutive hops, we're
                # probably stuck in a wasteful retry loop (different
                # tools, same failure pattern, or hallucinated tools).
                # This complements B-397 which only catches identical
                # consecutive failures.
                if _had_success_this_hop:
                    _no_progress_counter = 0
                else:
                    _no_progress_counter += 1
                    if _no_progress_counter >= _NO_PROGRESS_THRESHOLD:
                        await publish(EventType.ANTI_REQ_VIOLATION, {
                            "message": (
                                f"agent made no progress — "
                                f"{_NO_PROGRESS_THRESHOLD} consecutive "
                                f"hops without a successful tool call"
                            ),
                            "tool": None,
                            "error_signature": "no_progress",
                            "hop": hop,
                            "kind": "no_progress",
                        })
                        return AgentTurnResult(
                            ok=False,
                            text=(
                                f"⚠️ I've been trying for "
                                f"{_no_progress_counter} hops without "
                                f"making any successful progress. The "
                                f"tools keep failing or returning errors. "
                                f"Let me stop here and ask: could you "
                                f"rephrase the request or provide more "
                                f"specific guidance?"
                            ),
                            hops=hop + 1,
                            tool_calls=tool_calls_made,
                            events=events,
                            error="no_progress",
                        )

                # Next hop: send tool results back to the LLM.
                continue

            # 4. No tool calls -- terminal assistant text.
            # Append the assistant turn to messages so it becomes part of
            # the saved history for the next turn.
            messages.append(Message(
                role="assistant", content=response.content,
            ))
            compression_info = self._persist_history(session_id, messages)
            if compression_info is not None:
                # B-33: emit a CONTEXT_COMPRESSED event so the Trace
                # page surfaces the squeeze. Best-effort — never let
                # observability break the turn.
                try:
                    await publish(EventType.CONTEXT_COMPRESSED, compression_info)
                except Exception:  # noqa: BLE001
                    pass

            # B-26 Cross-session memory write-back via MemoryManager.
            # The manager fans out sync_turn to every registered
            # provider (failure-isolated). Builtin file provider is a
            # no-op for this hook (it persists via remember tool, not
            # via raw turn capture); external SqliteVec provider
            # ingests the turn for future recall.
            if self._memory_manager is not None and response.content:
                try:
                    await self._memory_manager.sync_turn(
                        session_id=session_id,
                        agent_id=self._agent_id,
                        user_content=user_message,
                        assistant_content=response.content,
                    )
                    # Hint providers about the next-turn query so they
                    # can spin a background prefetch — used by external
                    # plugins with async backends. Best-effort.
                    await self._memory_manager.queue_prefetch(
                        user_message, session_id=session_id,
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _log_memory_failure(exc)

            # B-112: post-sampling hooks (free-code parity). Each hook
            # gets a snapshot of the just-finished turn and runs in
            # the background via gather() so the user's next prompt
            # isn't blocked. Hook failures are caught + logged inside
            # _safe_run; never propagate. Runs only on terminal turns
            # (final assistant response, no pending tool calls).
            if self._post_sampling_registry is not None and response.content:
                try:
                    from xmclaw.daemon.post_sampling_hooks import HookContext
                    from xmclaw.daemon.factory import _resolve_persona_profile_dir
                    try:
                        pdir = _resolve_persona_profile_dir(self._cfg)
                    except Exception:  # noqa: BLE001
                        pdir = None
                    hook_ctx = HookContext(
                        session_id=session_id,
                        agent_id=self._agent_id,
                        user_message=user_message,
                        assistant_response=response.content,
                        history=list(self._histories.get(session_id) or []),
                        llm=llm,
                        persona_dir=pdir,
                        cfg=self._cfg or {},
                        # B-197: hand the memory manager + embedder so
                        # extractor hooks can dual-write facts to the
                        # vec store. Manager fans out to all wired
                        # providers; embedder is best-effort.
                        memory_provider=self._memory_manager,
                        embedder=self._embedder,
                        # B-198 Phase 3: persona_store rendered as
                        # disk cache after each fact upsert.
                        persona_store=self._persona_store,
                    )
                    # Fire-and-forget — don't await, the next turn must
                    # not wait for hooks. Strong ref via add() / discard
                    # callback (B-69 pattern) to prevent GC mid-flight.
                    bg = asyncio.create_task(
                        self._post_sampling_registry.dispatch(hook_ctx),
                        name=f"post-sampling-hooks-{session_id[:8]}",
                    )
                    self._post_sampling_bg.add(bg)
                    bg.add_done_callback(self._post_sampling_bg.discard)
                except Exception as exc:  # noqa: BLE001
                    _log_memory_failure(exc)

            # Skill invocation tracking is fully deterministic via
            # tool_invocation_started events (skill_<id> tools), no
            # heuristic SKILL_INVOKED emission needed.

            # Jarvisification: record successful turn for pattern extraction.
            _evo_loop = getattr(self, "_evolution_loop", None)
            if _evo_loop is not None and response.content:
                try:
                    _evo_loop.record_success(
                        task=user_message[:200],
                        approach="agent_turn",
                        result=response.content[:500],
                    )
                except Exception:  # noqa: BLE001
                    pass

            # 2026-05-10 Phase B: UnifiedMemorySystem auto-put.
            #
            # Heuristic-gated MemoryExtractor decides whether anything
            # in this turn is worth long-term storage. When yes, we
            # ``unified_memory.put()`` the distilled fact + emit
            # MEMORY_PUT_AUTO so the UI's 记忆活动 timeline can show
            # the agent learning. Same posture as Phase A: fire-and-
            # forget background task, never blocks turn return, never
            # fails the turn on extractor / write errors.
            #
            # Why background instead of synchronous: the LLM extract
            # call can take 3-8 s. Adding that to every turn's tail
            # latency tanks UX. The trade-off is the put isn't visible
            # until the next turn — acceptable, the user already got
            # their answer.
            if (
                self._memory_extractor is not None
                and self._unified_memory is not None
                and response.content
                and user_message
            ):
                async def _bg_extract_and_put() -> None:
                    try:
                        fact = await self._memory_extractor.extract(
                            user_message=user_message,
                            assistant_response=response.content,
                        )
                        if fact is None:
                            return
                        new_id = await self._unified_memory.put(
                            text=fact.text,
                            layer=fact.layer,
                            node_type=fact.node_type,
                            metadata={
                                "source": "auto_extract",
                                "session_id": session_id,
                                "reason": fact.reason,
                            },
                        )
                        await publish(EventType.MEMORY_PUT_AUTO, {
                            "session_id": session_id,
                            "id": new_id,
                            "text": fact.text[:300],
                            "layer": fact.layer,
                            "node_type": fact.node_type,
                            "reason": fact.reason,
                        })
                    except Exception as exc:  # noqa: BLE001
                        _log_memory_failure(exc)

                bg_extract = asyncio.create_task(
                    _bg_extract_and_put(),
                    name=f"unified-extract-{session_id[:8]}",
                )
                # Reuse the post_sampling_bg set so background hooks
                # all share one GC-anchor — same shape as the existing
                # _post_sampling_bg pattern.
                self._post_sampling_bg.add(bg_extract)
                bg_extract.add_done_callback(
                    self._post_sampling_bg.discard,
                )

            return AgentTurnResult(
                ok=True, text=response.content, hops=hop + 1,
                tool_calls=tool_calls_made,
                events=events,
            )
        return None
