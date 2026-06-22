"""Hop loop mixin for AgentLoop.

Extracted from agent_loop.py to reduce module size.
Contains the LLM ↔ tool hop loop execution logic.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.cognition.goal_anchor import (
    GoalAnchorState,
    GoalAnchorTracker,
    get_session_focus,
)
from xmclaw.core.bus import BehavioralEvent, EventType
from xmclaw.core.ir.toolcall import ToolSpec
from xmclaw.daemon.history_utils import _is_transient_tool_error
from xmclaw.daemon.turn_types import AgentTurnResult, _log_memory_failure
from xmclaw.providers.llm.base import Message
from xmclaw.security import SOURCE_TOOL_RESULT, apply_policy
from xmclaw.utils.cost import BudgetExceeded


# B-302: mechanistic honesty guard. Detects when the assistant claims
# to have remembered something without actually invoking the tool.
_MEMORY_HONESTY_TRIGGERS: tuple[str, ...] = (
    "记下了", "记住了", "已记录", "已经记录",
    "我记住了", "我记下了", "我已经记录",
)
_MEMORY_TOOLS: frozenset[str] = frozenset({"remember", "learn_about_user", "memory"})

# 2026-06-08 动态 per-call 超时:按 hop 深度递增(代替按开场消息分档)。
# 有效超时 = min(llm_timeout_s 上限, BASE + hop×STEP)。深一跳 = 任务更复杂、
# 上下文更大 → 给更多时间,只增不减。BASE 给推理模型 hop0 留足首 token 时间。
_HOP_BASE_TIMEOUT_S = 240.0
_HOP_STEP_TIMEOUT_S = 120.0

# 2026-06-16: stream stall timeout defaults to 120s, but is configurable
# via cfg["llm"]["stream_stall_timeout_s"] at AgentLoop boot. Bump this
# default for complex tasks (deep reasoning, long generation, etc.) that
# may pause for extended periods without emitting tokens.
_STREAM_STALL_TIMEOUT_S = 120.0
_STREAM_HARD_CAP_S = 1800.0


def set_stream_stall_timeout(value: float) -> None:
    """Override the stream-stall timeout. Called from AgentLoop.__init__
    when the user supplies ``cfg.llm.stream_stall_timeout_s``."""
    global _STREAM_STALL_TIMEOUT_S
    _STREAM_STALL_TIMEOUT_S = max(30.0, float(value))


def _hop_timeout(hop: int, bound: float) -> float:
    """Per-call wall-clock for a given hop depth, capped at ``bound``.

    hop0=240s, +120s/hop, capped at the configured ``llm.timeout_s``.
    Deeper hop = task has proven it's complex + context is bigger → more
    time. Replaces the old "judge complexity from the opening message"
    tiering that starved short-message-launched deep tasks at hop 2.
    """
    return min(float(bound), _HOP_BASE_TIMEOUT_S + max(0, int(hop)) * _HOP_STEP_TIMEOUT_S)


def _messages_hash(messages: list[Message]) -> int:
    """Cheap fingerprint for message-list caching."""
    h = 0
    for m in messages:
        h = hash((h, m.role, m.content, m.tool_call_id, len(m.tool_calls)))
    return h


def _check_memory_honesty(
    assistant_text: str | None,
    tool_calls_made: list[Any],
) -> str | None:
    """Return a corrective nudge if the assistant claims memory without
    actually calling a memory tool.  None means honest or no claim."""
    text = (assistant_text or "").strip()
    if not text:
        return None
    # Did the assistant claim to have remembered?
    claimed = any(t in text for t in _MEMORY_HONESTY_TRIGGERS)
    if not claimed:
        return None
    # Did any memory tool actually run this hop?
    actually_called = any(
        (getattr(tc, "name", None) or tc.get("name", "")) in _MEMORY_TOOLS
        for tc in tool_calls_made
    )
    if actually_called:
        return None
    return (
        "你刚才说记住了/记下了，但我没有检测到 "
        "`remember` 或 `learn_about_user` 工具的调用。"
        "如果信息确实需要长期保存，请立即调用对应工具；"
        "如果只是口头确认，请改说'了解了'或'收到'，"
        "避免让用户误以为数据已持久化。"
    )


async def _invoke_single_tool(
    call: Any,
    effective_tools: Any,
    session_id: str,
    *,
    tool_timeout_s: float = 180.0,
    hook_engine: Any = None,
    agent_id: str = "main",
    cancel_event: "asyncio.Event | None" = None,
) -> Any:
    """Invoke one tool with defensive error handling and retry.

    Standalone async function — does NOT depend on HopLoopMixin or
    AgentLoop state.  All dependencies are passed as parameters so
    the function is trivially testable in isolation.

    Returns the raw ``ToolResult``.  Event publishing and loop-state
    mutation are the caller's responsibility so that multiple calls
    can be executed in parallel.

    2026-06-12: accepts ``cancel_event`` so the user's Stop button
    takes effect mid-tool instead of waiting for the next hop boundary.
    """
    import dataclasses as _dc
    import asyncio as _asyncio
    from xmclaw.core.ir import ToolResult as _ToolResult

    # Fast check before any work.
    if cancel_event is not None and cancel_event.is_set():
        return _ToolResult(
            call_id=call.id, ok=False, content=None,
            error="cancelled by user",
        )

    call_with_sid = _dc.replace(call, session_id=session_id)

    # Wave-32: PreToolUse hook dispatch.
    if hook_engine is not None:
        try:
            from xmclaw.core.hooks import HookEvent as _HE
            _pre = await hook_engine.dispatch(
                _HE.PRE_TOOL_USE,
                session_id=session_id,
                agent_id=agent_id,
                payload={
                    "tool_name": call_with_sid.name,
                    "args": dict(call_with_sid.args or {}),
                    "call_id": call_with_sid.id,
                },
            )
            if (
                _pre.decision == "deny"
                or _pre.continue_ is False
            ):
                return _ToolResult(
                    call_id=call.id,
                    ok=False,
                    content=None,
                    error=(
                        f"[hook denied] {_pre.block_reason or ''}".strip()
                    ),
                )
            if isinstance(_pre.updated_input, dict):
                call_with_sid = _dc.replace(
                    call_with_sid, args=_pre.updated_input,
                )
        except Exception as _exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger as _gl
            _gl(__name__).warning(
                "pre_tool_use_hook.failed tool=%s err=%s",
                call.name, _exc,
            )

    try:
        invoke_coro = effective_tools.invoke(call_with_sid)
        if cancel_event is not None:
            # Race invoke against cancel — whichever finishes first wins.
            invoke_task = _asyncio.ensure_future(invoke_coro)
            cancel_task = _asyncio.ensure_future(cancel_event.wait())
            done, pending = await _asyncio.wait(
                [invoke_task, cancel_task],
                timeout=tool_timeout_s,
                return_when=_asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                invoke_task.cancel()
                for t in pending:
                    t.cancel()
                result = _ToolResult(
                    call_id=call.id, ok=False, content=None,
                    error="cancelled by user",
                )
            elif invoke_task in done:
                cancel_task.cancel()
                for t in pending:
                    t.cancel()
                result = invoke_task.result()
            else:
                # timeout — neither finished
                invoke_task.cancel()
                cancel_task.cancel()
                raise _asyncio.TimeoutError()
        else:
            result = await _asyncio.wait_for(
                invoke_coro,
                timeout=tool_timeout_s,
            )
    except _asyncio.TimeoutError:
        from xmclaw.utils.log import get_logger as _gl
        _gl(__name__).warning(
            "tool.invoke_wall_clock_exceeded tool=%s timeout=%.1fs",
            call.name, tool_timeout_s,
        )
        result = _ToolResult(
            call_id=call.id,
            ok=False,
            content=None,
            error=(
                f"tool '{call.name}' exceeded {tool_timeout_s:.0f}s "
                f"wall-clock and was aborted. The tool was likely "
                f"stuck waiting for an external event (page nav, "
                f"subprocess stdout, MCP server reply). Try a "
                f"different approach or add an explicit shorter "
                f"timeout in the tool args if it supports one."
            ),
        )
    except Exception as _invoke_exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger as _gl
        _gl(__name__).warning(
            "tool.invoke_uncaught_exception tool=%s err=%s",
            call.name, _invoke_exc,
        )
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
    # B-17: retry up to 3× on transient failures with exponential back-off.
    # 2026-06-22: expanded from 1 → 3 retries (0.5s, 2s, 5s) and widened
    # the transient pattern list in history_utils.py. Real networks (CN
    # cross-border, satellite, hotel Wi-Fi) often need >1 attempt.
    _B17_BACKOFFS = (0.5, 2.0, 5.0)
    _b17_attempt = 0
    while (
        not result.ok
        and result.error
        and _is_transient_tool_error(result.error)
        and _b17_attempt < len(_B17_BACKOFFS)
    ):
        delay = _B17_BACKOFFS[_b17_attempt]
        _b17_attempt += 1
        await _asyncio.sleep(delay)
        try:
            retry = await _asyncio.wait_for(
                effective_tools.invoke(call_with_sid),
                timeout=tool_timeout_s,
            )
        except _asyncio.TimeoutError:
            retry = _ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=(
                    f"tool '{call.name}' retry #{_b17_attempt} also exceeded "
                    f"{tool_timeout_s:.0f}s wall-clock"
                ),
            )
        except Exception as _retry_exc:  # noqa: BLE001
            retry = _ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=(
                    f"{type(_retry_exc).__name__}: {_retry_exc} "
                    f"(retry #{_b17_attempt} raised uncaught — "
                    f"ToolProvider contract violation)"
                ),
            )
        if retry.ok:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).info(
                "tool.retry_succeeded tool=%s attempt=%d first_error=%s",
                call.name, _b17_attempt, (result.error or "")[:120],
            )
            result = retry
            break
        else:
            # If the retry itself hit a *different* transient error,
            # keep looping; if it's a non-transient error, stop.
            if not _is_transient_tool_error(retry.error or ""):
                break
            result = retry  # bubble the latest error for next attempt

    # Wave-32: PostToolUse hook dispatch.
    if hook_engine is not None:
        try:
            from xmclaw.core.hooks import HookEvent as _HE2
            _post = await hook_engine.dispatch(
                _HE2.POST_TOOL_USE,
                session_id=session_id,
                agent_id=agent_id,
                payload={
                    "tool_name": call.name,
                    "call_id": call.id,
                    "ok": result.ok,
                    "error": result.error or "",
                },
            )
            if (
                isinstance(_post.updated_input, (str, dict))
                and result.ok
            ):
                result = _dc.replace(
                    result, content=_post.updated_input,
                )
        except Exception as _exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger as _gl
            _gl(__name__).warning(
                "post_tool_use_hook.failed tool=%s err=%s",
                call.name, _exc,
            )

    return result


class HopLoopMixin:
    """Provides the LLM ↔ tool hop loop."""

    async def _invoke_single_tool(
        self, call: Any, effective_tools: Any, session_id: str,
        *,
        cancel_event: "asyncio.Event | None" = None,
    ) -> Any:
        """Thin wrapper around the module-level function that forwards
        instance-level configuration. 2026-06-12: accepts cancel_event
        so the Stop button interrupts long-running tool calls."""
        return await _invoke_single_tool(
            call, effective_tools, session_id,
            tool_timeout_s=float(
                getattr(self, "_tool_invoke_timeout_s", 180.0),
            ),
            hook_engine=getattr(self, "_hook_engine", None),
            agent_id=getattr(self, "_agent_id", "main"),
            cancel_event=cancel_event,
        )

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
        llm_timeout_s: float = 600.0,
        ultrathink: bool = False,
        _turn_metrics: "dict[str, Any] | None" = None,
        bus: "Any | None" = None,
    ) -> AgentTurnResult | None:
        """Execute the LLM ↔ tool hop loop.

        Returns a result when the loop terminates naturally
        (assistant text without tool calls, cancelled, stuck, etc).
        Returns ``None`` when max_hops is reached — caller must
        synthesise the truncation message.
        """
        _NO_PROGRESS_THRESHOLD = 5
        _no_progress_counter = 0
        # B-302: honesty guard — max 1 correction per turn to avoid loops.
        _B302_MAX_CORRECTIONS = 1
        _b302_corrected = 0
        # Reset fallback chain tracking each turn so every provider
        # gets a fresh chance (audit 2026-06-11).
        self._fallback_tried_models.clear()
        # 2026-05-26 (audit G1 phase 2): narration tracking moved to
        # ``narration_enforcer.NarrationEnforcer``. Hop loop just
        # observes per-hop and gets back a NarrationDecision.
        from xmclaw.daemon.narration_enforcer import NarrationEnforcer
        _narration = NarrationEnforcer(
            strict=getattr(self, "_narration_strict", False),
        )
        _narration_nudge_pending: str | None = None
        # 2026-06-04: compression result cache. If messages haven't
        # changed since the last compression, skip the 1-3s pipeline.
        _last_compress_hash: int | None = None
        _last_compressed_messages: list[Message] | None = None

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

        # B-PERF: pre-compute session-level GoalAnchor state once per
        # turn instead of re-scanning the full history on every hop.
        # User messages don't change mid-turn, so this is pure waste.
        _session_history = self._histories.get(session_id) or []
        _session_user_thread: list[str] = []
        for _msg in _session_history:
            if getattr(_msg, "role", None) == "user":
                _content = getattr(_msg, "content", None) or ""
                if isinstance(_content, str) and _content.strip():
                    s = _content.lstrip()
                    if s.startswith("[GOAL-ANCHOR]") or s.startswith("[turn hint]"):
                        continue
                    _session_user_thread.append(_content)
        _session_goal: str | None = (
            _session_user_thread[0] if _session_user_thread else None
        )
        _current_focus: str | None = get_session_focus(session_id)

        # Jarvis Phase 6.3: active skill matches for GoalAnchor.
        _skill_matches: list[dict[str, Any]] | None = None
        _reg = getattr(self, "_skill_registry", None)
        if _reg is not None and user_message:
            try:
                _matched = _reg.find_multi(user_message, top_k=3)
                if _matched:
                    _skill_matches = [
                        {
                            "skill_id": r.skill_id,
                            "version": r.version,
                            "title": (r.manifest.title or ""),
                        }
                        for r in _matched
                    ]
            except Exception:  # noqa: BLE001
                pass

        _is_multi_turn = bool(
            _session_goal and _session_goal.strip() != user_message.strip()
        )

        # Phase 11: map tool names → LLM capabilities so the hop loop can
        # swap the CHAT model for the next hop when the task needs a model
        # the current one lacks. Populated per turn; cleared after the batch.
        #
        # 2026-06-15: ONLY vision belongs here. Generation (image/video/
        # audio) is done INSIDE the generate_image / generate_video tool,
        # which calls the configured generation backend directly — you do
        # NOT chat with DALL-E. Swapping the next chat hop to a generation
        # endpoint (the old behavior) just fed a chat request to an
        # image-only model and broke the hop. Vision is different: after a
        # screenshot we want a chat model that can actually SEE the image,
        # and a vision model is still a chat model — that swap is valid.
        _CAPABILITY_BY_TOOL: dict[str, str] = {
            "camera_capture": "vision",
            "screen_capture": "vision",
            "image_read": "vision",
        }

        # 2026-06-16: todo-staleness nudge. The model tends to write a todo
        # list once and then never update item statuses as it works, so the
        # UI's "待办 0/N" bar sits frozen even as the agent clearly makes
        # progress (user report). Track the latest todo items + hops since
        # the last todo_write; when there are unfinished items and several
        # hops have passed without an update, nudge it to sync the list.
        _todo_items: list[Any] = []
        _hops_since_todo = 0
        _TODO_NUDGE_EVERY = 4

        for hop in range(self._max_hops):
            hop_corr = f"{turn_uuid}-{hop}"
            # 2026-06-08 动态超时(按 hop 深度,不按开场消息)。
            # 根因(用户报):旧逻辑在 turn 开头按「第一条用户消息」算一次超时,
            # 整个 hop 循环共用——一句"继续"引爆的深任务到 hop 2 早就不简单了,
            # 却还锁在短消息定的短档被掐。真实复杂度是「跑出来的」:到了第 N 跳
            # = 任务已被证明越复杂 + 上下文越大,就该给越多时间。
            # eff = min(配置上限, 基线 + hop×步长),只增不减,封顶 llm_timeout_s。
            #   hop0=240s, hop1=360, hop2=480, hop3=600, hop≥3 封顶。
            _eff_timeout = _hop_timeout(hop, llm_timeout_s)

            # Phase 11: if a previous hop (or external caller) set a
            # pending capability, re-resolve the LLM so this hop uses
            # the specialised model (e.g. vision-capable for screenshots,
            # image_gen for generation tasks).
            _pending_cap = getattr(self, "_pending_capability_pick", None)
            if (
                isinstance(_pending_cap, str) and _pending_cap.strip()
                and getattr(self, "_llm_registry", None) is not None
            ):
                try:
                    _cap_prof = self._llm_registry.pick_by_capability(
                        _pending_cap,
                        prefer_tier=("vision", "strong", "balanced", "fast"),
                    )
                    if _cap_prof is not None and _cap_prof.llm is not llm:
                        llm = _cap_prof.llm
                except Exception:  # noqa: BLE001
                    pass

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
                    ok=False,
                    text="已取消。" if tool_calls_made else "",
                    hops=hop,
                    tool_calls=tool_calls_made,
                    events=events,
                    error="cancelled",
                )
            # #1 Steering (2026-06-15): drain any mid-turn user guidance
            # HERE — a hop boundary is the only safe point to splice a new
            # user message in (all tool_calls from the prior hop already
            # have their results appended, so we won't orphan a tool_call).
            # The message lands before this hop's LLM call, so the agent
            # sees and adapts to it WITHOUT losing the work so far.
            _steer = getattr(self, "_steer_queue", {}).get(session_id)
            if _steer:
                for _txt in _steer:
                    messages.append(Message(
                        role="user",
                        content=f"[用户追加指令 / steering]\n{_txt}",
                    ))
                    await publish(EventType.USER_MESSAGE, {
                        "content": _txt,
                        "channel": "steering",
                        "hop": hop,
                    })
                self._steer_queue[session_id] = []

            # 2026-06-16: todo-staleness nudge (safe hop boundary). If the
            # agent has unfinished todos and hasn't touched the list in a
            # few hops, remind it to sync — otherwise the "待办 N/M" bar
            # stays frozen while it works.
            _pending_todos = [
                it for it in _todo_items
                if isinstance(it, dict)
                and str(it.get("status") or "").lower() not in ("completed", "done", "cancelled")
            ]
            if _pending_todos and _hops_since_todo >= _TODO_NUDGE_EVERY:
                # 上下文卫生：系统催促用 system 身份，不再伪装成用户发言。
                messages.append(Message(
                    role="system",
                    content=(
                        "[系统提示] 你的待办列表还有未完成项,且已经几步没更新了。"
                        "请立刻用 todo_write 重写整张列表,把已完成的标 completed、"
                        "正在做的标 in_progress —— 保持待办与真实进度一致(用户在侧栏盯着进度条)。"
                    ),
                ))
                _hops_since_todo = 0
            _hops_since_todo += 1

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
            _should_anchor = (
                _goal_anchor_tracker.should_anchor(hop)
                or (hop == 0 and _is_multi_turn)
            )
            if _should_anchor:
                anchor_text = _goal_anchor_tracker.format(GoalAnchorState(
                    original_goal=user_message,
                    session_goal=_session_goal,
                    session_user_thread=(
                        _session_user_thread if _session_user_thread else None
                    ),
                    current_focus=_current_focus,
                    skill_matches=_skill_matches,
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
                # 上下文卫生：GoalAnchor 是系统提醒，用 system 身份注入。
                messages.append(Message(role="system", content=anchor_text))
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
            }, correlation_id=hop_corr)
            # ↑ 2026-06-22: LLM_REQUEST was the ONLY per-hop LLM event missing
            # ``correlation_id=hop_corr`` (CHUNK / THINKING_CHUNK / RESPONSE all
            # carry it). Without it the chat reducer keyed the request off the
            # event's own random id, spawning a phantom "思考中…" bubble that
            # never matched the hop's CHUNK/RESPONSE stream — the empty "思考"
            # ghost the user saw. Now all of a hop's events share one id.

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
                # 2026-05-25 (audit G1 phase 2): narration hard-
                # enforcement. If prior hops were tool-only with no
                # plain text, inject a nudge before the next LLM
                # call. State lives in the NarrationEnforcer
                # observer constructed above.
                if _narration_nudge_pending is not None:
                    # 上下文卫生：narration nudge 是系统提示，用 system 身份。
                    messages.append(Message(
                        role="system", content=_narration_nudge_pending,
                    ))
                    _narration_nudge_pending = None
                _did_compress, _did_emit = False, False
                # 2026-06-04: cache hit when messages unchanged.
                _compress_hash = _messages_hash(messages)
                if (
                    _last_compress_hash is not None
                    and _compress_hash == _last_compress_hash
                    and _last_compressed_messages is not None
                ):
                    messages = _last_compressed_messages
                    _did_compress = True
                else:
                    try:
                        _new_msgs, _did_compress = await self._maybe_compress_messages(
                            messages, session_id,
                        )
                        if _did_compress:
                            messages = _new_msgs
                            # Fix Bug A (audit 2026-06-11): re-stash inflight
                            # after compression so the finally block sees
                            # post-compression messages. Previously the
                            # local rebind was invisible to inflight.
                            try:
                                _ims = getattr(self, "_inflight_messages", None)
                                if _ims is not None and session_id in _ims:
                                    _ims[session_id] = messages
                            except Exception:
                                pass
                            _last_compress_hash = _compress_hash
                            _last_compressed_messages = list(messages)
                            await publish(EventType.CONTEXT_COMPRESSED, {
                                "hop": hop,
                                "trigger": "proactive_threshold",
                                "session_id": session_id,
                            })
                            _did_emit = True
                    except Exception as _exc:  # noqa: BLE001
                        # 2026-06-04: aggregate compression errors.
                        try:
                            from xmclaw.core.error_aggregator import ErrorSeverity, get_aggregator
                            get_aggregator().record(
                                ErrorSeverity.WARNING, __name__, "run_hop_loop.compress",
                                _exc, message="proactive compression failed",
                            )
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
                _B230_MAX_CONTINUES = 1  # 2026-06-04: reduced from 3 to 1
                _b230_continue_count = 0
                _b230_acc_content = ""
                _b230_orig_max_tokens: int | None = None
                # Wave-32+ Speculation: build a per-hop cache that
                # the on_tool_block callback fills with prefetched
                # read-only tool tasks. Phase B below checks the
                # cache before dispatching, so any task that was
                # already started during streaming is awaited
                # instead of re-invoked.
                from xmclaw.cognition.speculation import (
                    SpeculationCache,
                    make_speculation_callback,
                )
                _spec_cache = SpeculationCache()
                _spec_invoke = lambda tc: self._invoke_single_tool(  # noqa: E731
                    tc, effective_tools, session_id, cancel_event=cancel_event,
                )
                _spec_callback = make_speculation_callback(_spec_cache, _spec_invoke)

                # 2026-06-14: publish TOOL_CALL_EMITTED + TOOL_INVOCATION_STARTED
                # the moment a tool_use block streams in (not at end of LLM
                # response). Without this the frontend gets all tool cards in
                # one batch when Phase A iterates response.tool_calls.
                _streamed_tool_ids: set[str] = set()
                def _on_tool_block(tc: Any) -> Any:
                    try:
                        tc_id = getattr(tc, "id", None) or ""
                        if tc_id and tc_id not in _streamed_tool_ids:
                            _streamed_tool_ids.add(tc_id)
                            asyncio.create_task(publish(
                                EventType.TOOL_CALL_EMITTED, {
                                    "call_id": tc_id,
                                    "name": getattr(tc, "name", "") or "",
                                    "args": getattr(tc, "args", {}) or {},
                                    "provenance": getattr(tc, "provenance", "llm"),
                                },
                            ))
                            asyncio.create_task(publish(
                                EventType.TOOL_INVOCATION_STARTED, {
                                    "call_id": tc_id,
                                    "name": getattr(tc, "name", "") or "",
                                },
                            ))
                    except Exception:  # noqa: BLE001 — never break streaming
                        pass
                    return _spec_callback(tc)

                # 2026-05-30: immediate UI banner when stream() degrades
                # to non-streaming complete() (Anthropic risk reject /
                # compat shim missing /stream). Fires the MOMENT the
                # provider knows it has to fall back — not 30s later
                # when the non-streamed reply lands — so the user
                # doesn't read the silent wait as a hang.
                async def _on_stream_fallback(reason: str) -> None:
                    try:
                        await publish(EventType.LLM_STREAM_FALLBACK, {
                            "hop": hop,
                            "reason": reason,
                            "provider": getattr(
                                llm, "__class__", type(llm),
                            ).__name__,
                            "session_id": session_id,
                        })
                    except Exception:  # noqa: BLE001
                        pass

                # 2026-06-04: first-token timeout = max(total/3, 5s).
                # If the first chunk doesn't arrive within this window,
                # we try a fallback profile before giving up.
                # 2026-06-15: cap at 60s so deep-hop timeouts don't leave the
                # user staring at a silent spinner for minutes.
                _first_token_timeout = min(max(_eff_timeout / 3.0, 10.0), 60.0)

                async def _call_llm_with_first_token_guard(
                    _llm: Any,
                ) -> Any:
                    """Call complete_streaming with first-token + total timeouts.

                    If the first token doesn't arrive within ``_first_token_timeout``,
                    cancel the call and try the registry's default fallback profile
                    once.  Raises ``asyncio.TimeoutError`` when both primary and
                    fallback fail the first-token window.
                    """
                    _ft_event = asyncio.Event()
                    _done_event = asyncio.Event()
                    _resp: Any = None
                    _err: Exception | None = None
                    # Last time any token (text or thinking) arrived. Drives
                    # the stall-based completion guard below so a long-but-
                    # live stream isn't killed for merely taking a while.
                    _last_activity = [time.perf_counter()]

                    async def _wc(delta: str) -> None:
                        _ft_event.set()
                        _last_activity[0] = time.perf_counter()
                        await _emit_chunk(delta)

                    async def _wtc(delta: str) -> None:
                        _ft_event.set()
                        _last_activity[0] = time.perf_counter()
                        await _emit_thinking_chunk(delta)

                    async def _await_stream(_t: "asyncio.Task") -> None:
                        """Wait for ``_t`` to finish, aborting only if the
                        stream stalls (no token for STALL seconds) or blows
                        the absolute HARD_CAP. Cancels ``_t`` before raising
                        TimeoutError so the provider call doesn't leak."""
                        _start = time.perf_counter()
                        try:
                            while not _done_event.is_set():
                                now = time.perf_counter()
                                if now - _last_activity[0] > _STREAM_STALL_TIMEOUT_S:
                                    raise asyncio.TimeoutError("stream_stall")
                                if now - _start > _STREAM_HARD_CAP_S:
                                    raise asyncio.TimeoutError("stream_hard_cap")
                                try:
                                    await asyncio.wait_for(
                                        _done_event.wait(), timeout=2.0,
                                    )
                                except asyncio.TimeoutError:
                                    continue  # tick: re-check stall/cap
                        except asyncio.TimeoutError:
                            _t.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await _t
                            raise
                        await _t  # propagate result / provider exception

                    # 2026-06-15: heartbeat so the UI knows the LLM is still
                    # working during long no-token gaps (deep reasoning,
                    # overloaded provider). Starts after first token and stops
                    # when the call completes or times out.
                    _heartbeat_task: asyncio.Task | None = None

                    async def _llm_heartbeat(start_ts: float) -> None:
                        tick = 0
                        while True:
                            await asyncio.sleep(15.0)
                            tick += 1
                            elapsed = round(time.perf_counter() - start_ts, 1)
                            try:
                                await publish(EventType.INNER_MONOLOGUE, {
                                    "kind": "llm_still_working",
                                    "hop": hop,
                                    "tick": tick,
                                    "elapsed_seconds": elapsed,
                                    "model": getattr(_llm, "model", "") or "",
                                }, correlation_id=hop_corr)
                            except Exception:  # noqa: BLE001
                                pass

                    async def _do_call(_llm_instance: Any) -> None:
                        nonlocal _resp, _err
                        try:
                            _resp = await _llm_instance.complete_streaming(
                                messages, tools=tool_specs,
                                on_chunk=_wc,
                                on_thinking_chunk=_wtc,
                                on_tool_block=_on_tool_block,
                                on_stream_fallback=_on_stream_fallback,
                                cancel=cancel_event,
                                # 2026-06-14: 深思/ultrathink toggle → 真实开启
                                # provider extended_thinking（仅本回合）。None
                                # 时回落到 profile 静态默认。
                                extended_thinking=ultrathink or None,
                            )
                        except Exception as exc:
                            _err = exc
                        finally:
                            _done_event.set()

                    async def _await_first_token() -> None:
                        """Wake on the first token OR call completion —
                        whichever is first — within the first-token window.
                        Raises TimeoutError if neither happens. Racing
                        ``_done_event`` matters for tool-only replies (no
                        text delta): the call returns fast but ``_ft_event``
                        never fires, so waiting on it alone stalled the full
                        window for nothing."""
                        ft = asyncio.ensure_future(_ft_event.wait())
                        dn = asyncio.ensure_future(_done_event.wait())
                        try:
                            done, _pending = await asyncio.wait(
                                {ft, dn},
                                timeout=_first_token_timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not done:
                                raise asyncio.TimeoutError
                        finally:
                            for _f in (ft, dn):
                                if not _f.done():
                                    _f.cancel()

                    _task = asyncio.create_task(_do_call(_llm))
                    try:
                        await _await_first_token()
                    except asyncio.TimeoutError:
                        if _done_event.is_set():
                            # Completed without chunks (non-streaming fallback)
                            if _err is not None:
                                raise _err
                            return _resp
                        # First-token timeout — cancel and try fallback
                        _task.cancel()
                        try:
                            await _task
                        except asyncio.CancelledError:
                            pass

                        from xmclaw.utils.log import get_logger
                        get_logger(__name__).warning(
                            "agent_loop.first_token_timeout hop=%d "
                            "ft_timeout=%.1fs session=%s model=%s",
                            hop, _first_token_timeout, session_id,
                            getattr(_llm, "model", "") or "",
                        )

                        # Try fallback profile
                        _fallback = None
                        _registry = getattr(self, "_llm_registry", None)
                        if _registry is not None:
                            _default = _registry.default()
                            if _default is not None and _default.llm is not _llm:
                                _fallback = _default.llm

                        if _fallback is not None:
                            await publish(EventType.INNER_MONOLOGUE, {
                                "kind": "first_token_fallback",
                                "original_model": getattr(_llm, "model", "") or "",
                                "fallback_model": getattr(_fallback, "model", "") or "",
                                "hop": hop,
                            })
                            _ft_event.clear()
                            _done_event.clear()
                            _resp = None
                            _err = None
                            _task = asyncio.create_task(_do_call(_fallback))
                            try:
                                await _await_first_token()
                            except asyncio.TimeoutError:
                                if _done_event.is_set():
                                    if _err is not None:
                                        raise _err
                                    return _resp
                                _task.cancel()
                                try:
                                    await _task
                                except asyncio.CancelledError:
                                    pass
                                raise asyncio.TimeoutError("first_token")
                            # Fallback first token arrived, wait for completion.
                            _heartbeat_task = asyncio.create_task(
                                _llm_heartbeat(time.perf_counter()),
                            )
                            try:
                                await _await_stream(_task)
                            finally:
                                if _heartbeat_task is not None:
                                    _heartbeat_task.cancel()
                                    with contextlib.suppress(asyncio.CancelledError):
                                        await _heartbeat_task
                            if _err is not None:
                                raise _err
                            return _resp
                        else:
                            raise asyncio.TimeoutError("first_token")

                    # First token arrived from primary, wait for completion.
                    # Start the heartbeat so the UI sees progress during long
                    # gaps; cancel it as soon as the call finishes.
                    _heartbeat_task = asyncio.create_task(
                        _llm_heartbeat(time.perf_counter()),
                    )
                    try:
                        await _await_stream(_task)
                    finally:
                        if _heartbeat_task is not None:
                            _heartbeat_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await _heartbeat_task
                    if _err is not None:
                        raise _err
                    return _resp

                while True:  # outer = B-230 auto-continue
                    while True:  # inner = B-227 classify-and-retry
                        try:
                            response = await _call_llm_with_first_token_guard(llm)
                            break  # inner: success
                        except asyncio.TimeoutError:
                            # Re-raise into the original timeout handler
                            # below (separate path with its own user msg).
                            raise
                        except Exception as _exc:  # noqa: BLE001
                            from xmclaw.utils.error_classifier import (
                                classify_api_error, backoff_schedule,
                                is_non_transient_reason,
                            )
                            ce = classify_api_error(
                                _exc,
                                provider=getattr(llm, "__class__", type(llm)).__name__,
                                model=getattr(llm, "model", "") or "",
                            )
                            _b227_last_classified = ce

                            # 2026-06-04: fast-fail non-transient errors.
                            # Auth, billing, model_not_found etc. will never
                            # succeed on retry — surface immediately.
                            if is_non_transient_reason(ce.reason):
                                try:
                                    from xmclaw.utils.log import get_logger
                                    get_logger(__name__).warning(
                                        "agent_loop.llm_fast_fail hop=%d reason=%s "
                                        "non_transient=True msg=%s",
                                        hop, ce.reason.value, ce.message[:120],
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                raise

                            # 2026-06-04: context_overflow gets 1 retry only,
                            # and we force-compress BEFORE sleeping.
                            _is_context_overflow = ce.reason.value in (
                                "context_overflow", "payload_too_large",
                                "long_context_tier",
                            )
                            if _is_context_overflow and _b227_attempts >= 1:
                                try:
                                    from xmclaw.utils.log import get_logger
                                    get_logger(__name__).warning(
                                        "agent_loop.llm_retry_exhausted hop=%d "
                                        "reason=%s context_overflow_retry_limit=1",
                                        hop, ce.reason.value,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                raise

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

                            # Multi-provider fallback chain (audit 2026-06-11):
                            # when primary LLM fails, iterate through ALL
                            # available profiles in the registry before
                            # giving up. Previously only switched to the
                            # default once, then slept-and-retried the
                            # same failing endpoint.
                            _registry = getattr(self, "_llm_registry", None)
                            _tried = getattr(self, "_fallback_tried_models", None)
                            if _tried is None:
                                object.__setattr__(self, "_fallback_tried_models", set())
                                _tried = getattr(self, "_fallback_tried_models")
                            _tried.add(getattr(llm, "model", "") or "")
                            if _registry is not None and _b227_attempts > 0:
                                _next_llm = None
                                for _prof in _registry:
                                    _pm = getattr(_prof.llm, "model", "") if _prof.llm else ""
                                    if _prof.llm is not None and _pm not in _tried:
                                        _next_llm = _prof.llm
                                        break
                                if _next_llm is not None:
                                    try:
                                        from xmclaw.utils.log import get_logger
                                        get_logger(__name__).info(
                                            "agent_loop.llm_fallback_switch "
                                            "hop=%d from=%s to=%s tried=%d",
                                            hop,
                                            getattr(llm, "model", "") or "",
                                            getattr(_next_llm, "model", "") or "",
                                            len(_tried),
                                        )
                                    except Exception:  # noqa: BLE001
                                        pass
                                    llm = _next_llm
                                    _b227_attempts = 0
                                    continue

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
                                        _last_compress_hash = _messages_hash(messages)
                                        _last_compressed_messages = list(messages)
                                        # 2026-06-12: re-stash inflight after
                                        # compression so the finally block sees
                                        # post-compression progress.
                                        try:
                                            _ims = getattr(self, "_inflight_messages", None)
                                            if _ims is not None and session_id in _ims:
                                                _ims[session_id] = messages
                                        except Exception:
                                            pass
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
                        # 2026-06-04: force-compress before continue to avoid
                        # repeated truncation on already-bloated context.
                        try:
                            _new_msgs, _did_force = await self._maybe_compress_messages(
                                messages, session_id, force=True,
                            )
                            if _did_force:
                                messages = _new_msgs
                                _last_compress_hash = None
                                _last_compressed_messages = None
                                # 2026-06-12: re-stash inflight after B-230
                                # force compression.
                                try:
                                    _ims = getattr(self, "_inflight_messages", None)
                                    if _ims is not None and session_id in _ims:
                                        _ims[session_id] = messages
                                except Exception:
                                    pass
                        except Exception:  # noqa: BLE001
                            pass
                        # 2026-06-04: give the LLM more headroom on the
                        # continue call by bumping max_tokens when the
                        # provider exposes it as a mutable attribute.
                        try:
                            _orig = getattr(llm, "max_tokens", None)
                            if (
                                _orig is not None
                                and isinstance(_orig, int)
                                and _orig > 0
                            ):
                                _b230_orig_max_tokens = _orig
                                llm.max_tokens = int(_orig * 1.5)
                        except Exception:  # noqa: BLE001
                            pass
                        # B-230 fix: remove the previous continuation
                        # pair (assistant + user) so accumulated content
                        # doesn't duplicate across continues, preserving
                        # context window budget.
                        _msgs = list(messages)
                        if (
                            _b230_continue_count > 0
                            and len(_msgs) >= 2
                            and _msgs[-1].role == "user"
                            and "[B-230 auto-continue]" in (
                                _msgs[-1].content or ""
                            )
                            and _msgs[-2].role == "assistant"
                        ):
                            _msgs = _msgs[:-2]
                        messages = _msgs + [
                            Message(
                                role="assistant",
                                content=_b230_acc_content,
                            ),
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
                _stalled_s = round((time.perf_counter() - t0), 0)
                await publish(EventType.ANTI_REQ_VIOLATION, {
                    "message": (
                        f"LLM call timed out at hop {hop} after "
                        f"{_stalled_s:.0f}s — no token progress for "
                        f"{_STREAM_STALL_TIMEOUT_S:.0f}s, aborting turn "
                        "rather than blocking forever."
                    ),
                    "hop": hop,
                    "category": "llm_timeout",
                })
                err = (
                    f"LLM call timed out at hop {hop} (stream stalled — "
                    f"no progress for {_STREAM_STALL_TIMEOUT_S:.0f}s). "
                    "Provider may be overloaded or stuck."
                )
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
            finally:
                # 2026-06-04: restore max_tokens if we bumped it for B-230
                # continue.  Runs even when the above except blocks return.
                if _b230_orig_max_tokens is not None:
                    try:
                        llm.max_tokens = _b230_orig_max_tokens
                    except Exception:  # noqa: BLE001
                        pass

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

            # Wave-27 fix-6: dynamic ctx_len ratchet. Every successful
            # LLM completion proves the model accepted that many input
            # tokens, so its true context window is AT LEAST that big.
            # Stash a high-water mark on the agent so cold-rebuilds
            # (e.g. after a daemon restart) can recover the estimate
            # via _resolve_context_length, AND apply it live to the
            # existing compressor so the current session benefits
            # without waiting for a session-reset / rebuild. This is
            # the self-healing path for "you registered MiniMax, what
            # about every other model" — no static table update
            # needed after the first successful call.
            if response.prompt_tokens > 0:
                prev = int(
                    getattr(self, "_observed_prompt_tokens_high_water", 0) or 0
                )
                if response.prompt_tokens > prev:
                    self._observed_prompt_tokens_high_water = int(
                        response.prompt_tokens
                    )
                    if self._compressor is not None:
                        try:
                            self._compressor.maybe_raise_context_length(
                                response.prompt_tokens,
                            )
                        except Exception:  # noqa: BLE001
                            pass

            # Anti-req #6 cont'd: record the call's usage against the
            # budget right after we see it. check_budget on the NEXT
            # hop will block if we crossed the cap during this one.
            #
            # Wave-30 (2026-05-18): emit COST_TICK on EVERY LLM call,
            # not just when a cost_tracker is wired. Pre-fix the dashboard
            # cache-hit-rate widget was zero on every install that
            # didn't set ``cost.track`` or ``cost.budget_usd`` (i.e.,
            # the default install) — the cache stats live in the same
            # event, and the event itself was gated behind cost
            # tracking. Cost is now ``None`` in the payload when no
            # tracker is wired; the UI handles that with the existing
            # ``cost.cache_hit_rate != null`` check.
            _cache_creation = int(getattr(
                response, "cache_creation_input_tokens", 0,
            ) or 0)
            _cache_read = int(getattr(
                response, "cache_read_input_tokens", 0,
            ) or 0)
            _tick_payload: dict[str, Any] = {
                "hop": hop,
                # B-107: surface per-call token counts so the Web UI
                # can render a live "tokens this turn" widget without
                # synthesising it from chunk events.
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "model": getattr(llm, "model", "") or "",
                # B-316: cache stats (Anthropic + Moonshot Kimi + Zhipu
                # GLM; OpenAI proper reports cached_tokens via
                # prompt_tokens_details which openai.py
                # _extract_cache_tokens maps to cache_read).
                "cache_creation_input_tokens": _cache_creation,
                "cache_read_input_tokens": _cache_read,
            }
            if self._cost_tracker is not None:
                cost = self._cost_tracker.record(
                    provider=getattr(llm, "__class__", type(llm)).__name__,
                    model=getattr(llm, "model", "") or "",
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
                _tick_payload.update({
                    "cost_usd": cost,
                    "spent_usd": self._cost_tracker.spent_usd,
                    "budget_usd": self._cost_tracker.budget_usd,
                    "remaining_usd": self._cost_tracker.remaining_usd,
                })
            else:
                # No tracker → no cost reporting, but cache stats are
                # still meaningful for the Dashboard widget.
                _tick_payload["cost_usd"] = None
            await publish(EventType.COST_TICK, _tick_payload)

            # Jarvis Phase 1-2: emit cache metrics summary every 5 hops
            # so the dashboard can show live cache hit rates without
            # recomputing from the full event log.
            _cache_metrics = getattr(self, "_cache_metrics", None)
            if _cache_metrics is not None and hop % 5 == 0:
                try:
                    _provider = getattr(llm, "__class__", type(llm)).__name__
                    _summary = _cache_metrics.build_summary_payload(
                        session_id, provider=_provider,
                    )
                    if _summary is not None:
                        await publish(EventType.CACHE_METRICS_SUMMARY, _summary)
                except Exception:  # noqa: BLE001
                    pass

            # 2026-05-25: narration tracking. A "silent" hop = the
            # model emitted tool calls but no visible plain-text
            # content (think-tool counts as hidden — already routed
            # through INNER_MONOLOGUE elsewhere). Two consecutive
            # silent hops → nudge prompt on next hop. Three → also
            # publish a synthetic progress marker so the user isn't
            # staring at silence.
            _tool_names = [
                getattr(tc, "name", "?") for tc in (response.tool_calls or [])
            ]
            _decision = _narration.observe_hop(
                response_content=response.content,
                has_tool_calls=bool(response.tool_calls),
                hop=hop,
                tool_names=_tool_names,
            )
            if _decision.nudge_message:
                _narration_nudge_pending = _decision.nudge_message
            if _decision.progress_marker:
                try:
                    await publish(
                        EventType.INNER_MONOLOGUE,
                        _decision.progress_marker,
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Jarvis Phase 1-2: strict narration enforcement. When the
            # enforcer says "force text response", discard tool_calls
            # from this hop so the model must produce plain text on
            # the next hop (nudge is already queued).
            if _decision.force_text_response and response.tool_calls:
                # Strip tool_calls but keep any text content so the
                # model sees its own (empty) response in context.
                response = dataclasses.replace(response, tool_calls=())

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
                # 2026-05-26: also carry thinking so DeepSeek V4 (and
                # any other provider that hard-requires thinking-echo
                # on subsequent hops) sees its own prior reasoning.
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    thinking=getattr(response, "thinking", "") or "",
                    thinking_signature=getattr(
                        response, "thinking_signature", "",
                    ) or "",
                ))

                # Phase A: emit start events for any tool_use blocks
                # whose stream-time _on_tool_block didn't fire (e.g. when
                # the provider degraded to non-streaming complete()).
                for call in response.tool_calls:
                    if call.id in _streamed_tool_ids:
                        continue  # already emitted during stream
                    await publish(EventType.TOOL_CALL_EMITTED, {
                        "call_id": call.id,
                        "name": call.name,
                        "args": call.args,
                        "provenance": call.provenance,
                    })
                    await publish(EventType.TOOL_INVOCATION_STARTED, {
                        "call_id": call.id, "name": call.name,
                    })
                # Yield so WS tasks flush: tool cards appear BEFORE results.
                # Without this, bus.publish returns immediately (background
                # asyncio tasks), Phase B starts, and the WS send races
                # against tool execution — the frontend gets all events at
                # once instead of seeing cards appear one by one.
                if bus is not None and hasattr(bus, "drain"):
                    await bus.drain()
                else:
                    await asyncio.sleep(0.05)

                # Phase 11: inspect the tool batch and set a capability
                # hint so the NEXT hop (or any nested agent / callback
                # that reads _resolve_llm) routes to the right model.
                # We set it here — before Phase B — so that if a tool
                # internally triggers an LLM call it can see the hint.
                for _tc in response.tool_calls:
                    _cap = _CAPABILITY_BY_TOOL.get(_tc.name)
                    if _cap:
                        object.__setattr__(
                            self, "_pending_capability_pick", _cap,
                        )
                        break

                # Phase B: invoke tools with read-parallel / write-smart
                # semantics (B-7).  Read-only tools (file_read, list_dir,
                # web_search, …) run concurrently; write tools are grouped
                # by target file path so calls touching different files run
                # in parallel while calls touching the same file remain
                # serial, preserving safety.
                from xmclaw.cognition.speculation import maybe_await_cached

                _read_only_names = {
                    spec.name for spec in (effective_tools.list_tools() or [])
                    if getattr(spec, "read_only", False)
                }

                def _extract_target_path(tc: Any) -> str | None:
                    args = getattr(tc, "args", None) or {}
                    for key in ("path", "file", "filepath", "filename"):
                        if key in args:
                            return str(args[key])
                    return None

                # 2026-06-15: track wall-clock start per call and emit a
                # progress heartbeat every 2s so long-running tools don't
                # look frozen. Minimum display time is enforced in Phase C.
                _invoke_start_ts: dict[str, float] = {}

                async def _invoke_one(call: Any) -> Any:
                    _invoke_start_ts[call.id] = time.perf_counter()

                    async def _progress_loop() -> None:
                        while True:
                            await asyncio.sleep(2.0)
                            elapsed = round(
                                time.perf_counter() - _invoke_start_ts[call.id], 1,
                            )
                            try:
                                await publish(
                                    EventType.TOOL_INVOCATION_PROGRESS, {
                                        "call_id": call.id,
                                        "name": call.name,
                                        "elapsed_seconds": elapsed,
                                        "message": None,
                                    },
                                )
                            except Exception:  # noqa: BLE001
                                pass

                    _progress_task = asyncio.create_task(_progress_loop())
                    try:
                        return await maybe_await_cached(
                            _spec_cache, call,
                            lambda c=call: self._invoke_single_tool(
                                c, effective_tools, session_id,
                                cancel_event=cancel_event,
                            ),
                        )
                    finally:
                        _progress_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await _progress_task

                async def _serial_writes(
                    _items: list[tuple[int, Any]],
                ) -> list[tuple[int, Any]]:
                    _out: list[tuple[int, Any]] = []
                    for _i, _c in _items:
                        _out.append((_i, await _invoke_one(_c)))
                    return _out

                _tc_list = response.tool_calls
                _invoke_results: list[Any] = [None] * len(_tc_list)
                _idx = 0
                while _idx < len(_tc_list):
                    if _tc_list[_idx].name in _read_only_names:
                        # Collect a contiguous run of read-only calls
                        _batch: list[Any] = []
                        _indices: list[int] = []
                        while (
                            _idx < len(_tc_list)
                            and _tc_list[_idx].name in _read_only_names
                        ):
                            _batch.append(_tc_list[_idx])
                            _indices.append(_idx)
                            _idx += 1
                        _batch_results = await asyncio.gather(
                            *[_invoke_one(c) for c in _batch]
                        )
                        for _i, _res in zip(_indices, _batch_results):
                            _invoke_results[_i] = _res
                    else:
                        # Collect a contiguous run of write calls
                        _write_batch: list[Any] = []
                        _write_indices: list[int] = []
                        while (
                            _idx < len(_tc_list)
                            and _tc_list[_idx].name not in _read_only_names
                        ):
                            _write_batch.append(_tc_list[_idx])
                            _write_indices.append(_idx)
                            _idx += 1

                        # Group by target path so different files run in
                        # parallel while the same file stays serial.
                        _path_groups: dict[str | None, list[tuple[int, Any]]] = {}
                        for _w_idx, _w_tc in zip(_write_indices, _write_batch):
                            _path = _extract_target_path(_w_tc)
                            if _path not in _path_groups:
                                _path_groups[_path] = []
                            _path_groups[_path].append((_w_idx, _w_tc))

                        # Execute each path group serially, all groups in parallel.
                        _group_results = await asyncio.gather(*[
                            _serial_writes(_items) for _items in _path_groups.values()
                        ])

                        # Flatten back into result list preserving original order.
                        for _grp in _group_results:
                            for _i, _res in _grp:
                                _invoke_results[_i] = _res

                # Cancel any speculated tools whose ToolCall didn't
                # end up in the response — defensive cleanup, should
                # be rare (LLM emitting then retracting a tool_use is
                # not part of the Anthropic protocol but the SDK has
                # bugs).
                _spec_cache.cancel_remaining()

                # 2026-06-12: check cancel after tool phase completes.
                # Tools that were already running may have finished even
                # though cancel was set, but any subsequent hop iteration
                # should bail before the next LLM call.
                if cancel_event.is_set():
                    return AgentTurnResult(
                        ok=False,
                        text="已取消。" if tool_calls_made else "",
                        hops=hop + 1,
                        tool_calls=tool_calls_made,
                        events=events,
                        error="cancelled",
                    )

                # Phase C: process results in original order (serial).
                # 2026-06-15: enforce a minimum visible running state so
                # very fast tools don't flash past the user. If a tool
                # finished in < 300ms we pause before publishing FINISHED.
                _MIN_TOOL_DISPLAY_MS = 300.0
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
                            # Feed the staleness nudge: remember the list +
                            # reset the "hops since update" counter.
                            _todo_items = items
                            _hops_since_todo = 0
                    # B-MULTIMODAL-UI / Wave 26: surface media
                    # attachments to the chat UI. Tools emit either
                    # the legacy ``metadata.attach_image: str`` or the
                    # canonical ``metadata.attachments: [MediaAttachment
                    # dicts]``. normalize_attachments() handles both.
                    # The UI doesn't have FS access, so we publish
                    # /api/v2/media/<filename> URLs the browser can
                    # <img>/<video>/<audio> directly.
                    from xmclaw.core.ir import normalize_attachments
                    _image_urls: list[str] = []
                    _video_urls: list[str] = []
                    _audio_urls: list[str] = []
                    # 文档类附件（xlsx/pdf/doc/…）：单独成列，前端渲染可下载
                    # 文件卡而非 <img>（2026-06-14 修"xlsx 加载失败"）。
                    _doc_dicts: list[dict[str, Any]] = []
                    _media_dicts: list[dict[str, Any]] = []

                    def _ensure_servable(att: Any) -> str:
                        """/api/v2/media/ 只服务 screenshots/audio/uploads 三个
                        目录 — 工具把产物存在别处（screenshot 存桌面是典型）
                        时 URL 必 404，前端裂图（2026-06-12 用户实测）。不在
                        可服务目录的文件复制进 uploads 再出 URL。"""
                        from pathlib import Path as _P
                        from xmclaw.utils.paths import data_dir as _dd
                        url = att.public_url()
                        try:
                            src = _P(str(getattr(att, "path", "") or ""))
                            if not src.is_file():
                                return url
                            v2 = _dd() / "v2"
                            servable = (v2 / "screenshots", v2 / "audio", v2 / "uploads")
                            if any(str(src.parent) == str(d) for d in servable):
                                return url
                            uploads = v2 / "uploads"
                            uploads.mkdir(parents=True, exist_ok=True)
                            dst = uploads / src.name
                            if not dst.exists():
                                import shutil as _sh
                                _sh.copy2(str(src), str(dst))
                            return f"/api/v2/media/{dst.name}"
                        except Exception:  # noqa: BLE001
                            return url  # 复制失败退回原 URL，最坏与修前一致

                    _tool_elapsed_ms = (
                        time.perf_counter()
                        - _invoke_start_ts.get(call.id, time.perf_counter())
                    ) * 1000.0
                    _remaining_ms = _MIN_TOOL_DISPLAY_MS - _tool_elapsed_ms
                    if _remaining_ms > 0:
                        await asyncio.sleep(_remaining_ms / 1000.0)

                    for att in normalize_attachments(
                        getattr(result, "metadata", None),
                    ):
                        url = _ensure_servable(att)
                        if att.kind == "image":
                            _image_urls.append(url)
                        elif att.kind == "video":
                            _video_urls.append(url)
                        elif att.kind == "audio":
                            _audio_urls.append(url)
                        else:
                            # document / unknown → 文件卡（带文件名 + 下载 url）
                            from pathlib import Path as _PP
                            _att_name = getattr(att, "name", None) or _PP(
                                str(getattr(att, "path", "") or "")
                            ).name
                            _doc_dicts.append({
                                "url": url,
                                "name": _att_name,
                                "mime": getattr(att, "mime", None),
                            })
                        _media_dicts.append({
                            **att.to_dict(),
                            "url": url,
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
                            "images": _image_urls,
                            "videos": _video_urls,
                            "audios": _audio_urls,
                            "documents": _doc_dicts,
                            # Wave 26: full attachment list for clients
                            # that want dimensions / duration / mime
                            # alongside the URL.
                            "attachments": _media_dicts,
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
                    if result.ok:
                        _had_success_this_hop = True
                        tool_msg_content = (
                            result.content if isinstance(result.content, str)
                            else str(result.content)
                        )
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

                    # Wave-27 fix-LAT12 (2026-05-17): hard per-tool-
                    # result size cap. A single tool message that's
                    # 200K+ chars (real-data: ``browser_eval`` returning
                    # a 300K-char DOM dump on a chaoxing course page,
                    # ``file_read`` of a 500K log, ``bash`` w/ verbose
                    # output) can blow the model's context window in
                    # one shot — Kimi rejected with "exceeded model
                    # token limit: 262144 (requested: 316217)" because
                    # ContextCompressor's ``protect_tail`` reserves
                    # recent tool results verbatim, so a giant one in
                    # the tail is NEVER pruned. Truncate here at write
                    # time so it never reaches the LLM. Head + tail
                    # so error-prone middle gets dropped first; agent
                    # can re-run with a narrower query when the truncated
                    # middle was the part it actually needed.
                    _TOOL_RESULT_MAX_CHARS = 80_000  # ~20K tokens at chars/4
                    if (
                        isinstance(tool_msg_content, str)
                        and len(tool_msg_content) > _TOOL_RESULT_MAX_CHARS
                    ):
                        orig_len = len(tool_msg_content)
                        head_n, tail_n = 50_000, 20_000
                        tool_msg_content = (
                            tool_msg_content[:head_n]
                            + (
                                f"\n\n…[TRUNCATED {orig_len - head_n - tail_n} "
                                f"chars from middle — full output was "
                                f"{orig_len} chars (~{orig_len // 4} tokens). "
                                f"Head ({head_n}) + tail ({tail_n}) kept. "
                                f"If the truncated middle is what you need, "
                                f"re-run the tool with a more targeted "
                                f"query (smaller selector, narrower path, "
                                f"line range, head/tail flag, etc.).]…\n\n"
                            )
                            + tool_msg_content[-tail_n:]
                        )

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

                # Wave-33: persist the working history after each completed
                # tool batch. If the daemon crashes before the turn ends, the
                # next turn resumes from this hop instead of losing all
                # intermediate tool calls and results.
                try:
                    await self._persist_history(
                        session_id, messages, mid_hop=True,
                    )
                except Exception:  # noqa: BLE001
                    pass

                # Meta-cognitive no-progress guard: if we haven't made a
                # successful tool call for N consecutive hops, we're
                # probably stuck in a wasteful retry loop (different
                # tools, same failure pattern, or hallucinated tools).
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

                # Phase 11: clear the capability hint before the next
                # hop so we don't permanently lock onto a specialised
                # model once the media task is done.
                object.__setattr__(self, "_pending_capability_pick", None)

                # B-302: honesty guard — did the assistant claim to have
                # remembered without actually calling a memory tool?
                if _b302_corrected < _B302_MAX_CORRECTIONS:
                    _nudge = _check_memory_honesty(
                        response.content, tool_calls_made,
                    )
                    if _nudge:
                        messages.append(Message(role="user", content=_nudge))
                        _b302_corrected += 1
                        continue  # give the model one more hop to fix it

                # Next hop: send tool results back to the LLM.
                continue

            # 4. No tool calls -- terminal assistant text.
            # Append the assistant turn to messages so it becomes part of
            # the saved history for the next turn. 2026-05-26: include
            # thinking so the next user turn (if any) doesn't 400 on
            # DeepSeek V4 thinking mode.
            messages.append(Message(
                role="assistant", content=response.content,
                thinking=getattr(response, "thinking", "") or "",
                thinking_signature=getattr(
                    response, "thinking_signature", "",
                ) or "",
            ))

            # 2026-06-15: when the model returns empty text after tool
            # calls, don't silently return an empty answer. Nudge once so
            # the model produces a visible summary for the user.
            if (
                not (response.content or "").strip()
                and tool_calls_made
                and not cancel_event.is_set()
            ):
                messages.append(Message(
                    role="user",
                    content=(
                        "[系统提示] 你刚刚执行了工具调用，但还没有给出"
                        "可见的最终回复。请根据工具结果向用户给出清晰、"
                        "简洁的总结或结论。"
                    ),
                ))
                continue  # one more hop

            # B-302: honesty guard on terminal text.
            if _b302_corrected < _B302_MAX_CORRECTIONS:
                _nudge = _check_memory_honesty(
                    response.content, tool_calls_made,
                )
                if _nudge:
                    messages.append(Message(role="user", content=_nudge))
                    _b302_corrected += 1
                    # Don't return yet — give the model one hop to
                    # actually call the memory tool.
                    continue

            # Wave-33: terminal assistant response is also a completed hop.
            # Persist it before the turn-end cleanup so crashes here don't
            # lose the final synthesis.
            try:
                await self._persist_history(
                    session_id, messages, mid_hop=True,
                )
            except Exception:  # noqa: BLE001
                pass

            compression_info = await self._persist_history(session_id, messages)
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
                        # Wave-27 follow-up: lessons dual-write to v2
                        # facts so they hit the LanceDB dedup pipeline.
                        # None when v2 isn't wired (config off / boot
                        # failure) — extractor hooks skip the v2 path
                        # silently in that case.
                        memory_v2_service=getattr(
                            self, "_memory_service", None,
                        ),
                        # Phase 1 (CMP): hand the Gateway to hooks so
                        # they can route writes through the unified pipeline.
                        memory_gateway=getattr(
                            self, "_memory_gateway", None,
                        ),
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

            # Phase 7.A.6 (2026-05-23): auto-extract + remember, V2-only.
            #
            # Heuristic-gated LLMFactExtractor decides whether anything
            # in this turn is worth long-term storage. When yes, we
            # remember() each distilled fact + emit MEMORY_PUT_AUTO so
            # the UI's 记忆活动 timeline can show the agent learning.
            # Background task: never blocks turn return, never fails
            # the turn on extractor / write errors.
            #
            # Why background instead of synchronous: the LLM extract
            # call can take 3-8 s. Adding that to every turn's tail
            # latency tanks UX. The trade-off is the put isn't visible
            # until the next turn — acceptable, the user already got
            # their answer.
            # Phase 1 (CMP): background LLM extraction routes through
            # CognitiveMemoryGateway when available.  The Gateway's
            # stubbed THINK/DECIDE layer makes this a transparent
            # passthrough in Phase 1; later phases will enable true
            # cross-turn summarisation here.
            _gateway = getattr(self, "_memory_gateway", None)
            mem_svc = getattr(self, "_memory_service", None)
            v2_extractor = getattr(self, "_memory_v2_llm_extractor", None)
            if (
                (mem_svc is not None or _gateway is not None)
                and v2_extractor is not None
                and response.content
                and user_message
            ):
                async def _bg_extract_and_put() -> None:
                    try:
                        candidates = await v2_extractor.extract_candidates(
                            user_message=user_message,
                            assistant_response=response.content,
                        )
                        # 2026-06-09 P3: ``preference`` and ``lesson`` are
                        # owned by the post-sampling ExtractLessonsHook (it
                        # dual-writes them to v2). The Layer-2 extractor
                        # prompt already omits these kinds; this is the
                        # defense-in-depth guard so a non-compliant / stubbed
                        # model can't double-write them through this path.
                        candidates = [
                            c for c in candidates
                            if c.kind not in ("preference", "lesson")
                        ]
                        if _gateway is not None:
                            from xmclaw.memory.v2.gateway_models import Observation
                            observations = [
                                Observation(
                                    source="post_sampling",
                                    content=cand.text,
                                    turn_id=session_id,
                                    timestamp=time.time(),
                                    metadata={
                                        "kind_hint": cand.kind,
                                        "scope_hint": cand.scope,
                                        "confidence_hint": cand.confidence,
                                    },
                                )
                                for cand in candidates
                            ]
                            written = await _gateway.ingest_batch(
                                observations,
                                context={"session_id": session_id},
                            )
                            for fact in written:
                                if fact is None:
                                    continue
                                await publish(EventType.MEMORY_PUT_AUTO, {
                                    "session_id": session_id,
                                    "id": fact.id,
                                    "text": fact.text[:300],
                                    "layer": fact.layer,
                                    "kind": fact.kind,
                                    "scope": fact.scope,
                                    "reason": "gateway_auto_extract:add",
                                })
                        else:
                            # Legacy path: direct write to MemoryService.
                            _use_decision = (
                                getattr(self, "_memory_write_decision", False)
                                and hasattr(mem_svc, "remember_with_decision")
                            )
                            for cand in candidates:
                                if _use_decision:
                                    result = await mem_svc.remember_with_decision(
                                        text=cand.text,
                                        kind=cand.kind,
                                        scope=cand.scope,
                                        confidence=cand.confidence,
                                        source_event_id=session_id,
                                    )
                                    fact = result.get("fact")
                                    _action = result.get("action", "ADD")
                                else:
                                    fact = await mem_svc.remember(
                                        text=cand.text,
                                        kind=cand.kind,
                                        scope=cand.scope,
                                        confidence=cand.confidence,
                                        source_event_id=session_id,
                                    )
                                    _action = "ADD"
                                if fact is None:
                                    continue
                                await publish(EventType.MEMORY_PUT_AUTO, {
                                    "session_id": session_id,
                                    "id": fact.id,
                                    "text": fact.text[:300],
                                    "layer": fact.layer,
                                    "kind": fact.kind,
                                    "scope": fact.scope,
                                    "reason": f"llm_auto_extract:{_action.lower()}",
                                })
                    except Exception as exc:  # noqa: BLE001
                        _log_memory_failure(exc)

                bg_extract = asyncio.create_task(
                    _bg_extract_and_put(),
                    name=f"memory-extract-{session_id[:8]}",
                )
                # Reuse the post_sampling_bg set so background hooks
                # all share one GC-anchor — same shape as the existing
                # _post_sampling_bg pattern.
                self._post_sampling_bg.add(bg_extract)
                bg_extract.add_done_callback(
                    self._post_sampling_bg.discard,
                )

            if _turn_metrics is not None:
                _turn_metrics["hop_count"] = hop + 1
                _turn_metrics["tool_call_count"] = len(tool_calls_made)

            # Plan lifecycle close-out (PlanFirst path).
            # When agent_loop's PlanFirst gate emitted PLAN_STARTED at
            # turn entry, the front-end PlanStrip needs PLAN_COMPLETED
            # to flip "plan.active" off; without it the bar stays
            # "running" forever. We also fast-forward any remaining
            # pending step pills to "done" so the user sees a clean
            # 100% state instead of pills frozen mid-progress.
            _plan_step_ids = list(
                getattr(self, "_active_plan_step_ids", []) or []
            )
            _plan_id = getattr(self, "_active_plan_id", None)
            if _plan_id and _plan_step_ids:
                try:
                    _completed = getattr(
                        self, "_active_plan_completed", set()
                    ) or set()
                    for _idx, _sid in enumerate(_plan_step_ids):
                        if _idx in _completed:
                            continue
                        await publish(EventType.PLAN_STEP_COMPLETED, {
                            "plan_id": _plan_id,
                            "step_id": _sid,
                            "step_index": _idx,
                            "n_steps": len(_plan_step_ids),
                            "action_kind": "llm_turn",
                        })
                    await publish(EventType.PLAN_COMPLETED, {
                        "plan_id": _plan_id,
                        "n_steps": len(_plan_step_ids),
                        "status": "completed",
                    })
                except Exception:  # noqa: BLE001 - observability only
                    pass
                finally:
                    self._active_plan_id = None
                    self._active_plan_step_ids = []

            return AgentTurnResult(
                ok=True, text=response.content, hops=hop + 1,
                tool_calls=tool_calls_made,
                events=events,
            )
        if _turn_metrics is not None:
            _turn_metrics["hop_count"] = hop
            _turn_metrics["tool_call_count"] = len(tool_calls_made)
        return None
