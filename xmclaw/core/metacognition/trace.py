"""DecisionTrace — every meaningful agent decision becomes a row.

Stored in a dedicated SQLite table next to events.db (same WAL,
same backup story). Append-only by design — patterns emerge from
high-volume reads, not mutations.

Schema:
    decision_traces(
        id           TEXT PRIMARY KEY,
        ts           REAL,        -- unix timestamp
        session_id   TEXT,
        turn_id      TEXT,
        step         INTEGER,     -- step within the turn
        kind         TEXT,        -- "tool_choice" | "skill_choice" |
                                  -- "answer_style" | "memory_recall" |
                                  -- "decline" | "ask_clarification"
        chosen       TEXT,        -- what we did
        alternatives TEXT,        -- JSON list of options NOT chosen
        reason       TEXT,        -- short rationale (LLM or rule-based)
        outcome      TEXT,        -- "ok" | "error" | "user_pushed_back"
                                  -- | "unknown" (set later if known)
        outcome_note TEXT         -- free-form addendum
    )

The recorder is **best-effort**: any DB error is logged + swallowed.
A missing trace doesn't break the agent — metacognition just has
less to work with.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


DecisionKind = Literal[
    "tool_choice",
    "skill_choice",
    "answer_style",
    "memory_recall",
    "decline",
    "ask_clarification",
]

DecisionOutcome = Literal[
    "unknown",          # default — outcome not yet determined
    "ok",
    "error",
    "user_pushed_back",
]


@dataclass(slots=True)
class DecisionTrace:
    """One agent decision. ``id`` defaults to a fresh hex; callers
    that want to match later updates (set_outcome) save the id."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    session_id: str = ""
    turn_id: str = ""
    step: int = 0
    kind: DecisionKind = "tool_choice"
    chosen: str = ""
    alternatives: list[str] = field(default_factory=list)
    reason: str = ""
    outcome: DecisionOutcome = "unknown"
    outcome_note: str = ""


class DecisionTraceRecorder:
    """Single-instance per daemon. Owns the SQLite connection +
    schema. Reads + writes are sync (low volume; the agent isn't
    bottlenecked on this) but wrapped so async callers can ``await``
    via ``asyncio.to_thread`` if they prefer.

    Args:
        db_path: SQLite file. Defaults to events.db sibling
            ``decisions.db`` so journal back-pressure doesn't bleed
            in. Pass an explicit path for tests.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            # Patch A (2026-05-10): paths.default_decisions_db_path()
            # so XMC_DATA_DIR / XMC_V2_DECISIONS_DB_PATH overrides
            # reroute properly. Pre-fix this manually walked from
            # default_events_db_path's parent which only honored the
            # events-db env var (wrong knob).
            from xmclaw.utils.paths import default_decisions_db_path
            db_path = default_decisions_db_path()
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS decision_traces (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                step INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                chosen TEXT NOT NULL,
                alternatives TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT 'unknown',
                outcome_note TEXT NOT NULL DEFAULT ''
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS dt_ts ON decision_traces(ts DESC)",
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS dt_kind ON decision_traces(kind)",
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS dt_turn ON decision_traces(turn_id)",
        )
        self._conn.commit()

    # ── Writes ────────────────────────────────────────────────────

    def record(self, trace: DecisionTrace) -> str:
        """Insert one trace. Returns its id. Best-effort: errors
        are logged and the original id is still returned so
        ``set_outcome`` calls don't blow up downstream."""
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO decision_traces (
                    id, ts, session_id, turn_id, step, kind,
                    chosen, alternatives, reason, outcome, outcome_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.id, trace.ts, trace.session_id,
                    trace.turn_id, trace.step, trace.kind,
                    trace.chosen,
                    json.dumps(trace.alternatives, ensure_ascii=False),
                    trace.reason, trace.outcome, trace.outcome_note,
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision_trace.record_failed err=%s", exc)
        return trace.id

    def set_outcome(
        self,
        trace_id: str,
        outcome: DecisionOutcome,
        note: str = "",
    ) -> bool:
        """Backfill an outcome on an existing trace. Returns True
        when a row was updated. Used after a tool result or a user
        pushback signals whether the original choice held up."""
        try:
            cur = self._conn.execute(
                "UPDATE decision_traces "
                "SET outcome = ?, outcome_note = ? WHERE id = ?",
                (outcome, note, trace_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decision_trace.set_outcome_failed id=%s err=%s",
                trace_id, exc,
            )
            return False

    # ── Reads ─────────────────────────────────────────────────────

    def recent(
        self, *, limit: int = 200,
        kind: DecisionKind | None = None,
        since: float | None = None,
    ) -> list[DecisionTrace]:
        """Pull the most recent traces, newest first. Filterable by
        kind + since-timestamp."""
        where: list[str] = ["1=1"]
        params: list[Any] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if since is not None:
            where.append("ts >= ?")
            params.append(float(since))
        sql = (
            f"SELECT * FROM decision_traces WHERE {' AND '.join(where)} "
            "ORDER BY ts DESC LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, (*params, int(limit))).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision_trace.recent_failed err=%s", exc)
            return []
        return [self._row_to_trace(dict(r)) for r in rows]

    def count(self) -> int:
        """Total traces. Useful for "is the recorder receiving
        anything" sanity checks."""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM decision_traces",
            ).fetchone()
            return int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _row_to_trace(row: dict[str, Any]) -> DecisionTrace:
        try:
            alts = json.loads(row.get("alternatives", "[]"))
        except json.JSONDecodeError:
            alts = []
        return DecisionTrace(
            id=row["id"],
            ts=float(row["ts"]),
            session_id=row.get("session_id", "") or "",
            turn_id=row.get("turn_id", "") or "",
            step=int(row.get("step", 0) or 0),
            kind=row["kind"],
            chosen=row.get("chosen", "") or "",
            alternatives=alts if isinstance(alts, list) else [],
            reason=row.get("reason", "") or "",
            outcome=row.get("outcome", "unknown") or "unknown",
            outcome_note=row.get("outcome_note", "") or "",
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DecisionTrace",
    "DecisionTraceRecorder",
    "DecisionKind",
    "DecisionOutcome",
]
