"""TickStore — Phase D persistence unit tests."""
from __future__ import annotations

from xmclaw.cognition.tick_store import TickStore


async def test_save_and_get_tick(tmp_path):
    store = TickStore(db_path=tmp_path / "ticks.db")
    summary = {"tick": 1, "n_percepts": 3, "timestamp": 1000.0}
    await store.save(summary)
    loaded = await store.get_tick(1)
    assert loaded == summary


async def test_get_missing_tick_returns_none(tmp_path):
    store = TickStore(db_path=tmp_path / "ticks.db")
    assert await store.get_tick(99) is None


async def test_list_ticks_desc_by_tick(tmp_path):
    store = TickStore(db_path=tmp_path / "ticks.db")
    for i in range(5):
        await store.save({"tick": i + 1, "ts": 1000.0 + i})
    rows = await store.list_ticks(limit=3)
    assert [r["tick"] for r in rows] == [5, 4, 3]


async def test_list_ticks_time_range(tmp_path):
    store = TickStore(db_path=tmp_path / "ticks.db")
    for i in range(5):
        await store.save({"tick": i + 1, "timestamp": 1000.0 + i * 10})
    rows = await store.list_ticks(since=1020.0, until=1030.0)
    assert len(rows) == 2
    assert all(1020.0 <= r["timestamp"] <= 1030.0 for r in rows)


async def test_overwrite_same_tick(tmp_path):
    store = TickStore(db_path=tmp_path / "ticks.db")
    await store.save({"tick": 1, "a": 1})
    await store.save({"tick": 1, "a": 2})
    loaded = await store.get_tick(1)
    assert loaded["a"] == 2
