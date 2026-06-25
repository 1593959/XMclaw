from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from xmclaw.daemon.self_critique_runtime import (
    build_self_critique_critic_call,
    build_turn_self_critique_request,
    make_self_critique_memory_resolver,
    maybe_run_turn_self_critique,
    resolve_self_critique_memory,
)
from xmclaw.core.bus.events import EventType
from xmclaw.core.bus.memory import InProcessEventBus
from xmclaw.daemon.turn_types import AgentTurnResult


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Any], tools: list[Any] | None = None) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        return SimpleNamespace(content='{"ok": true}')


@pytest.mark.asyncio
async def test_build_self_critique_critic_call_wraps_llm_complete() -> None:
    llm = _FakeLLM()
    critic_call = build_self_critique_critic_call(llm)

    assert critic_call is not None
    raw = await critic_call("critique this trajectory")

    assert raw == '{"ok": true}'
    assert len(llm.calls) == 1
    messages = llm.calls[0]["messages"]
    assert [m.role for m in messages] == ["system", "user"]
    assert "Reflexion critic" in messages[0].content
    assert messages[1].content == "critique this trajectory"
    assert llm.calls[0]["tools"] == []


def test_build_self_critique_critic_call_returns_none_without_complete() -> None:
    assert build_self_critique_critic_call(object()) is None


def test_resolve_self_critique_memory_prefers_agent_then_app_state() -> None:
    agent_memory = object()
    app_memory = object()
    agent = SimpleNamespace(_memory_service=agent_memory)
    app_state = SimpleNamespace(memory_v2_service=app_memory)

    assert resolve_self_critique_memory(agent, app_state) is agent_memory
    assert resolve_self_critique_memory(SimpleNamespace(), app_state) is app_memory
    assert resolve_self_critique_memory(None, None) is None


def test_make_self_critique_memory_resolver_is_late_binding() -> None:
    agent = SimpleNamespace(_memory_service=None)
    app_state = SimpleNamespace(memory_v2_service=None)
    resolver = make_self_critique_memory_resolver(agent, app_state)

    assert resolver() is None
    memory = object()
    agent._memory_service = memory
    assert resolver() is memory


def test_build_turn_self_critique_request_compacts_failed_turn() -> None:
    result = AgentTurnResult(
        ok=False,
        text="failed",
        hops=3,
        tool_calls=[
            {"name": "bash", "args": {"command": "exit 1"}, "error": "exit 1"},
        ],
        error="hit max_hops=3",
    )

    request = build_turn_self_critique_request(
        result,
        trigger="max_hops_exit",
        session_id="sess",
        user_message="please finish",
    )

    assert request.trigger == "max_hops_exit"
    assert request.session_id == "sess"
    assert request.failure_summary == "hit max_hops=3"
    assert request.graph_state["final"] == "failed"
    assert request.graph_state["hops"] == 3
    assert request.trajectory[0].kind == "user_message"
    assert request.trajectory[1].tool_name == "bash"
    assert request.trajectory[1].ok is False


@pytest.mark.asyncio
async def test_maybe_run_turn_self_critique_is_best_effort() -> None:
    calls: list[dict[str, Any]] = []

    class _Engine:
        async def run(self, request, *, critic_call, memory_service):
            calls.append({
                "request": request,
                "critic_call": critic_call,
                "memory_service": memory_service,
            })
            return SimpleNamespace(status="completed", request=request)

    memory = object()
    bus = InProcessEventBus()
    events = []

    async def _capture(event):
        events.append(event)

    bus.subscribe(lambda e: True, _capture)
    agent = SimpleNamespace(
        _self_critique_engine=_Engine(),
        _self_critique_critic_call=lambda prompt: prompt,
        _memory_service=memory,
        _bus=bus,
        _agent_id="agent-test",
    )
    result = AgentTurnResult(
        ok=False,
        text="failed",
        hops=1,
        error="stuck_loop",
    )

    run_result = await maybe_run_turn_self_critique(
        agent,
        result,
        trigger="stuck_loop_exit",
        session_id="sess",
        user_message="go",
    )

    assert run_result is not None
    await bus.drain()
    assert len(calls) == 1
    assert calls[0]["request"].trigger == "stuck_loop_exit"
    assert calls[0]["memory_service"] is memory
    assert agent._last_self_critique_result is run_result
    critique_events = [
        e for e in events if e.type == EventType.SELF_CRITIQUE_REQUESTED
    ]
    assert len(critique_events) == 1
    assert critique_events[0].agent_id == "agent-test"
    assert critique_events[0].payload["source"] == "agent_loop"
    assert critique_events[0].payload["trigger"] == "stuck_loop_exit"
