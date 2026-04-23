"""HTTP ``/api/v2/events`` endpoint — Epic #13 event-log replay.

Covers both backends: the persistent :class:`SqliteEventBus` path (the one
``xmclaw serve`` uses in practice) and the in-memory fallback that
``create_app()`` uses when no bus is supplied. Same query contract either
way; the ``bus`` field in the response tells clients which backend served
the data.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import (
    EventType,
    InProcessEventBus,
    SqliteEventBus,
    make_event,
)
from xmclaw.daemon.app import create_app


@pytest.fixture()
def loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Dedicated event loop per test.

    ``asyncio.get_event_loop()`` is unreliable in Python 3.10+ once another
    test has closed its own loop (pytest-asyncio's loop goes away between
    tests). Creating a fresh loop here makes sync tests deterministic when
    mixed into a larger suite.
    """
    l = asyncio.new_event_loop()
    try:
        yield l
    finally:
        l.close()


# --------------------------------------------------------------------------- #
# Sqlite-backed bus — end-to-end contract
# --------------------------------------------------------------------------- #


def test_events_endpoint_returns_sqlite_backed_rows(
    tmp_path: Path, loop: asyncio.AbstractEventLoop,
) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        app = create_app(bus=bus)
        client = TestClient(app)

        loop.run_until_complete(bus.publish(make_event(
            session_id="alpha", agent_id="a", type=EventType.USER_MESSAGE,
            payload={"content": "hello"},
        )))
        loop.run_until_complete(bus.publish(make_event(
            session_id="alpha", agent_id="a", type=EventType.LLM_RESPONSE,
            payload={"content": "hi back"},
        )))
        loop.run_until_complete(bus.publish(make_event(
            session_id="beta", agent_id="a", type=EventType.USER_MESSAGE,
            payload={"content": "elsewhere"},
        )))

        r = client.get("/api/v2/events", params={"session_id": "alpha"})
        assert r.status_code == 200
        body = r.json()
        assert body["bus"] == "SqliteEventBus"
        assert body["count"] == 2
        kinds = [e["type"] for e in body["events"]]
        assert kinds == ["user_message", "llm_response"]
    finally:
        bus.close()


def test_events_endpoint_type_filter(
    tmp_path: Path, loop: asyncio.AbstractEventLoop,
) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        app = create_app(bus=bus)
        client = TestClient(app)

        loop.run_until_complete(bus.publish(make_event(
            session_id="s", agent_id="a", type=EventType.USER_MESSAGE,
        )))
        loop.run_until_complete(bus.publish(make_event(
            session_id="s", agent_id="a", type=EventType.COST_TICK,
        )))
        loop.run_until_complete(bus.publish(make_event(
            session_id="s", agent_id="a", type=EventType.LLM_RESPONSE,
        )))

        r = client.get("/api/v2/events", params={
            "session_id": "s",
            "types": "user_message,llm_response",
        })
        body = r.json()
        assert {e["type"] for e in body["events"]} == {"user_message", "llm_response"}
    finally:
        bus.close()


def test_events_endpoint_fts_search(
    tmp_path: Path, loop: asyncio.AbstractEventLoop,
) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        app = create_app(bus=bus)
        client = TestClient(app)

        loop.run_until_complete(bus.publish(make_event(
            session_id="s", agent_id="a", type=EventType.USER_MESSAGE,
            payload={"content": "remember the pairing token"},
        )))
        loop.run_until_complete(bus.publish(make_event(
            session_id="s", agent_id="a", type=EventType.USER_MESSAGE,
            payload={"content": "nothing to see here"},
        )))

        r = client.get("/api/v2/events", params={"q": "pairing"})
        body = r.json()
        assert body["count"] == 1
        assert "pairing" in body["events"][0]["payload"]["content"]
    finally:
        bus.close()


def test_events_endpoint_limit_clamped(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        app = create_app(bus=bus)
        client = TestClient(app)

        # Hard upper clamp is 2000; send a > 2000 request and ensure we
        # don't blow through the clamp.
        r = client.get("/api/v2/events", params={"limit": 99999})
        assert r.status_code == 200
        assert r.json()["count"] == 0  # empty DB
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# In-memory fallback — same contract, different backend label
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_events_endpoint_falls_back_to_in_memory_log() -> None:
    bus = InProcessEventBus()
    app = create_app(bus=bus)
    client = TestClient(app)

    await bus.publish(make_event(
        session_id="s", agent_id="a", type=EventType.USER_MESSAGE,
        payload={"content": "memory-only"},
    ))
    await bus.drain()

    r = client.get("/api/v2/events", params={"session_id": "s"})
    body = r.json()
    assert body["bus"] == "InProcessEventBus"
    assert body["count"] == 1
    assert body["events"][0]["payload"]["content"] == "memory-only"
