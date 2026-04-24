"""AgentLoop — user-turn orchestrator.

Lives in ``xmclaw.daemon`` (not ``xmclaw.core``) because it stitches
across the ``xmclaw.providers.llm`` and ``xmclaw.providers.tool``
boundaries. The CI ``check_import_direction`` gate enforces that
``xmclaw.core.*`` modules may not import from ``xmclaw.providers.*``;
AgentLoop legitimately does, so it sits one layer above core in the
dependency graph.

Given an ``LLMProvider`` and an optional ``ToolProvider``, turn a user
message into a final assistant response, publishing every step to the
bus as a BehavioralEvent.

Design:

  ``run_turn(session_id, user_message)``
    emits USER_MESSAGE
    repeats up to ``max_hops`` times:
      emits LLM_REQUEST
      calls llm.complete(messages, tools=tools)
      emits LLM_RESPONSE
      if response has tool_calls:
        for each tool call:
          emits TOOL_CALL_EMITTED
          emits TOOL_INVOCATION_STARTED
          invokes tool_provider.invoke(call)
          emits TOOL_INVOCATION_FINISHED (with side_effects from ToolResult)
        feed tool results back into messages; continue
      else:
        return assistant text (loop ends)
    if hop limit reached: emit ANTI_REQ_VIOLATION("hop limit")

Anti-req #1 in this layer: we only ever consume structured ``ToolCall``
objects produced by the provider's translator. A response whose
``tool_calls`` is empty becomes a terminal text response, never a
"tried to look like a tool call but wasn't" fallback path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)
from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.llm.base import LLMProvider, Message
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.security import (
    SOURCE_TOOL_RESULT,
    PolicyMode,
    apply_policy,
)
from xmclaw.utils.cost import BudgetExceeded, CostTracker


def _default_system_prompt() -> str:
    """Built at import time so the OS / user-home hints are concrete."""
    import platform
    os_name = platform.system()  # Windows / Linux / Darwin
    home = str(Path.home())
    desktop = str(Path.home() / "Desktop")
    shell_hint = {
        "Windows": (
            "The shell is PowerShell. You can use Unix-style aliases "
            "(ls, cat, pwd, rm) OR native Get-ChildItem / Get-Content. "
            "Do NOT use bash-isms like `$(whoami)` or `&&` chaining -- "
            "PowerShell uses `;` and `$env:USERNAME`."
        ),
        "Linux": "The shell is bash.",
        "Darwin": "The shell is bash / zsh (macOS).",
    }.get(os_name, f"The shell is whatever is on PATH.")
    return (
        "You are XMclaw, a local-first AI agent running on the user's own "
        f"machine. OS: {os_name}. User home: {home}. Desktop: {desktop}. "
        "You have real access to their filesystem, a shell, and the web.\n\n"
        "Available tools -- use them aggressively rather than refusing:\n"
        "  - file_read, file_write, list_dir: inspect and modify files\n"
        f"  - bash: run shell commands. {shell_hint}\n"
        "  - web_fetch: GET a URL and read its content\n"
        "  - web_search: search the web when a fact needs looking up\n\n"
        "Guidelines:\n"
        "  - Never say 'I don't have that tool' without checking the list "
        "above. 'List the Desktop' is `list_dir` on the Desktop path. "
        "'Check weather' / 'check GitHub stars' is `web_search` or "
        "`web_fetch`. 'Read this file' is `file_read`.\n"
        "  - Paths on Windows can use either forward or backslashes. You "
        "already know the user's home and Desktop; don't ask.\n"
        "  - If a tool call fails, READ THE ERROR MESSAGE the tool "
        "returned and tell the user the real reason -- do NOT hallucinate "
        "that the file was empty or the result was 'None'. The error you "
        "receive is the truth.\n"
        "  - Don't loop more than 2-3 times on the same failing tool. If "
        "web_search returns nothing useful, tell the user what you tried "
        "rather than retrying indefinitely.\n"
        "  - Remember earlier turns. When the user references a fact you "
        "established before, answer from that history.\n"
        "  - Respond in the language the user writes in."
    )


_DEFAULT_SYSTEM = _default_system_prompt()


@dataclass
class AgentTurnResult:
    """What ``run_turn`` returns after a single user turn completes."""

    ok: bool
    text: str                              # final assistant text (if any)
    hops: int                              # LLM calls made
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[BehavioralEvent] = field(default_factory=list)
    error: str | None = None


class AgentLoop:
    """Explicit state machine — one method, ``run_turn``, orchestrates
    a single user message through to its final assistant response.

    This is deliberately separate from ``OnlineScheduler``'s bandit-
    over-variants logic. Scheduler picks a variant; AgentLoop runs a
    turn with whatever variant (or plain LLM call) the caller
    selected. Phase 4.2+ can stack them: scheduler selects the skill
    version, agent loop runs the turn, grader scores it, controller
    decides promotion.
    """

    def __init__(
        self,
        llm: LLMProvider,
        bus: InProcessEventBus,
        *,
        tools: ToolProvider | None = None,
        system_prompt: str = _DEFAULT_SYSTEM,
        max_hops: int = 5,
        agent_id: str = "agent",
        cost_tracker: CostTracker | None = None,
        history_cap: int = 40,
        prompt_injection_policy: PolicyMode = PolicyMode.DETECT_ONLY,
    ) -> None:
        self._llm = llm
        self._bus = bus
        self._tools = tools
        self._system_prompt = system_prompt
        self._max_hops = max_hops
        self._agent_id = agent_id
        self._cost_tracker = cost_tracker
        # Per-session conversation history. Keyed by session_id; each value
        # is the running list of Messages EXCLUDING the system prompt
        # (which is re-prepended on every run_turn so operator changes to
        # _system_prompt take effect immediately, not after the next restart).
        self._histories: dict[str, list[Message]] = {}
        self._history_cap = history_cap
        # Epic #14: what the scanner does when a tool result looks hostile.
        self._injection_policy = prompt_injection_policy

    def clear_session(self, session_id: str) -> None:
        """Drop a session's conversation history. Called by the WS gateway
        on SESSION_LIFECYCLE destroy, or by a ``/reset`` user intent."""
        self._histories.pop(session_id, None)

    def _persist_history(
        self, session_id: str, messages: list[Message],
    ) -> None:
        """Save conversation history (system prompt excluded) with a size cap.

        Trims from the front to keep the most recent ``_history_cap``
        messages. Because Anthropic / OpenAI require assistant messages
        with tool_calls to be immediately followed by their tool results,
        we round the cut point up to the next "clean" boundary -- i.e.
        skip forward past any trailing tool-result orphans until we
        land on a user message or the end.
        """
        # Drop the system message we prepended for this turn.
        history = [m for m in messages if m.role != "system"]
        if len(history) <= self._history_cap:
            self._histories[session_id] = history
            return
        start = len(history) - self._history_cap
        # Advance past partial tool blocks: if the first kept message is a
        # tool result or an assistant message that references tools, skip
        # forward to the next user turn.
        while start < len(history) and history[start].role in ("tool", "assistant"):
            start += 1
        self._histories[session_id] = history[start:]

    async def run_turn(
        self, session_id: str, user_message: str,
    ) -> AgentTurnResult:
        events: list[BehavioralEvent] = []
        tool_calls_made: list[dict[str, Any]] = []

        async def publish(
            type_: EventType, payload: dict[str, Any],
            *, correlation_id: str | None = None,
        ) -> BehavioralEvent:
            event = make_event(
                session_id=session_id, agent_id=self._agent_id,
                type=type_, payload=payload, correlation_id=correlation_id,
            )
            events.append(event)
            await self._bus.publish(event)
            return event

        # 1. Announce the user message.
        await publish(
            EventType.USER_MESSAGE,
            {"content": user_message, "channel": "agent_loop"},
        )

        # Resume prior history for this session; the first turn starts empty.
        # Note: system prompt is prepended fresh each turn (not stored in
        # history) so reprovisioning the agent picks up the new prompt.
        prior = self._histories.get(session_id, [])
        messages: list[Message] = [
            Message(role="system", content=self._system_prompt),
            *prior,
            Message(role="user", content=user_message),
        ]
        tool_specs = self._tools.list_tools() if self._tools else None

        for hop in range(self._max_hops):
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

            # 2. LLM request event (messages_hash is a cheap fingerprint
            # so the bus consumer can distinguish different hops).
            await publish(EventType.LLM_REQUEST, {
                "model": getattr(self._llm, "model", None),
                "hop": hop,
                "messages_count": len(messages),
                "tools_count": len(tool_specs) if tool_specs else 0,
            })

            t0 = time.perf_counter()
            try:
                response = await self._llm.complete(
                    messages, tools=tool_specs,
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.perf_counter() - t0) * 1000.0
                await publish(EventType.LLM_RESPONSE, {
                    "hop": hop,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": latency_ms,
                })
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
            })

            # Anti-req #6 cont'd: record the call's usage against the
            # budget right after we see it. check_budget on the NEXT
            # hop will block if we crossed the cap during this one.
            if self._cost_tracker is not None:
                cost = self._cost_tracker.record(
                    provider=getattr(self._llm, "__class__", type(self._llm)).__name__,
                    model=getattr(self._llm, "model", "") or "",
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
                await publish(EventType.COST_TICK, {
                    "hop": hop,
                    "cost_usd": cost,
                    "spent_usd": self._cost_tracker.spent_usd,
                    "budget_usd": self._cost_tracker.budget_usd,
                    "remaining_usd": self._cost_tracker.remaining_usd,
                })

            # 3. If the model made tool calls, execute them and feed
            # results back into the conversation.
            if response.tool_calls:
                if self._tools is None:
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
                    # Fill session_id so stateful tools (todo_write/read)
                    # can key their per-session buckets. ToolCall is frozen
                    # so we construct a copy via dataclasses.replace.
                    import dataclasses as _dc
                    call_with_sid = _dc.replace(call, session_id=session_id)
                    result = await self._tools.invoke(call_with_sid)
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
                    await publish(EventType.TOOL_INVOCATION_FINISHED, {
                        "call_id": result.call_id,
                        "name": call.name,
                        "result": result.content,
                        "error": result.error,
                        "latency_ms": result.latency_ms,
                        "expected_side_effects": list(result.side_effects),
                        "ok": result.ok,
                    })
                    tool_calls_made.append({
                        "name": call.name,
                        "args": call.args,
                        "ok": result.ok,
                        "error": result.error,
                        "side_effects": list(result.side_effects),
                    })
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
                # Next hop: send tool results back to the LLM.
                continue

            # 4. No tool calls -- terminal assistant text.
            # Append the assistant turn to messages so it becomes part of
            # the saved history for the next turn.
            messages.append(Message(
                role="assistant", content=response.content,
            ))
            self._persist_history(session_id, messages)
            return AgentTurnResult(
                ok=True, text=response.content, hops=hop + 1,
                tool_calls=tool_calls_made,
                events=events,
            )

        # 5. Hit the hop limit.
        await publish(EventType.ANTI_REQ_VIOLATION, {
            "message": f"agent loop hit max_hops={self._max_hops} without terminal text",
            "hops": self._max_hops,
        })
        return AgentTurnResult(
            ok=False, text="",
            hops=self._max_hops,
            tool_calls=tool_calls_made,
            events=events,
            error=f"hit max_hops={self._max_hops}",
        )
