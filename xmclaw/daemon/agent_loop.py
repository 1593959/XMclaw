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


_DEFAULT_SYSTEM = (
    "You are a helpful assistant. You have access to tools when they are "
    "provided. Use tools for any task that requires reading, writing, or "
    "acting on the user's system; don't guess at file contents. Respond "
    "with plain text when no tool is needed."
)


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
    ) -> None:
        self._llm = llm
        self._bus = bus
        self._tools = tools
        self._system_prompt = system_prompt
        self._max_hops = max_hops
        self._agent_id = agent_id

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

        messages: list[Message] = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=user_message),
        ]
        tool_specs = self._tools.list_tools() if self._tools else None

        for hop in range(self._max_hops):
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
                    result = await self._tools.invoke(call)
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
                    messages.append(Message(
                        role="tool",
                        content=(
                            result.content if isinstance(result.content, str)
                            else str(result.content)
                        ),
                        tool_call_id=call.id,
                    ))
                # Next hop: send tool results back to the LLM.
                continue

            # 4. No tool calls — terminal assistant text.
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
