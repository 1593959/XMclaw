"""SQLite-backed conversation history store.

Survives daemon restarts so ``xmclaw chat --resume <id>`` can pick up
where a previous run left off. Sits alongside ``events.db`` (the
immutable audit log) — this DB holds *mutable* per-session state:

  - the running list of ``Message`` objects (system prompt excluded —
    that's regenerated each turn so prompt edits take effect immediately)
  - the message count + last-update timestamp for the resume picker

Schema:
  session_history(session_id PK, history_json, message_count, updated_at)

``Message`` and ``ToolCall`` are frozen dataclasses; we round-trip them
through a JSON shape that mirrors the dataclass fields.  Adding a field
to either dataclass without bumping ``schema_version`` here would
silently drop it on load — keep the two in sync.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_history (
    session_id     TEXT PRIMARY KEY,
    history_json   TEXT NOT NULL,
    message_count  INTEGER NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_history_updated
    ON session_history(updated_at DESC);
"""


def _toolcall_to_dict(tc: ToolCall) -> dict:
    return {
        "name": tc.name,
        "args": tc.args,
        "provenance": tc.provenance,
        "id": tc.id,
        "raw_snippet": tc.raw_snippet,
        "session_id": tc.session_id,
    }


def _toolcall_from_dict(d: dict) -> ToolCall:
    return ToolCall(
        name=d["name"],
        args=d.get("args", {}) or {},
        provenance=d.get("provenance", "synthetic"),
        id=d.get("id") or "",
        raw_snippet=d.get("raw_snippet"),
        session_id=d.get("session_id"),
    )


def _message_to_dict(m: Message) -> dict:
    return {
        "role": m.role,
        "content": m.content,
        "tool_calls": [_toolcall_to_dict(tc) for tc in m.tool_calls],
        "tool_call_id": m.tool_call_id,
    }


def _message_from_dict(d: dict) -> Message:
    return Message(
        role=d["role"],
        content=d.get("content", "") or "",
        tool_calls=tuple(_toolcall_from_dict(tc) for tc in d.get("tool_calls") or ()),
        tool_call_id=d.get("tool_call_id"),
    )


class SessionStore:
    """Read/write conversation history, persisted as JSON in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def save(self, session_id: str, messages: list[Message]) -> None:
        history = [m for m in messages if m.role != "system"]
        payload = json.dumps(
            [_message_to_dict(m) for m in history],
            ensure_ascii=False,
        )
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_history
                    (session_id, history_json, message_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    history_json = excluded.history_json,
                    message_count = excluded.message_count,
                    updated_at = excluded.updated_at
                """,
                (session_id, payload, len(history), now),
            )

    def load(self, session_id: str) -> list[Message] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT history_json FROM session_history WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return [_message_from_dict(d) for d in raw]

    def delete(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM session_history WHERE session_id = ?",
                (session_id,),
            )

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Return [{session_id, message_count, updated_at, preview}], newest first.

        The ``preview`` field is a short string derived from the first
        user message — drives the human-readable session title in the
        Web UI Sessions page (so users see "做一个音乐播放器" instead
        of an opaque ``chat-16fc5186``). Falls back to "" when history
        couldn't be parsed.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, message_count, updated_at, history_json
                FROM session_history
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        out: list[dict] = []
        for sid, count, updated, hjson in rows:
            preview = ""
            try:
                hist = json.loads(hjson) if hjson else []
                for entry in hist:
                    if (
                        isinstance(entry, dict)
                        and entry.get("role") == "user"
                    ):
                        body = entry.get("content") or ""
                        if isinstance(body, str):
                            cleaned = body.strip().split("\n", 1)[0]
                            preview = cleaned[:80]
                            break
            except (json.JSONDecodeError, TypeError):
                preview = ""
            out.append({
                "session_id": sid,
                "message_count": count,
                "updated_at": updated,
                "preview": preview,
            })
        return out
