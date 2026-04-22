"""BuiltinTools -- ``todo_write`` / ``todo_read`` unit tests.

Covers:
  - Happy-path write + read-back.
  - Per-session isolation via ToolCall.session_id.
  - Validation (missing items, bad status enum, empty content).
  - Listener callback fires on every successful write.
"""
from __future__ import annotations

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools

import pytest


def _call(name: str, args: dict, *, session_id: str | None = None) -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_todo_write_then_read_happy_path() -> None:
    tools = BuiltinTools()
    items = [
        {"content": "design the schema", "status": "done"},
        {"content": "write the migration", "status": "in_progress"},
        {"content": "backfill test data", "status": "pending"},
    ]
    w = await tools.invoke(_call("todo_write", {"items": items}, session_id="s1"))
    assert w.ok is True
    assert "3 todos" in w.content
    assert "1 done" in w.content
    assert "1 in progress" in w.content

    r = await tools.invoke(_call("todo_read", {}, session_id="s1"))
    assert r.ok is True
    assert "[x] design the schema" in r.content
    assert "[~] write the migration" in r.content
    assert "[ ] backfill test data" in r.content


@pytest.mark.asyncio
async def test_todo_sessions_are_isolated() -> None:
    """Writing in session A must not leak into session B."""
    tools = BuiltinTools()
    await tools.invoke(_call("todo_write", {
        "items": [{"content": "A only", "status": "pending"}],
    }, session_id="alpha"))
    r = await tools.invoke(_call("todo_read", {}, session_id="beta"))
    assert r.ok is True
    assert "A only" not in r.content
    assert "no todos" in r.content


@pytest.mark.asyncio
async def test_todo_write_replaces_full_list() -> None:
    tools = BuiltinTools()
    await tools.invoke(_call("todo_write", {
        "items": [
            {"content": "old-1", "status": "pending"},
            {"content": "old-2", "status": "pending"},
        ],
    }, session_id="s1"))
    await tools.invoke(_call("todo_write", {
        "items": [{"content": "new-only", "status": "done"}],
    }, session_id="s1"))
    r = await tools.invoke(_call("todo_read", {}, session_id="s1"))
    assert "old-1" not in r.content
    assert "old-2" not in r.content
    assert "new-only" in r.content


@pytest.mark.asyncio
async def test_todo_write_validates_inputs() -> None:
    tools = BuiltinTools()

    r = await tools.invoke(_call("todo_write", {}))
    assert r.ok is False
    assert "items" in r.error

    r = await tools.invoke(_call("todo_write", {
        "items": [{"content": "", "status": "pending"}],
    }))
    assert r.ok is False
    assert "non-empty" in r.error

    r = await tools.invoke(_call("todo_write", {
        "items": [{"content": "x", "status": "random-garbage"}],
    }))
    assert r.ok is False
    assert "status" in r.error

    r = await tools.invoke(_call("todo_write", {
        "items": ["not a dict"],
    }))
    assert r.ok is False
    assert "object" in r.error


@pytest.mark.asyncio
async def test_todo_listener_fires_on_write() -> None:
    seen: list[tuple[str, list]] = []

    def _listener(sid: str, items: list) -> None:
        seen.append((sid, list(items)))

    tools = BuiltinTools(todo_listener=_listener)
    await tools.invoke(_call("todo_write", {
        "items": [{"content": "watch this", "status": "pending"}],
    }, session_id="sess-x"))
    assert len(seen) == 1
    assert seen[0][0] == "sess-x"
    assert seen[0][1][0]["content"] == "watch this"


@pytest.mark.asyncio
async def test_todo_write_emits_bus_event_via_agent_loop() -> None:
    """Integration within AgentLoop: TODO_UPDATED event fires on the bus
    with the items payload whenever the model calls todo_write."""
    import asyncio
    from collections.abc import AsyncIterator
    from dataclasses import dataclass, field

    from xmclaw.core.bus import EventType, InProcessEventBus, BehavioralEvent
    from xmclaw.core.bus.memory import accept_all
    from xmclaw.core.ir import ToolCall, ToolCallShape
    from xmclaw.daemon.agent_loop import AgentLoop
    from xmclaw.providers.llm.base import (
        LLMChunk, LLMProvider, LLMResponse, Message, Pricing,
    )

    @dataclass
    class _LLM(LLMProvider):
        script: list[LLMResponse] = field(default_factory=list)
        _i: int = 0
        model: str = "t"

        async def stream(  # pragma: no cover
            self, messages, tools=None, *, cancel=None,
        ) -> AsyncIterator[LLMChunk]:
            if False:
                yield  # type: ignore[unreachable]

        async def complete(self, messages, tools=None):  # noqa: ANN001
            r = self.script[self._i]; self._i += 1; return r

        @property
        def tool_call_shape(self) -> ToolCallShape:
            return ToolCallShape.ANTHROPIC_NATIVE

        @property
        def pricing(self) -> Pricing:
            return Pricing()

    bus = InProcessEventBus()
    received: list[BehavioralEvent] = []
    bus.subscribe(accept_all, lambda e: received.append(e) or None)  # type: ignore[func-returns-value]

    llm = _LLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="todo_write",
                args={"items": [{"content": "step one", "status": "pending"}]},
                provenance="anthropic", id="t1",
            ),),
        ),
        LLMResponse(content="done"),
    ])
    tools = BuiltinTools()
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-bus", "plan one step")
    # drain the bus so subscribers see every event
    await bus.drain()

    updated = [e for e in received if e.type == EventType.TODO_UPDATED]
    assert len(updated) == 1, f"expected 1 TODO_UPDATED, got {len(updated)}"
    assert updated[0].session_id == "sess-bus"
    assert updated[0].payload["items"][0]["content"] == "step one"
    assert updated[0].payload["count"] == 1
