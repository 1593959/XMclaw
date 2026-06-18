"""Tests for SqliteEventBus write-behind batching (B-xxx).

Covers:
1. High-frequency events (LLM_CHUNK) are collected, not written immediately.
2. After 100ms they are flushed via executemany().
3. Low-frequency events (USER_MESSAGE) are still written immediately.
4. Subscribers still receive events individually (no batching on the read side).
5. Mixed event types batch independently per type.
6. Performance: 100 LLM_CHUNK events result in 1 executemany call instead of 100 execute calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import BehavioralEvent, EventType, SqliteEventBus, make_event
from xmclaw.core.bus.memory import accept_all


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
# Helpers
# --------------------------------------------------------------------------- #

class _CountingConn:
    """Lightweight wrapper that records execute / executemany calls."""

    def __init__(self, real_conn: Any) -> None:
        self._real = real_conn
        self.execute_count = 0
        self.executemany_count = 0
        self.rows = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.execute_count += 1
        return self._real.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self.executemany_count += 1
        if len(args) > 1:
            self.rows += len(args[1])
        return self._real.executemany(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# --------------------------------------------------------------------------- #
# Batching behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_high_frequency_events_are_batched_not_written_immediately(
    tmp_path: Path,
) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        counter = _CountingConn(bus._conn)
        bus._conn = counter

        ev = _ev(type=EventType.LLM_CHUNK, payload={"chunk": "hello"})
        await bus.publish(ev)
        # Immediately after publish, no DB write should have occurred
        assert counter.execute_count == 0
        assert counter.executemany_count == 0

        # Wait for the flush interval to elapse
        await asyncio.sleep(0.15)
        assert counter.executemany_count == 1
        assert counter.rows == 1
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_low_frequency_events_are_written_immediately(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        counter = _CountingConn(bus._conn)
        bus._conn = counter

        ev = _ev(type=EventType.USER_MESSAGE, payload={"text": "hi"})
        await bus.publish(ev)
        assert counter.execute_count == 1
        assert counter.executemany_count == 0
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_subscribers_receive_events_individually(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        seen: list[BehavioralEvent] = []

        async def handler(event: BehavioralEvent) -> None:
            seen.append(event)

        bus.subscribe(accept_all, handler)

        events = [_ev(type=EventType.LLM_CHUNK, payload={"i": i}) for i in range(5)]
        for ev in events:
            await bus.publish(ev)

        await asyncio.sleep(0.15)
        await bus.drain()

        assert len(seen) == 5
        assert [e.payload["i"] for e in seen] == list(range(5))
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_mixed_event_types_batch_independently(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        counter = _CountingConn(bus._conn)
        bus._conn = counter

        # Publish interleaved high-frequency and low-frequency events
        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"t": "chunk1"}))
        await bus.publish(_ev(type=EventType.USER_MESSAGE, payload={"t": "msg1"}))
        await bus.publish(
            _ev(type=EventType.LLM_THINKING_CHUNK, payload={"t": "think1"})
        )
        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"t": "chunk2"}))

        # Wait for flush
        await asyncio.sleep(0.15)
        # one batch for LLM_CHUNK, one for LLM_THINKING_CHUNK
        assert counter.executemany_count == 2
        assert counter.rows == 3  # 2 chunks + 1 thinking
        # USER_MESSAGE immediate (1) + BEGIN/COMMIT per batch (2 batches × 2)
        assert counter.execute_count == 5
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_performance_100_llm_chunks_reduce_to_one_executemany(
    tmp_path: Path,
) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        counter = _CountingConn(bus._conn)
        bus._conn = counter

        events = [
            _ev(type=EventType.LLM_CHUNK, payload={"i": i}) for i in range(100)
        ]
        for ev in events:
            await bus.publish(ev)

        await asyncio.sleep(0.15)
        assert counter.executemany_count == 1
        assert counter.rows == 100
        # Only BEGIN + COMMIT from the single batch transaction
        assert counter.execute_count == 2
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_flush_happens_within_100ms_window(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        counter = _CountingConn(bus._conn)
        bus._conn = counter

        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"a": 1}))
        await asyncio.sleep(0.05)
        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"a": 2}))
        await asyncio.sleep(0.05)
        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"a": 3}))
        await asyncio.sleep(0.05)
        await bus.publish(_ev(type=EventType.LLM_CHUNK, payload={"a": 4}))

        # After ~200ms, all events should have been flushed in two batches
        # (first batch at ~100ms, second at ~200ms)
        await asyncio.sleep(0.15)
        assert counter.executemany_count >= 1
        assert counter.rows == 4
    finally:
        bus.close()


@pytest.mark.asyncio
async def test_high_frequency_events_are_actually_persisted(tmp_path: Path) -> None:
    bus = SqliteEventBus(tmp_path / "events.db")
    try:
        events = [
            _ev(type=EventType.LLM_CHUNK, payload={"i": i}) for i in range(5)
        ]
        for ev in events:
            await bus.publish(ev)

        await asyncio.sleep(0.15)
        await bus.drain()

        got = bus.query(session_id="s1", types=[EventType.LLM_CHUNK])
        assert len(got) == 5
        assert [e.payload["i"] for e in got] == list(range(5))
    finally:
        bus.close()
