"""Tests for :mod:`xmclaw.core.bus.sqlite` (Epic #13).

Covers: schema creation, publish-then-fan-out ordering, query filters,
FTS5 keyword search, session aggregates, batch publish, and durability
across reopen.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    SqliteEventBus,
    make_event,
)
from xmclaw.core.bus.memory import accept_all


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ev(
    *,
    session_id: str = "s1",
    agent_id: str = "a1",
    type: EventType = EventType.USER_MESSAGE,  # noqa: A002
    payload: dict | None = None,
) -> BehavioralEvent:
    return make_event(
        session_id=session_id,
        agent_id=agent_id,
        type=type,
        payload=payload or {},
    )


# --------------------------------------------------------------------------- #
# Schema + lifecycle
# --------------------------------------------------------------------------- #


def test_init_creates_schema_and_sets_user_version(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    bus = SqliteEventBus(db)
    try:
        raw = sqlite3.connect(str(db))
        try:
            tables = {
                r[0]
                for r in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
            assert {"events", "sessions", "events_fts"} <= tables
            assert int(raw.execute("PRAGMA user_version;").fetchone()[0]) == 1
            mode = raw.execute("PRAGMA journal_mode;").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            raw.close()
    finally:
        bus.close()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    SqliteEventBus(db).close()
    # Second open must not error (CREATE IF NOT EXISTS + user_version == target).
    bus = SqliteEventBus(db)
    bus.close()


def test_init_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "deeper" / "events.db"
    assert not nested.parent.exists()
    bus = SqliteEventBus(nested)
    try:
        assert nested.parent.is_dir()
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# publish() — write-then-fanout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_persists_and_fans_out(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        seen: list[BehavioralEvent] = []

        async def handler(event: BehavioralEvent) -> None:
            seen.append(event)

        bus.subscribe(accept_all, handler)
        ev = _ev(payload={"content": "hi"})
        await bus.publish(ev)
        await bus.drain()

        # Subscriber saw it.
        assert len(seen) == 1
        assert seen[0].id == ev.id

        # And it was persisted.
        got = bus.query(session_id="s1")
        assert len(got) == 1
        assert got[0].id == ev.id
        assert got[0].payload == {"content": "hi"}
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_publish_writes_before_fanout(tmp_path: Path) -> None:
    """Subscribers must only see events that are already durable."""
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        seen_rows_at_handler_time: list[int] = []

        async def handler(event: BehavioralEvent) -> None:
            # At the moment this runs, the event should already be in the DB.
            rows = bus.query(session_id=event.session_id)
            seen_rows_at_handler_time.append(len(rows))

        bus.subscribe(accept_all, handler)
        await bus.publish(_ev(payload={}))
        await bus.drain()

        assert seen_rows_at_handler_time == [1]
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_publish_many_batch_inserts(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        events = [_ev(payload={"i": i}) for i in range(5)]
        await bus.publish_many(events)
        await bus.drain()

        got = bus.query(session_id="s1", limit=100)
        assert len(got) == 5
        assert [e.payload["i"] for e in got] == list(range(5))
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# query() filters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_query_filters_by_session(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        await bus.publish(_ev(session_id="A", payload={"x": 1}))
        await bus.publish(_ev(session_id="B", payload={"x": 2}))
        await bus.publish(_ev(session_id="A", payload={"x": 3}))

        a = bus.query(session_id="A")
        b = bus.query(session_id="B")
        assert [e.payload["x"] for e in a] == [1, 3]
        assert [e.payload["x"] for e in b] == [2]
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_query_filters_by_since_until(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        e1 = _ev(payload={"n": 1})
        await bus.publish(e1)
        await asyncio.sleep(0.01)
        e2 = _ev(payload={"n": 2})
        await bus.publish(e2)
        await asyncio.sleep(0.01)
        e3 = _ev(payload={"n": 3})
        await bus.publish(e3)

        mid = (e2.ts + e3.ts) / 2
        tail = bus.query(session_id="s1", since=mid)
        assert [e.id for e in tail] == [e3.id]

        head = bus.query(session_id="s1", until=mid)
        assert [e.id for e in head] == [e1.id, e2.id]
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_query_filters_by_types(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        await bus.publish(_ev(type=EventType.USER_MESSAGE))
        await bus.publish(_ev(type=EventType.LLM_RESPONSE))
        await bus.publish(_ev(type=EventType.TOOL_INVOCATION_FINISHED))

        got = bus.query(types=[EventType.LLM_RESPONSE, EventType.USER_MESSAGE])
        kinds = {e.type for e in got}
        assert kinds == {EventType.LLM_RESPONSE, EventType.USER_MESSAGE}
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_query_limit_and_offset(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        for i in range(5):
            await bus.publish(_ev(payload={"i": i}))

        first_two = bus.query(limit=2)
        next_two = bus.query(limit=2, offset=2)
        assert [e.payload["i"] for e in first_two] == [0, 1]
        assert [e.payload["i"] for e in next_two] == [2, 3]
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# FTS5 search
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_fts_matches_payload_keywords(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        await bus.publish(_ev(payload={"note": "memory pressure rising"}))
        await bus.publish(_ev(payload={"note": "disk is fine"}))
        await bus.publish(_ev(payload={"note": "memory reclaim scheduled"}))

        hits = bus.search("memory")
        notes = [e.payload["note"] for e in hits]
        assert len(hits) == 2
        assert all("memory" in n for n in notes)
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_search_scoped_by_session(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        await bus.publish(_ev(session_id="A", payload={"note": "memory leak"}))
        await bus.publish(_ev(session_id="B", payload={"note": "memory slow"}))

        only_a = bus.search("memory", session_id="A")
        assert len(only_a) == 1
        assert only_a[0].session_id == "A"
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# sessions aggregate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sessions_summary_tracks_counts(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        await bus.publish(_ev(session_id="A"))
        await bus.publish(_ev(session_id="A"))
        await bus.publish(_ev(session_id="B"))

        summaries = {s["session_id"]: s for s in bus.session_summaries()}
        assert summaries["A"]["event_count"] == 2
        assert summaries["B"]["event_count"] == 1
        assert summaries["A"]["last_ts"] >= summaries["A"]["started_ts"]
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_events_survive_reopen(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    bus = SqliteEventBus(db)
    try:
        ev = _ev(payload={"survive": True})
        await bus.publish(ev)
        await bus.drain()
    finally:
        bus.close()

    bus2 = SqliteEventBus(db)
    try:
        got = bus2.query(session_id="s1")
        assert len(got) == 1
        assert got[0].id == ev.id
        assert got[0].payload == {"survive": True}
    finally:
        bus2.close()


# --------------------------------------------------------------------------- #
# Concurrent publishes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_publish_serialized_no_loss(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        events = [_ev(payload={"i": i}) for i in range(20)]
        await asyncio.gather(*(bus.publish(e) for e in events))
        await bus.drain()

        got = bus.query(session_id="s1", limit=100)
        assert len(got) == 20
        assert {e.payload["i"] for e in got} == set(range(20))
    finally:
        bus.close()


# --------------------------------------------------------------------------- #
# Scale / performance (Epic #13 exit criterion)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fts5_search_stays_fast_at_representative_scale(
    tmp_path: Path,
) -> None:
    """Epic #13 exit criterion: FTS5 keyword search must return in
    <100ms on a representative workload.

    500 events approximates a busy 24h session. We assert a 500ms
    ceiling (5x the target) to absorb CI noise while still catching
    order-of-magnitude regressions — a linear scan or a missing FTS5
    index would blow past this easily.
    """
    import time

    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        # Mix "memory" / "disk" / "network" keywords so the FTS5 match
        # set is a realistic subset, not the whole table.
        N = 500
        keywords = ["memory pressure", "disk saturation", "network latency"]
        events = [
            _ev(payload={"note": f"{keywords[i % 3]} sample {i}"})
            for i in range(N)
        ]
        await asyncio.gather(*(bus.publish(e) for e in events))
        await bus.drain()

        t0 = time.perf_counter()
        hits = bus.search("memory", limit=1000)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Roughly a third of N should match.
        assert len(hits) > N // 4, (
            f"expected ~{N // 3} hits, got {len(hits)}"
        )
        assert elapsed_ms < 500, (
            f"FTS5 search took {elapsed_ms:.1f}ms "
            f"(exit criterion is <100ms, this guard is 5x headroom)"
        )
    finally:
        bus.close()
