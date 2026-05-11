"""Chaos tests for PerceptionBus saturation.

Verify that the CognitiveDaemon's perception pipeline survives
bursts of mixed percept types without dropping events or deadlocking.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from xmclaw.cognition.perception_bus import Percept, PerceptionBus


# ── saturation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burst_of_mixed_percepts() -> None:
    """Fire 500 mixed percepts rapidly — the bus must queue and deliver
    them without loss (within its configured capacity)."""
    b = PerceptionBus()
    received: list[str] = []

    async def _sink(percept: Percept) -> None:
        received.append(percept.kind)
        await asyncio.sleep(0)  # yield to stress the scheduler

    b.subscribe(_sink)

    sources = ["ws", "file", "time", "process", "internal"]
    for i in range(500):
        await b.push(Percept(
            id=f"p-{i}",
            source=sources[i % 5],  # type: ignore[arg-type]
            kind=f"kind-{i % 5}",
            timestamp=time.time(),
            payload={"seq": i},
            suggested_salience=0.5,
        ))

    # Give subscribers time to finish.
    await asyncio.sleep(0.1)

    # All 500 must have been delivered.
    assert len(received) == 500


@pytest.mark.asyncio
async def test_broken_sink_does_not_kill_bus() -> None:
    """A subscriber that raises must not prevent other subscribers
    from receiving the percept, nor break subsequent pushes."""
    b = PerceptionBus()
    good_received: list[str] = []

    async def _bad_sink(_percept: Percept) -> None:
        raise RuntimeError("intentional chaos")

    async def _good_sink(p: Percept) -> None:
        good_received.append(p.kind)

    b.subscribe(_bad_sink)
    b.subscribe(_good_sink)

    await b.push(Percept(
        id="p1", source="ws", kind="msg",  # type: ignore[arg-type]
        timestamp=time.time(), payload={},
    ))
    await asyncio.sleep(0.05)

    assert len(good_received) == 1

    # Subsequent pushes must still work.
    await b.push(Percept(
        id="p2", source="ws", kind="msg2",  # type: ignore[arg-type]
        timestamp=time.time(), payload={},
    ))
    await asyncio.sleep(0.05)
    assert len(good_received) == 2


@pytest.mark.asyncio
async def test_multiple_sinks_all_receive() -> None:
    """Every registered sink must see every percept."""
    b = PerceptionBus()
    sink_a: list[str] = []
    sink_b: list[str] = []

    async def _sink_a(p: Percept) -> None:
        sink_a.append(p.kind)

    async def _sink_b(p: Percept) -> None:
        sink_b.append(p.kind)

    b.subscribe(_sink_a)
    b.subscribe(_sink_b)

    for i in range(50):
        await b.push(Percept(
            id=f"p-{i}", source="ws", kind="msg",  # type: ignore[arg-type]
            timestamp=time.time(), payload={"i": i},
        ))

    await asyncio.sleep(0.1)

    assert len(sink_a) == 50
    assert len(sink_b) == 50


@pytest.mark.asyncio
async def test_concurrent_producers() -> None:
    """10 producers pushing simultaneously — no corruption or loss."""
    b = PerceptionBus()
    received: list[str] = []

    async def _sink(p: Percept) -> None:
        received.append(p.kind)

    b.subscribe(_sink)

    async def _producer(tag: str) -> None:
        for i in range(50):
            await b.push(Percept(
                id=f"{tag}-{i}",
                source="ws",  # type: ignore[arg-type]
                kind=tag,
                timestamp=time.time(),
                payload={"tag": tag, "i": i},
            ))

    await asyncio.gather(*[_producer(f"p{i}") for i in range(10)])
    await asyncio.sleep(0.1)

    assert len(received) == 500
