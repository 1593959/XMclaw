"""SessionStore + AgentLoop persistence — unit tests.

Covers:
  1. Round-trip: save messages, load them back, identical content
  2. Tool-call messages survive (round-trip preserves ToolCall fields)
  3. ``list_recent`` orders newest-first and reports counts
  4. ``delete`` removes the row
  5. AgentLoop hydrates from store on cold-start (in-memory empty)
  6. AgentLoop persists after every turn — survives a fresh AgentLoop
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.session_store import SessionStore
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


@dataclass
class _ScriptedLLM(LLMProvider):
    script: list[LLMResponse] = field(default_factory=list)
    model: str = "scripted"
    _i: int = 0

    async def stream(  # pragma: no cover
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


def test_round_trip_plain_messages(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello!"),
        Message(role="user", content="what's 2+2?"),
        Message(role="assistant", content="4"),
    ]
    store.save("sess-a", msgs)
    loaded = store.load("sess-a")
    assert loaded is not None
    assert [(m.role, m.content) for m in loaded] == [
        ("user", "hi"),
        ("assistant", "hello!"),
        ("user", "what's 2+2?"),
        ("assistant", "4"),
    ]


def test_round_trip_tool_call_message(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    tc = ToolCall(
        name="bash",
        args={"command": "ls"},
        provenance="anthropic",
        id="call_42",
        raw_snippet="<tool_use>...</tool_use>",
    )
    msgs = [
        Message(role="user", content="list files"),
        Message(role="assistant", content="", tool_calls=(tc,)),
        Message(role="tool", content="a.txt\nb.txt", tool_call_id="call_42"),
        Message(role="assistant", content="two files."),
    ]
    store.save("sess-tc", msgs)
    loaded = store.load("sess-tc")
    assert loaded is not None
    assert len(loaded) == 4
    assistant = loaded[1]
    assert assistant.role == "assistant"
    assert len(assistant.tool_calls) == 1
    rt = assistant.tool_calls[0]
    assert rt.name == "bash"
    assert rt.args == {"command": "ls"}
    assert rt.id == "call_42"
    assert rt.provenance == "anthropic"
    tool_msg = loaded[2]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_42"


def test_strips_system_messages_on_save(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    msgs = [
        Message(role="system", content="you are a helpful agent"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    store.save("sess-sys", msgs)
    loaded = store.load("sess-sys")
    assert loaded is not None
    assert all(m.role != "system" for m in loaded)
    assert len(loaded) == 2


def test_load_unknown_session_returns_none(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    assert store.load("never-existed") is None


def test_list_recent_orders_newest_first(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.save("old", [Message(role="user", content="x")])
    store.save("middle", [Message(role="user", content="y"), Message(role="assistant", content="z")])
    store.save("newest", [Message(role="user", content="latest")])
    rows = store.list_recent(limit=10)
    sids = [r["session_id"] for r in rows]
    assert sids == ["newest", "middle", "old"]
    by_sid = {r["session_id"]: r["message_count"] for r in rows}
    assert by_sid["middle"] == 2
    assert by_sid["old"] == 1


def test_delete_removes_row(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.save("doomed", [Message(role="user", content="bye")])
    assert store.load("doomed") is not None
    store.delete("doomed")
    assert store.load("doomed") is None
    assert store.list_recent() == []


@pytest.mark.asyncio
async def test_agent_loop_persists_after_each_turn(tmp_path) -> None:
    """A fresh AgentLoop with the same store sees the prior turn's history."""
    db = tmp_path / "sessions.db"
    bus = InProcessEventBus()
    store = SessionStore(db)
    llm1 = _ScriptedLLM(script=[
        LLMResponse(content="hi back", tool_calls=()),
    ])
    agent1 = AgentLoop(llm=llm1, bus=bus, session_store=store)
    await agent1.run_turn("sess-resume", "hello")
    await bus.drain()

    # Second AgentLoop starts cold (new in-memory cache) but the same store.
    llm2 = _ScriptedLLM(script=[
        LLMResponse(content="and again", tool_calls=()),
    ])
    agent2 = AgentLoop(llm=llm2, bus=bus, session_store=store)
    await agent2.run_turn("sess-resume", "still there?")
    await bus.drain()

    # The 2nd LLM must have seen the prior exchange in its prompt.
    # _ScriptedLLM doesn't expose received messages, so verify via store: 4 msgs.
    saved = store.load("sess-resume")
    assert saved is not None
    roles = [m.role for m in saved]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert saved[0].content == "hello"
    assert saved[1].content == "hi back"
    assert saved[2].content == "still there?"
    assert saved[3].content == "and again"


@pytest.mark.asyncio
async def test_clear_session_drops_persisted_history(tmp_path) -> None:
    bus = InProcessEventBus()
    store = SessionStore(tmp_path / "sessions.db")
    llm = _ScriptedLLM(script=[LLMResponse(content="hi", tool_calls=())])
    agent = AgentLoop(llm=llm, bus=bus, session_store=store)
    await agent.run_turn("sess-clear", "hello")
    await bus.drain()
    assert store.load("sess-clear") is not None

    agent.clear_session("sess-clear")
    assert store.load("sess-clear") is None
