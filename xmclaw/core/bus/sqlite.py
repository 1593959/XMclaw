"""Persistent SQLite-backed event log (Epic #13).

Durable wrapper around :class:`InProcessEventBus` that appends every event to
a SQLite file **before** fan-out. Subscribers only see persisted events, so a
crash mid-publish cannot silently lose a reflected tool call.

Design choices:

* **WAL + ``synchronous=NORMAL``** — reader/writer concurrency without the
  full fsync cost; good enough for a local runtime where occasional loss of
  the tail (last few ms) is acceptable on OS-level crash.
* **One connection per bus** + ``asyncio.Lock`` to serialize writes. SQLite's
  stdlib driver is thread-safe per-connection only when callers serialize
  access, which an asyncio-native runtime already does naturally.
* **FTS5 over payload** (external-content) — keyword search without
  duplicating JSON bodies. Triggers keep it in sync with ``events``.
* **Sessions table** is derived; it's maintained via the same insert trigger
  so the read-side doesn't need to re-aggregate on every query.

Schema version is tracked in ``PRAGMA user_version``; bumps live in
:data:`MIGRATIONS` below and are applied idempotently on open.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.bus.memory import InProcessEventBus
from xmclaw.utils.paths import default_events_db_path as _central_default_events_db_path


def default_events_db_path() -> Path:
    """Location of the default event-log database.

    Uses ``~/.xmclaw/v2/events.db``. Honors ``XMC_V2_EVENTS_DB_PATH`` for
    narrow overrides, and ``XMC_DATA_DIR`` for moving the whole workspace.
    Delegates to :func:`xmclaw.utils.paths.default_events_db_path` — the
    central single source of truth for runtime paths (§3.1).
    """
    return _central_default_events_db_path()

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = 1

# Each migration is an idempotent SQL script that brings the DB from
# ``index``  to ``index + 1``. ``MIGRATIONS[0]`` is the initial create.
MIGRATIONS: list[str] = [
    # v0 -> v1: initial schema
    """
    CREATE TABLE IF NOT EXISTS events (
        id              TEXT PRIMARY KEY,
        ts              REAL NOT NULL,
        session_id      TEXT NOT NULL,
        agent_id        TEXT NOT NULL,
        type            TEXT NOT NULL,
        payload         TEXT NOT NULL,
        correlation_id  TEXT,
        parent_id       TEXT,
        schema_version  INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts);
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
    CREATE INDEX IF NOT EXISTS idx_events_corr ON events(correlation_id);

    CREATE TABLE IF NOT EXISTS sessions (
        session_id   TEXT PRIMARY KEY,
        agent_id     TEXT NOT NULL,
        started_ts   REAL NOT NULL,
        last_ts      REAL NOT NULL,
        event_count  INTEGER NOT NULL DEFAULT 0
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
        payload,
        content='events',
        content_rowid='rowid'
    );

    -- Keep FTS and sessions in sync with events on insert.
    CREATE TRIGGER IF NOT EXISTS events_ai_fts AFTER INSERT ON events BEGIN
        INSERT INTO events_fts(rowid, payload) VALUES (new.rowid, new.payload);
    END;

    CREATE TRIGGER IF NOT EXISTS events_ai_session AFTER INSERT ON events BEGIN
        INSERT INTO sessions(session_id, agent_id, started_ts, last_ts, event_count)
        VALUES (new.session_id, new.agent_id, new.ts, new.ts, 1)
        ON CONFLICT(session_id) DO UPDATE SET
            last_ts = new.ts,
            event_count = event_count + 1;
    END;
    """,
]


# --------------------------------------------------------------------------- #
# Row <-> event mapping
# --------------------------------------------------------------------------- #


def _event_to_row(event: BehavioralEvent) -> tuple[Any, ...]:
    return (
        event.id,
        event.ts,
        event.session_id,
        event.agent_id,
        event.type.value if isinstance(event.type, EventType) else str(event.type),
        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
        event.correlation_id,
        event.parent_id,
        event.schema_version,
    )


def _row_to_event(row: sqlite3.Row | tuple[Any, ...]) -> BehavioralEvent:
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        keys = (
            "id", "ts", "session_id", "agent_id", "type",
            "payload", "correlation_id", "parent_id", "schema_version",
        )
        d = dict(zip(keys, row, strict=False))
    return BehavioralEvent(
        id=d["id"],
        ts=float(d["ts"]),
        session_id=d["session_id"],
        agent_id=d["agent_id"],
        type=EventType(d["type"]),
        payload=json.loads(d["payload"]) if d["payload"] else {},
        correlation_id=d["correlation_id"],
        parent_id=d["parent_id"],
        schema_version=int(d["schema_version"]),
    )


# --------------------------------------------------------------------------- #
# Bus
# --------------------------------------------------------------------------- #


_INSERT_SQL = (
    "INSERT INTO events "
    "(id, ts, session_id, agent_id, type, payload, correlation_id, parent_id, schema_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class SqliteEventBus(InProcessEventBus):
    """Durable event bus — appends every event to a SQLite WAL before fan-out.

    The connection is opened once in ``__init__`` and reused. Writes are
    serialized by an ``asyncio.Lock``; reads (``query`` / ``search``) hold a
    blocking lock briefly and can be called from any coroutine.
    """

    def __init__(self, db_path: Path | str) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage txns explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._write_lock = asyncio.Lock()
        self._read_lock = threading.Lock()
        self._configure_connection()
        self._run_migrations()

    # ---- lifecycle ------------------------------------------------------- #

    def _configure_connection(self) -> None:
        c = self._conn
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA foreign_keys=ON;")

    def _run_migrations(self) -> None:
        cur = self._conn.execute("PRAGMA user_version;")
        current = int(cur.fetchone()[0])
        for idx in range(current, SCHEMA_VERSION):
            # executescript() auto-commits any open tx, so we don't wrap it
            # in BEGIN/COMMIT (autocommit mode — isolation_level=None).
            self._conn.executescript(MIGRATIONS[idx])
            self._conn.execute(f"PRAGMA user_version = {idx + 1};")

    @contextmanager
    def _txn(self) -> Any:
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise
        else:
            self._conn.execute("COMMIT;")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ---- write path ------------------------------------------------------ #

    async def publish(self, event: BehavioralEvent) -> None:
        """Append then fan out. Append failures are not swallowed — the
        agent loop needs to know if the durable log breaks.
        """
        async with self._write_lock:
            # sqlite3 stdlib is sync; this is a single small insert so we
            # do it inline rather than bounce off an executor.
            self._conn.execute(_INSERT_SQL, _event_to_row(event))
        await super().publish(event)

    async def publish_many(self, events: Sequence[BehavioralEvent]) -> None:
        """Batch-append multiple events in a single transaction, then fan
        each out individually. Useful for replaying imported logs.
        """
        if not events:
            return
        rows = [_event_to_row(e) for e in events]
        async with self._write_lock:
            with self._txn():
                self._conn.executemany(_INSERT_SQL, rows)
        for e in events:
            await super().publish(e)

    # ---- read path ------------------------------------------------------- #

    def query(
        self,
        *,
        session_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        types: Sequence[EventType | str] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[BehavioralEvent]:
        """Return events matching the filter, ordered by ``ts ASC, rowid ASC``."""
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts < ?")
            params.append(float(until))
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(t.value if isinstance(t, EventType) else str(t) for t in types)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT id, ts, session_id, agent_id, type, payload, "
            "correlation_id, parent_id, schema_version "
            f"FROM events{where} ORDER BY ts ASC, rowid ASC LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        with self._read_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def search(
        self,
        q: str,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[BehavioralEvent]:
        """FTS5 keyword search over event payload."""
        sql = (
            "SELECT e.id, e.ts, e.session_id, e.agent_id, e.type, e.payload, "
            "e.correlation_id, e.parent_id, e.schema_version "
            "FROM events e JOIN events_fts f ON f.rowid = e.rowid "
            "WHERE events_fts MATCH ?"
        )
        params: list[Any] = [q]
        if session_id is not None:
            sql += " AND e.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY e.ts ASC, e.rowid ASC LIMIT ?"
        params.append(int(limit))
        with self._read_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def session_summaries(self) -> list[dict[str, Any]]:
        """Return per-session summary rows (agent, first/last ts, count)."""
        sql = (
            "SELECT session_id, agent_id, started_ts, last_ts, event_count "
            "FROM sessions ORDER BY last_ts DESC"
        )
        with self._read_lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]


# --------------------------------------------------------------------------- #
# Debugging helpers (kept lightweight so tests don't need extra imports)
# --------------------------------------------------------------------------- #


def event_as_jsonable(event: BehavioralEvent) -> dict[str, Any]:
    """Return a JSON-serialisable dict for an event (handy for API responses)."""
    d = asdict(event)
    d["type"] = event.type.value if isinstance(event.type, EventType) else str(event.type)
    return d
