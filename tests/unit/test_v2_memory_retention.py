"""Epic #5 — memory retention + MEMORY_EVICTED event + periodic sweep.

Covers:
  * ``SqliteVecMemory.prune`` / ``.evict`` emit ``MEMORY_EVICTED`` when a
    bus is provided, with correct payload (layer / count / reason /
    bytes_removed).
  * ``build_memory_from_config`` constructs a memory store with the
    right db_path / ttl / pinned_tags; honors ``enabled: false`` to
    return None; rejects malformed sections.
  * ``parse_retention_config`` parses layer caps + interval + defaults.
  * ``MemorySweepTask.sweep_once`` runs prune + evict on every layer.
  * ``MemorySweepTask.start/stop`` lifecycle — no leaked tasks.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.daemon.factory import (
    ConfigError,
    build_memory_from_config,
)
from xmclaw.daemon.memory_sweep import (
    LayerRetention,
    MemorySweepTask,
    RetentionPolicy,
    parse_retention_config,
)
from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _mem(bus=None) -> SqliteVecMemory:
    """Fresh in-memory store with embedding_dim unset."""
    return SqliteVecMemory(":memory:", bus=bus)


async def _collect_events(bus: InProcessEventBus) -> list[BehavioralEvent]:
    collected: list[BehavioralEvent] = []

    async def handler(event: BehavioralEvent) -> None:
        collected.append(event)

    bus.subscribe(lambda e: e.type == EventType.MEMORY_EVICTED, handler)
    return collected


async def _put_many(mem: SqliteVecMemory, layer: str, count: int) -> None:
    for i in range(count):
        await mem.put(
            layer,
            MemoryItem(
                id=f"{layer}-i{i}",  # unique across layers — PK is just id
                layer=layer,  # type: ignore[arg-type]
                text=f"item-{i}-" + ("x" * 20),
                metadata={},
            ),
        )


# ──────────────────────────────────────────────────────────────────────
# MEMORY_EVICTED event emission
# ──────────────────────────────────────────────────────────────────────


def test_evict_cap_items_emits_event():
    async def go():
        bus = InProcessEventBus()
        events = await _collect_events(bus)
        mem = _mem(bus=bus)
        try:
            await _put_many(mem, "working", 5)
            removed = await mem.evict("working", max_items=2)
            await bus.drain()
        finally:
            mem.close()
        assert removed == 3
        assert len(events) == 1
        ev = events[0]
        assert ev.type == EventType.MEMORY_EVICTED
        assert ev.session_id == "_system"
        assert ev.agent_id == "daemon"
        assert ev.payload["layer"] == "working"
        assert ev.payload["count"] == 3
        assert ev.payload["reason"] == "cap_items"
        # bytes_removed is only set when max_bytes was specified
        assert "bytes_removed" not in ev.payload

    asyncio.run(go())


def test_evict_cap_bytes_emits_event_with_bytes_removed():
    async def go():
        bus = InProcessEventBus()
        events = await _collect_events(bus)
        mem = _mem(bus=bus)
        try:
            await _put_many(mem, "short", 5)
            removed = await mem.evict("short", max_bytes=50)
            await bus.drain()
        finally:
            mem.close()
        assert removed >= 1
        assert len(events) == 1
        assert events[0].payload["reason"] == "cap_bytes"
        assert events[0].payload["bytes_removed"] > 0

    asyncio.run(go())


def test_prune_by_age_emits_event():
    async def go():
        import time

        bus = InProcessEventBus()
        events = await _collect_events(bus)
        # Force short TTL of 0 so the next put is already old enough.
        mem = SqliteVecMemory(":memory:", ttl={"short": 0.0}, bus=bus)
        try:
            await _put_many(mem, "short", 3)
            # Tiny sleep so ts < now() - ttl succeeds on fast CI.
            time.sleep(0.01)
            removed = await mem.prune("short")
            await bus.drain()
        finally:
            mem.close()
        assert removed == 3
        assert len(events) == 1
        assert events[0].payload["reason"] == "age"

    asyncio.run(go())


def test_evict_zero_items_emits_nothing():
    async def go():
        bus = InProcessEventBus()
        events = await _collect_events(bus)
        mem = _mem(bus=bus)
        try:
            removed = await mem.evict("long", max_items=10)
            await bus.drain()
        finally:
            mem.close()
        assert removed == 0
        assert events == []

    asyncio.run(go())


def test_bus_subscriber_exception_does_not_break_eviction():
    async def go():
        bus = InProcessEventBus()

        async def bad_handler(_ev: BehavioralEvent) -> None:
            raise RuntimeError("downstream is on fire")

        bus.subscribe(lambda e: e.type == EventType.MEMORY_EVICTED, bad_handler)
        mem = _mem(bus=bus)
        try:
            await _put_many(mem, "working", 3)
            removed = await mem.evict("working", max_items=1)
            await bus.drain()
        finally:
            mem.close()
        # Eviction itself must succeed even if the subscriber blew up.
        assert removed == 2

    asyncio.run(go())


# ──────────────────────────────────────────────────────────────────────
# build_memory_from_config
# ──────────────────────────────────────────────────────────────────────


def test_build_memory_from_config_default():
    bus = InProcessEventBus()
    cfg: dict = {}
    mem = build_memory_from_config(cfg, bus=bus)
    assert mem is not None
    # Default path is ~/.xmclaw/v2/memory.db — just assert it's set and
    # the bus wired through.
    assert mem.db_path != ""
    assert mem._bus is bus
    mem.close()


def test_build_memory_from_config_enabled_false():
    mem = build_memory_from_config({"memory": {"enabled": False}})
    assert mem is None


def test_build_memory_from_config_custom_db_path(tmp_path):
    db = tmp_path / "custom.db"
    mem = build_memory_from_config({"memory": {"db_path": str(db)}})
    assert mem is not None
    assert str(db) in mem.db_path
    mem.close()


def test_build_memory_from_config_ttl_and_pinned():
    mem = build_memory_from_config({
        "memory": {
            "db_path": ":memory:",
            "ttl": {"short": 60.0, "working": None},
            "pinned_tags": ["identity", "user-profile"],
        }
    })
    assert mem is not None
    assert mem._ttl["short"] == 60.0
    assert mem._ttl["working"] is None
    assert "identity" in mem._pinned_tags
    mem.close()


def test_build_memory_from_config_rejects_bad_section():
    with pytest.raises(ConfigError):
        build_memory_from_config({"memory": "not a dict"})


def test_build_memory_from_config_rejects_bad_pinned_tags():
    with pytest.raises(ConfigError):
        build_memory_from_config({
            "memory": {
                "db_path": ":memory:",
                "pinned_tags": [1, 2, 3],
            }
        })


def test_build_memory_from_config_rejects_bad_embedding_dim():
    with pytest.raises(ConfigError):
        build_memory_from_config({
            "memory": {"db_path": ":memory:", "embedding_dim": "wide"},
        })


# ──────────────────────────────────────────────────────────────────────
# parse_retention_config
# ──────────────────────────────────────────────────────────────────────


def test_parse_retention_missing_returns_default():
    p = parse_retention_config(None)
    assert p.sweep_interval_s == 3600.0
    assert p.prune_by_ttl is True
    assert p.for_layer("short").max_items is None


def test_parse_retention_per_layer_caps():
    p = parse_retention_config({
        "max_items": {"short": 500, "working": 5000, "long": None},
        "max_bytes": {"short": 10_000, "working": None, "long": None},
        "sweep_interval_s": 120,
    })
    assert p.for_layer("short") == LayerRetention(
        max_items=500, max_bytes=10_000,
    )
    assert p.for_layer("working") == LayerRetention(max_items=5000)
    assert p.for_layer("long") == LayerRetention()
    assert p.sweep_interval_s == 120.0


def test_parse_retention_bad_values_fall_back_to_none():
    p = parse_retention_config({
        "max_items": {"short": 0, "working": -5, "long": "lots"},
        "sweep_interval_s": "soon",
    })
    assert p.for_layer("short").max_items is None
    assert p.for_layer("working").max_items is None
    assert p.for_layer("long").max_items is None
    assert p.sweep_interval_s == 3600.0  # fell back to default


def test_any_cap_set_prune_by_ttl_only():
    p = RetentionPolicy(prune_by_ttl=True)
    assert p.any_cap_set() is True


def test_any_cap_set_fully_disabled():
    p = RetentionPolicy(prune_by_ttl=False)
    assert p.any_cap_set() is False


# ──────────────────────────────────────────────────────────────────────
# MemorySweepTask.sweep_once — exercises prune + evict across layers
# ──────────────────────────────────────────────────────────────────────


def test_sweep_once_runs_all_layers():
    async def go():
        bus = InProcessEventBus()
        events = await _collect_events(bus)
        # ttl=0 so prune evicts everything older than now.
        mem = SqliteVecMemory(
            ":memory:", ttl={"short": 0.0, "working": 0.0, "long": 0.0},
            bus=bus,
        )
        try:
            await _put_many(mem, "short", 3)
            await _put_many(mem, "working", 4)
            # small sleep so ts < now - ttl
            await asyncio.sleep(0.01)
            policy = RetentionPolicy(prune_by_ttl=True)
            task = MemorySweepTask(mem, policy)
            removed = await task.sweep_once()
            await bus.drain()
        finally:
            mem.close()
        assert removed["short"] == 3
        assert removed["working"] == 4
        assert removed["long"] == 0
        # two layers had actual evictions, emit one event each.
        layers = {ev.payload["layer"] for ev in events}
        assert layers == {"short", "working"}

    asyncio.run(go())


def test_sweep_once_isolates_layer_failures():
    async def go():
        class _FlakyMemory:
            def __init__(self):
                self.calls: list[str] = []

            async def prune(self, layer):
                self.calls.append(f"prune:{layer}")
                if layer == "working":
                    raise RuntimeError("working is cursed")
                return 1

            async def evict(self, layer, *, max_items=None, max_bytes=None):
                return 0

        mem = _FlakyMemory()
        task = MemorySweepTask(mem, RetentionPolicy(prune_by_ttl=True))
        removed = await task.sweep_once()
        assert removed == {"short": 1, "working": 0, "long": 1}
        # All three layers were attempted despite the working crash.
        assert mem.calls == ["prune:short", "prune:working", "prune:long"]

    asyncio.run(go())


# ──────────────────────────────────────────────────────────────────────
# MemorySweepTask.start/stop lifecycle
# ──────────────────────────────────────────────────────────────────────


def test_start_noop_when_no_caps():
    async def go():
        task = MemorySweepTask(
            memory=object(),
            policy=RetentionPolicy(prune_by_ttl=False),
        )
        await task.start()
        assert task._task is None
        await task.stop()  # must be safe to call anyway

    asyncio.run(go())


def test_start_stop_roundtrip():
    async def go():
        sweeps = 0

        class _NoopMem:
            async def prune(self, layer):
                nonlocal sweeps
                sweeps += 1
                return 0

            async def evict(self, layer, *, max_items=None, max_bytes=None):
                return 0

        # Interval so short that one sweep fires before we stop.
        policy = RetentionPolicy(
            sweep_interval_s=0.05, prune_by_ttl=True,
        )
        task = MemorySweepTask(_NoopMem(), policy)
        await task.start()
        await asyncio.sleep(0.12)
        await task.stop()
        assert sweeps >= 1
        # After stop, restart must create a fresh task.
        await task.start()
        assert task._task is not None
        await task.stop()

    asyncio.run(go())
