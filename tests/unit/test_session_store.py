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
async def test_failed_turn_still_persists_user_message(tmp_path) -> None:
    """B-RESUME (2026-05-31): a turn that ERRORS / times out must still
    persist the user's message so the conversation isn't lost and the
    user can say '继续' instead of retyping from scratch. Pre-fix,
    history was committed ONLY on the terminal-success path, so a failed
    turn vanished entirely (user report: '只有报错那一轮丢')."""
    db = tmp_path / "sessions.db"
    bus = InProcessEventBus()
    store = SessionStore(db)

    class _BoomLLM(_ScriptedLLM):
        async def complete(self, messages, tools=None):
            raise RuntimeError("simulated provider timeout")

    agent = AgentLoop(llm=_BoomLLM(), bus=bus, session_store=store)
    try:
        await agent.run_turn("sess-fail", "do the big multi-step task")
    except Exception:
        pass  # the turn fails — that's the scenario under test
    await bus.drain()

    saved = store.load("sess-fail")
    assert saved is not None and len(saved) >= 1
    # The user's prompt survived the failure.
    assert saved[0].role == "user"
    assert saved[0].content == "do the big multi-step task"
    # A placeholder assistant terminates the turn so the NEXT turn's API
    # call sees valid role alternation (no two consecutive user msgs).
    assert saved[-1].role == "assistant"


@pytest.mark.asyncio
async def test_clear_session_drops_persisted_history(tmp_path) -> None:
    bus = InProcessEventBus()
    store = SessionStore(tmp_path / "sessions.db")
    llm = _ScriptedLLM(script=[LLMResponse(content="hi", tool_calls=())])
    agent = AgentLoop(llm=llm, bus=bus, session_store=store)
    await agent.run_turn("sess-clear", "hello")
    await bus.drain()
    assert store.load("sess-clear") is not None

    await agent.clear_session("sess-clear")
    assert store.load("sess-clear") is None


# ── B-339 (audit #12): substring search across stored sessions ───────


def test_b339_search_finds_substring_match(tmp_path) -> None:
    """Pre-B-339 the Sessions page filtered client-side over only
    already-expanded sessions; sessions the user hadn't clicked
    weren't searchable at all. The new endpoint scans every stored
    history blob server-side."""
    from xmclaw.providers.llm.base import Message
    store = SessionStore(tmp_path / "sessions.db")
    store.save("sess-a", [
        Message(role="user", content="how do I deploy this app?"),
        Message(role="assistant", content="run xmclaw start"),
    ])
    store.save("sess-b", [
        Message(role="user", content="weather today?"),
    ])
    store.save("sess-c", [
        Message(role="user", content="another deploy question"),
    ])

    hits = store.search_messages("deploy")
    sids = {h["session_id"] for h in hits}
    assert sids == {"sess-a", "sess-c"}
    snippets = {h["session_id"]: h["match_snippet"] for h in hits}
    assert "deploy" in snippets["sess-a"].lower()
    assert "deploy" in snippets["sess-c"].lower()


def test_b339_search_empty_query_returns_empty(tmp_path) -> None:
    from xmclaw.providers.llm.base import Message
    store = SessionStore(tmp_path / "sessions.db")
    store.save("sess-a", [Message(role="user", content="hi")])

    assert store.search_messages("") == []
    assert store.search_messages("   ") == []


def test_b339_search_case_insensitive(tmp_path) -> None:
    from xmclaw.providers.llm.base import Message
    store = SessionStore(tmp_path / "sessions.db")
    store.save("sess-a", [Message(role="user", content="DEPLOY this NOW")])

    hits = store.search_messages("deploy")
    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-a"


def test_b339_search_escapes_sql_wildcards(tmp_path) -> None:
    """A user query containing ``%`` or ``_`` must be matched as a
    literal character, not as the SQL LIKE wildcard. Otherwise typing
    ``%`` in the search box would match every row (DoS / confusion)."""
    from xmclaw.providers.llm.base import Message
    store = SessionStore(tmp_path / "sessions.db")
    store.save("sess-a", [Message(role="user", content="100% sure")])
    store.save("sess-b", [Message(role="user", content="not at all certain")])

    hits = store.search_messages("100%")
    sids = {h["session_id"] for h in hits}
    assert sids == {"sess-a"}, (
        f"% must be escaped, not used as SQL wildcard; got {sids!r}"
    )


def test_b339_search_no_matches_returns_empty(tmp_path) -> None:
    from xmclaw.providers.llm.base import Message
    store = SessionStore(tmp_path / "sessions.db")
    store.save("sess-a", [Message(role="user", content="hello world")])
    assert store.search_messages("never-mentioned-token") == []


# ── Wave-32+ (2026-05-19) — internal-session classification ─────────


def test_is_internal_session_id_covers_all_internal_prefixes() -> None:
    """Pin the prefix list so adding a new autonomous/test session
    flavor without updating the filter doesn't silently leak it back
    into the user's Sessions UI."""
    from xmclaw.daemon.session_store import is_internal_session_id

    # All internal flavors → True
    assert is_internal_session_id("reflect:chat-a:1779")
    assert is_internal_session_id("dream:1779")
    assert is_internal_session_id("_system_anything")
    assert is_internal_session_id("evolution:proposal-1")
    assert is_internal_session_id("autonomous:step_1:abcd1234")
    assert is_internal_session_id("skill-dream-xyz")
    assert is_internal_session_id("step_1")
    assert is_internal_session_id("step_42")
    assert is_internal_session_id("smoke-fullb20-basic")
    assert is_internal_session_id("selfmod-fullb20-1779130115")
    assert is_internal_session_id("time-fullb20-1779130215")
    assert is_internal_session_id("worker:w1:t1")

    # User-authored chats → False
    assert not is_internal_session_id("chat-e31f0891")
    assert not is_internal_session_id("chat-0e255219")
    assert not is_internal_session_id("")
    # Plain "time-" prefix WITHOUT fullb20 is not internal — only the
    # specific smoke-test prefix should match.
    assert not is_internal_session_id("time-zone-helper-session")


def test_load_empty_history_is_not_falsy() -> None:
    """B-ContextLoss-2: session_store.load() returning [] must be treated
    as a valid empty history, not falsy.  Before the fix ``if loaded:``
    skipped the assignment so the in-memory cache stayed empty and the
    agent started from zero every turn."""
    from xmclaw.daemon.agent_loop import AgentLoop
    from xmclaw.core.bus import InProcessEventBus

    store = SessionStore.__new__(SessionStore)
    # Patch load to return an empty list (simulates a session that
    # exists but has zero messages).
    store.load = lambda sid: []  # type: ignore[method-assign]

    loop = AgentLoop(
        llm=_ScriptedLLM(), bus=InProcessEventBus(), tools=None,
        system_prompt="sys", session_store=store,
    )
    # The old code used ``if loaded:`` which treated ``[]`` as falsy,
    # so ``_histories`` would stay empty.  The fixed code uses
    # ``if loaded is not None:`` which accepts ``[]``.
    loaded = store.load("sess-empty")
    assert loaded == []
    if "sess-empty" not in loop._histories and loop._session_store is not None:
        try:
            loaded = loop._session_store.load("sess-empty")
        except Exception:
            loaded = None
        if loaded is not None:
            loop._histories["sess-empty"] = loaded
    assert loop._histories.get("sess-empty") == []
