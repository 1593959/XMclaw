"""IntentStore — SQLite persistence for learned user patterns.

Lightweight, no vector extension required. Patterns are small,
structured rows keyed by a hash of their antecedent sequence.

Connection model (B-xxx): each public method opens its own
sqlite3 connection and closes it on exit.  This matches
AutobiographicalMemory and avoids long-lived connections that
can leak across daemon shutdown or collide with WAL cleanup.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from xmclaw.cognition.intent_engine.models import UserPattern
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class IntentStore:
    """Persistent storage for :class:`UserPattern` records.

    Parameters
    ----------
    db_path : Path
        SQLite file location. Parent dirs are created if missing.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Per-operation connection that actually CLOSES on exit. The
        # previous ``return conn`` + ``with self._connect() as conn:`` only
        # used sqlite3's transaction context manager, which commits but
        # NEVER closes — so every call leaked a connection until GC. On
        # Windows that lingering handle blocked temp-dir cleanup
        # (``PermissionError: WinError 32`` in tests) and contradicted this
        # module's own docstring ("opens its own connection and closes it
        # on exit"). Commit on success / rollback on error, then close.
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                pattern_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                antecedent TEXT NOT NULL,   -- JSON list of event type strings
                predicted_intent TEXT NOT NULL,
                frequency INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0.0,
                last_seen REAL DEFAULT 0.0,
                context_buckets TEXT DEFAULT '{}'  -- JSON dict
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_intent
            ON patterns(predicted_intent)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_confidence
            ON patterns(confidence)
        """)
        # Feedback log — every time a proposal is shown and the user reacts.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT,
                proposal_ts REAL,
                reaction TEXT,  -- 'accepted' | 'ignored' | 'dismissed' | 'snoozed'
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.commit()

    # ── write ──

    def upsert_pattern(self, pattern: UserPattern) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO patterns (pattern_id, label, antecedent, predicted_intent,
                                      frequency, confidence, last_seen, context_buckets)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_id) DO UPDATE SET
                    frequency = excluded.frequency,
                    confidence = excluded.confidence,
                    last_seen = excluded.last_seen,
                    context_buckets = excluded.context_buckets
                """,
                (
                    pattern.pattern_id,
                    pattern.label,
                    json.dumps(pattern.antecedent, ensure_ascii=False),
                    pattern.predicted_intent,
                    pattern.frequency,
                    pattern.confidence,
                    pattern.last_seen,
                    json.dumps(pattern.context_buckets, ensure_ascii=False),
                ),
            )
            conn.commit()

    def record_feedback(
        self,
        pattern_id: str | None,
        reaction: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO feedback (pattern_id, proposal_ts, reaction, metadata) VALUES (?, ?, ?, ?)",
                (pattern_id, time.time(), reaction, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.commit()

    def bump_frequency(self, pattern_id: str) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE patterns SET frequency = frequency + 1, last_seen = ? WHERE pattern_id = ?",
                (time.time(), pattern_id),
            )
            conn.commit()

    def update_confidence(self, pattern_id: str, new_confidence: float) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE patterns SET confidence = ? WHERE pattern_id = ?",
                (max(0.0, min(1.0, new_confidence)), pattern_id),
            )
            conn.commit()

    # ── read ──

    def get_pattern(self, pattern_id: str) -> UserPattern | None:
        with self._connect() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM patterns WHERE pattern_id = ?", (pattern_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_pattern(row)

    def list_patterns(
        self,
        *,
        min_confidence: float = 0.0,
        intent_type: str | None = None,
        limit: int = 100,
    ) -> list[UserPattern]:
        with self._connect() as conn:
            cur = conn.cursor()
            sql = "SELECT * FROM patterns WHERE confidence >= ?"
            params: list[Any] = [min_confidence]
            if intent_type:
                sql += " AND predicted_intent = ?"
                params.append(intent_type)
            sql += " ORDER BY confidence DESC, frequency DESC LIMIT ?"
            params.append(limit)
            rows = cur.execute(sql, params).fetchall()
            return [self._row_to_pattern(r) for r in rows]

    def feedback_stats(self, pattern_id: str) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT reaction, COUNT(*) FROM feedback WHERE pattern_id = ? GROUP BY reaction",
                (pattern_id,),
            ).fetchall()
            return {r["reaction"]: r["COUNT(*)"] for r in rows}

    def close(self) -> None:
        """No-op — connections are already closed after each operation.

        Kept for API compatibility with lifespan shutdown sequences.
        """

    # ── helpers ──

    @staticmethod
    def _row_to_pattern(row: sqlite3.Row) -> UserPattern:
        return UserPattern(
            pattern_id=row["pattern_id"],
            label=row["label"],
            antecedent=json.loads(row["antecedent"]),
            predicted_intent=row["predicted_intent"],
            frequency=row["frequency"],
            confidence=row["confidence"],
            last_seen=row["last_seen"],
            context_buckets=json.loads(row["context_buckets"]),
        )
