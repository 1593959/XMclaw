"""Unit tests for SessionReflector — background cross-session lesson distillation."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.bus.sqlite import SqliteEventBus
from xmclaw.daemon.session_reflector import SessionReflector


class _ScriptedLLM:
    """Returns a fixed JSON facts array; records how many times called."""

    def __init__(self, facts: list[dict]) -> None:
        self._payload = json.dumps(facts, ensure_ascii=False)
        self.calls = 0

    async def complete(self, messages, tools=None):  # noqa: ARG002
        self.calls += 1
        return SimpleNamespace(content=self._payload)


class _StubMemory:
    def __init__(self) -> None:
        self.remembered: list[dict] = []

    async def remember(self, text, **kw):
        self.remembered.append({"text": text, **kw})
        return SimpleNamespace(text=text)


async def _seed(bus: SqliteEventBus, sid: str) -> None:
    evs = [
        make_event(session_id=sid, agent_id="a", type=EventType.USER_MESSAGE,
                   payload={"content": "把看板做成自动抓取"}),
        make_event(session_id=sid, agent_id="a", type=EventType.TOOL_INVOCATION_FINISHED,
                   payload={"name": "code_python", "ok": False, "error": "ModuleNotFoundError: flask"}),
        make_event(session_id=sid, agent_id="a", type=EventType.LLM_RESPONSE,
                   payload={"content": "装了 flask 后重跑，服务起来了", "ok": True}),
        make_event(session_id=sid, agent_id="a", type=EventType.PLAN_FAILED,
                   payload={"error": "max_hops"}),
    ]
    for e in evs:
        await bus.publish(e)


@pytest.mark.asyncio
async def test_reflect_writes_facts_then_is_incremental(tmp_path):
    db = tmp_path / "events.db"
    bus = SqliteEventBus(db_path=db)
    await _seed(bus, "chat-x")

    llm = _ScriptedLLM([
        {"text": "code_python 缺 flask 依赖会 ModuleNotFoundError", "kind": "lesson", "confidence": 0.9},
        {"text": "装 flask 后服务可启动", "kind": "correction", "confidence": 0.8},
    ])
    mem = _StubMemory()
    refl = SessionReflector(
        llm=llm, memory_service=mem,
        events_db_path=db, state_path=tmp_path / "state.json",
    )

    r1 = await refl.reflect_once()
    assert r1["ok"] and r1["sessions"] == 1
    assert r1["facts"] == 2
    assert len(mem.remembered) == 2
    # bucket + provenance routed correctly
    assert all(m["provenance"] == "session_reflector" for m in mem.remembered)
    assert all(m["bucket"] == "failure_modes" for m in mem.remembered)

    # Second pass: no NEW events → incremental watermark skips it (no LLM call).
    calls_before = llm.calls
    r2 = await refl.reflect_once()
    assert r2["sessions"] == 0
    assert llm.calls == calls_before  # didn't re-distil unchanged session
    bus.close()


@pytest.mark.asyncio
async def test_reflect_skips_thin_sessions(tmp_path):
    db = tmp_path / "events.db"
    bus = SqliteEventBus(db_path=db)
    # only 1 salient event — below _MIN_NEW_EVENTS, should be skipped
    await bus.publish(make_event(
        session_id="chat-thin", agent_id="a",
        type=EventType.USER_MESSAGE, payload={"content": "hi"},
    ))
    llm = _ScriptedLLM([{"text": "x", "kind": "lesson", "confidence": 0.5}])
    mem = _StubMemory()
    refl = SessionReflector(
        llm=llm, memory_service=mem,
        events_db_path=db, state_path=tmp_path / "state.json",
    )
    r = await refl.reflect_once()
    assert r["facts"] == 0
    assert llm.calls == 0  # never bothered the LLM for a thin session
    bus.close()


@pytest.mark.asyncio
async def test_internal_sessions_excluded(tmp_path):
    db = tmp_path / "events.db"
    bus = SqliteEventBus(db_path=db)
    await _seed(bus, "_system")  # internal session id
    llm = _ScriptedLLM([{"text": "x", "kind": "lesson", "confidence": 0.5}])
    mem = _StubMemory()
    refl = SessionReflector(
        llm=llm, memory_service=mem,
        events_db_path=db, state_path=tmp_path / "state.json",
    )
    r = await refl.reflect_once()
    assert r["sessions"] == 0
    assert llm.calls == 0
    bus.close()
