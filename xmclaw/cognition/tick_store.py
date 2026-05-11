"""TickStore — Phase D persistence for CognitiveDaemon tick summaries.

Mirrors :class:`xmclaw.cognition.self_experiment.ExperimentStore` shape:
SQLite-backed, JSON-blob schema, async-to-sync bridge.  Keeps the
last N ticks so the /daemon/history endpoint can surface trends
without polling the event bus.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def _default_db_path() -> Path:
    from xmclaw.utils.paths import default_ticks_db_path

    return default_ticks_db_path()


class TickStore:
    """SQLite-backed ring buffer of tick summaries.

    Schema (intentionally minimal — tick shape evolves):
      * ``tick_summaries(tick PRIMARY KEY, payload TEXT, ts REAL)``
        payload is a JSON blob of the tick summary dict.
      * ``idx_ticks_ts`` — time-range queries for history endpoint.
    """

    def __init__(self, db_path: Any | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tick_summaries (
                    tick INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticks_ts "
                "ON tick_summaries(ts)",
            )
            conn.commit()

    async def save(self, summary: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_sync, summary)

    def _save_sync(self, summary: dict[str, Any]) -> None:
        tick = int(summary["tick"])
        ts = float(summary.get("timestamp", time.time()))
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tick_summaries(tick, payload, ts) "
                "VALUES(?, ?, ?)",
                (tick, json.dumps(summary), ts),
            )
            conn.commit()

    async def list_ticks(
        self,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, since, until, limit)

    def _list_sync(
        self,
        since: float | None,
        until: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT payload FROM tick_summaries WHERE 1=1"
            params: list[Any] = []
            if since is not None:
                query += " AND ts >= ?"
                params.append(float(since))
            if until is not None:
                query += " AND ts <= ?"
                params.append(float(until))
            query += " ORDER BY tick DESC LIMIT ?"
            params.append(max(1, int(limit)))
            rows = conn.execute(query, params).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    async def get_tick(self, tick: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, tick)

    def _get_sync(self, tick: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM tick_summaries WHERE tick = ?",
                (int(tick),),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])
