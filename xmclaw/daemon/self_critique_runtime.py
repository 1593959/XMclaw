"""Daemon-side wiring helpers for Reflexion self critique.

This module intentionally lives in ``xmclaw.daemon`` because it adapts
runtime objects such as the selected LLM provider and app state. The
pure cognition layer only receives duck-typed callables.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.ir import Message
from xmclaw.cognition.self_critique import (
    CritiqueTrigger,
    SelfCritiqueRequest,
    SelfCritiqueRunResult,
    TrajectoryEvent,
)


def build_self_critique_critic_call(llm: Any | None) -> Callable[[str], Any] | None:
    """Wrap an LLM provider as ``SelfCritiqueEngine.run``'s critic call."""
    complete = getattr(llm, "complete", None)
    if not callable(complete):
        return None

    async def _critic_call(prompt: str) -> str:
        response = await complete(
            [
                Message(
                    role="system",
                    content=(
                        "You are a strict Reflexion critic. Return only the "
                        "JSON object requested by the user prompt."
                    ),
                ),
                Message(role="user", content=prompt),
            ],
            tools=[],
        )
        return str(getattr(response, "content", response) or "")

    return _critic_call


def resolve_self_critique_memory(agent: Any | None, app_state: Any | None) -> Any | None:
    """Resolve the live MemoryService after lifespan wiring completes."""
    if agent is not None:
        memory_service = getattr(agent, "_memory_service", None)
        if memory_service is not None:
            return memory_service
    if app_state is not None:
        return getattr(app_state, "memory_v2_service", None)
    return None


def make_self_critique_memory_resolver(
    agent: Any | None,
    app_state: Any | None,
) -> Callable[[], Any | None]:
    """Return a late-binding memory resolver for ActionDispatcher."""

    def _resolve() -> Any | None:
        return resolve_self_critique_memory(agent, app_state)

    return _resolve


def build_turn_self_critique_request(
    result: Any,
    *,
    trigger: CritiqueTrigger,
    session_id: str = "",
    user_message: str = "",
    goal: str = "",
) -> SelfCritiqueRequest:
    """Build a Reflexion request from a failed AgentLoop turn result."""
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    trajectory: list[TrajectoryEvent] = []
    for call in tool_calls[-12:]:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        error = str(call.get("error") or call.get("error_signature") or "")
        ok_raw = call.get("ok")
        ok = ok_raw if isinstance(ok_raw, bool) else (False if error else None)
        content = str(
            call.get("content")
            or call.get("result")
            or call.get("args")
            or call,
        )
        trajectory.append(TrajectoryEvent(
            kind="tool_call",
            content=content,
            ok=ok,
            tool_name=name,
            error=error,
        ))
    if user_message:
        trajectory.insert(0, TrajectoryEvent(
            kind="user_message",
            content=user_message,
            ok=None,
        ))
    failure_summary = str(getattr(result, "error", "") or "")
    if not failure_summary:
        failure_summary = "agent turn failed without an explicit error"
    graph_state = {
        "final": "failed",
        "hops": int(getattr(result, "hops", 0) or 0),
        "tool_call_count": len(tool_calls),
        "error": failure_summary,
        "trigger": trigger,
    }
    return SelfCritiqueRequest(
        trigger=trigger,
        session_id=session_id,
        goal=goal or user_message[:240],
        failure_summary=failure_summary,
        trajectory=tuple(trajectory),
        graph_state=graph_state,
    )


async def maybe_run_turn_self_critique(
    agent: Any,
    result: Any,
    *,
    trigger: CritiqueTrigger,
    session_id: str = "",
    user_message: str = "",
    goal: str = "",
) -> SelfCritiqueRunResult | None:
    """Best-effort Reflexion hook for failed AgentLoop exits."""
    engine = getattr(agent, "_self_critique_engine", None)
    if engine is None:
        return None
    critic_call = getattr(agent, "_self_critique_critic_call", None)
    memory_service = None
    resolver = getattr(agent, "_self_critique_memory_resolver", None)
    if callable(resolver):
        try:
            memory_service = resolver()
        except Exception:  # noqa: BLE001
            memory_service = None
    if memory_service is None:
        memory_service = getattr(agent, "_memory_service", None)
    request = build_turn_self_critique_request(
        result,
        trigger=trigger,
        session_id=session_id,
        user_message=user_message,
        goal=goal,
    )
    await _publish_self_critique_requested(agent, request)
    try:
        run_result = await engine.run(
            request,
            critic_call=critic_call,
            memory_service=memory_service,
        )
        setattr(agent, "_last_self_critique_request", request)
        setattr(agent, "_last_self_critique_result", run_result)
        return run_result
    except Exception:  # noqa: BLE001
        return None


async def _publish_self_critique_requested(
    agent: Any,
    request: SelfCritiqueRequest,
) -> None:
    bus = getattr(agent, "_bus", None)
    publish = getattr(bus, "publish", None)
    if not callable(publish):
        return
    try:
        await publish(
            make_event(
                session_id=request.session_id,
                agent_id=str(getattr(agent, "_agent_id", "") or "agent"),
                type=EventType.SELF_CRITIQUE_REQUESTED,
                payload={
                    "trigger": request.trigger,
                    "session_id": request.session_id,
                    "goal": request.goal,
                    "failure_summary": request.failure_summary,
                    "trajectory_events": len(request.trajectory),
                    "graph_state": dict(request.graph_state),
                    "source": "agent_loop",
                },
            ),
        )
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "build_self_critique_critic_call",
    "build_turn_self_critique_request",
    "make_self_critique_memory_resolver",
    "maybe_run_turn_self_critique",
    "resolve_self_critique_memory",
]
