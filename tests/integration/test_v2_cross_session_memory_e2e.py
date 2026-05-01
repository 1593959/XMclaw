"""End-to-end test: cross-session memory injection + write-back.

Proves the user's complaint "记忆跨会话也没有" is fixed. Phase 1 of
``docs/DEV_PLAN.md`` §1.2: AgentLoop now calls memory.put() at end of
turn and memory.query() at start of the next turn, with a
<memory-context> fence around injected text.

Mirrors the open-webui chat_memory_handler shape (no embedder needed —
text fallback works) plus Hermes's <memory-context> fence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Message, Pricing
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


@dataclass
class _RecordingLLM(LLMProvider):
    """Tiny fake LLM that records the messages it received and replies a fixed string."""

    reply: str = "ok"
    last_messages: list[Message] = field(default_factory=list)

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.last_messages = list(messages)
        return LLMResponse(content=self.reply, tool_calls=())

    async def stream(self, messages, tools=None, *, cancel=None):
        yield  # type: ignore[misc]

    @property
    def tool_call_shape(self):
        from xmclaw.core.ir import ToolCallShape
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.mark.asyncio
async def test_memory_injects_prior_session_into_new_turn(tmp_path):
    db = tmp_path / "memory.db"
    memory = SqliteVecMemory(db_path=db)
    bus = InProcessEventBus()
    llm = _RecordingLLM(reply="Got it. The build broke at line 47.")

    # === Session A: user mentions a fact ===
    loop_a = AgentLoop(
        llm=llm,
        bus=bus,
        memory=memory,
        memory_top_k=3,
        agent_id="test",
    )
    res_a = await loop_a.run_turn(
        session_id="session-a",
        user_message="The build broke at line 47 of main.py",
    )
    assert res_a.ok

    # The turn is now in long-term memory.
    items = await memory.query(layer="long", k=10)
    assert len(items) == 1
    assert "line 47" in items[0].text
    assert items[0].metadata["session_id"] == "session-a"

    # === Session B (new session, fresh AgentLoop) ===
    # The new session should see the prior fact via memory injection.
    # Simulate enough wall-clock gap that the 60s recency filter doesn't
    # exclude the just-written turn — push the row's ts back 120s.
    memory._conn.execute(
        "UPDATE memory_items SET ts = ts - 120 WHERE id = ?",
        (items[0].id,),
    )
    memory._conn.commit()

    loop_b = AgentLoop(
        llm=llm,
        bus=bus,
        memory=memory,
        memory_top_k=3,
        agent_id="test",
    )
    res_b = await loop_b.run_turn(
        session_id="session-b",
        user_message="Where did the build break?",
    )
    assert res_b.ok

    # The user message that the LLM saw must contain the memory fence
    # plus the prior fact — proving cross-session injection works.
    user_msg = next(m for m in llm.last_messages if m.role == "user")
    assert "<memory-context>" in user_msg.content
    assert "</memory-context>" in user_msg.content
    assert "line 47" in user_msg.content
    # And the original user message is preserved at the head:
    assert user_msg.content.startswith("Where did the build break?")

    memory.close()


@pytest.mark.asyncio
async def test_memory_excludes_same_session(tmp_path):
    """The current session's own write must not be re-injected — that
    would double the user's just-spoken sentence in the next turn."""
    db = tmp_path / "memory.db"
    memory = SqliteVecMemory(db_path=db)
    bus = InProcessEventBus()
    llm = _RecordingLLM(reply="Noted.")

    loop = AgentLoop(
        llm=llm, bus=bus, memory=memory, agent_id="test",
    )
    # Same session — second turn must NOT see the first turn via memory
    # injection (history serves that role).
    await loop.run_turn(
        session_id="solo",
        user_message="My favourite editor is Helix.",
    )
    await loop.run_turn(
        session_id="solo",
        user_message="What did I just say?",
    )
    user_msg = next(m for m in llm.last_messages if m.role == "user")
    # "Helix" is NOT pulled from memory into the current turn (it would
    # come in via history instead, which is the right path).
    assert "<memory-context>" not in user_msg.content

    memory.close()


@pytest.mark.asyncio
async def test_memory_optional_no_failures_when_unset(tmp_path):
    """When no memory is wired, run_turn behaves exactly as before."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(reply="ok")
    loop = AgentLoop(llm=llm, bus=bus, memory=None, agent_id="test")
    res = await loop.run_turn(session_id="s", user_message="hello")
    assert res.ok
    user_msg = next(m for m in llm.last_messages if m.role == "user")
    # Plain user message — no fence injected.
    assert user_msg.content == "hello"
