"""SuggestionInbox — pending proactive suggestions awaiting review.

R5 (2026-05-10): when ``AutonomyPolicy.evaluate`` returns
``surface`` or ``needs_confirmation``, the daemon parks the
proposal here. The UI exposes them as a "建议" tab; the operator
approves / rejects, and the daemon executes the approval (or just
discards on reject).

Stored in SQLite (sibling of ``decisions.db``) so suggestions
survive a daemon restart — operator might walk away mid-review.

Schema:
    suggestions(
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,         -- "metacognition" | "perception" | etc
        summary TEXT NOT NULL,
        payload TEXT NOT NULL,        -- JSON
        risk TEXT NOT NULL,           -- low/medium/high
        confidence REAL NOT NULL,
        verdict TEXT NOT NULL,        -- surface | needs_confirmation
        status TEXT NOT NULL DEFAULT 'pending',
                                      -- pending / approved / rejected /
                                      -- expired / applied
        decided_at REAL,
        decided_by TEXT,              -- "user" | "auto_expire"
        applied_at REAL,
        applied_outcome TEXT          -- short note when applied
    )

The inbox is dumb storage — the **execution** of an approved
suggestion is the daemon's responsibility (route into
EvolutionController / PersonaStore / TaskScheduler / whatever).
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


SuggestionStatus = Literal[
    "pending", "approved", "rejected", "expired", "applied",
]
SuggestionVerdict = Literal["surface", "needs_confirmation"]


@dataclass(slots=True)
class Suggestion:
    """One row in the inbox. See module docstring for field semantics."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    kind: str = ""
    source: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    risk: str = "high"
    confidence: float = 0.0
    verdict: SuggestionVerdict = "surface"
    status: SuggestionStatus = "pending"
    decided_at: float | None = None
    decided_by: str | None = None
    applied_at: float | None = None
    applied_outcome: str | None = None


class SuggestionInbox:
    """Append + decide + query queue of proactive suggestions.

    Args:
        db_path: SQLite file. Defaults to ``decisions.db`` sibling
            ``suggestions.db`` so journal back-pressure doesn't bleed.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            # Patch A (2026-05-10): paths.default_suggestions_db_path()
            # so XMC_DATA_DIR / XMC_V2_SUGGESTIONS_DB_PATH overrides
            # reroute properly.
            from xmclaw.utils.paths import default_suggestions_db_path
            db_path = default_suggestions_db_path()
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
            CREATE TABLE IF NOT EXISTS suggestions (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload TEXT NOT NULL,
                risk TEXT NOT NULL,
                confidence REAL NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decided_at REAL,
                decided_by TEXT,
                applied_at REAL,
                applied_outcome TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS sg_status "
            "ON suggestions(status, ts DESC)",
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS sg_kind "
            "ON suggestions(kind)",
        )
        self._conn.commit()

    # ── Writes ────────────────────────────────────────────────────

    def add(self, sg: Suggestion) -> str:
        """Insert. Best-effort; returns the id either way."""
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO suggestions (
                    id, ts, kind, source, summary, payload,
                    risk, confidence, verdict, status,
                    decided_at, decided_by, applied_at, applied_outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sg.id, sg.ts, sg.kind, sg.source, sg.summary,
                    json.dumps(sg.payload, ensure_ascii=False),
                    sg.risk, sg.confidence, sg.verdict, sg.status,
                    sg.decided_at, sg.decided_by,
                    sg.applied_at, sg.applied_outcome,
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("suggestion_inbox.add_failed err=%s", exc)
        return sg.id

    def decide(
        self, sg_id: str, *, status: SuggestionStatus,
        decided_by: str = "user",
    ) -> bool:
        """Update status from ``pending``. Returns True when one row
        was updated. Idempotent: re-deciding a non-pending row is a
        no-op (returns False).
        """
        if status not in ("approved", "rejected", "expired"):
            return False
        try:
            cur = self._conn.execute(
                "UPDATE suggestions "
                "SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, time.time(), decided_by, sg_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("suggestion_inbox.decide_failed err=%s", exc)
            return False

    def mark_applied(
        self, sg_id: str, *, outcome: str = "ok",
    ) -> bool:
        """Stamp an approved suggestion as applied."""
        try:
            cur = self._conn.execute(
                "UPDATE suggestions "
                "SET status = 'applied', applied_at = ?, "
                "applied_outcome = ? "
                "WHERE id = ? AND status = 'approved'",
                (time.time(), outcome, sg_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "suggestion_inbox.apply_failed err=%s", exc,
            )
            return False

    # ── Reads ─────────────────────────────────────────────────────

    def list_pending(self, limit: int = 50) -> list[Suggestion]:
        return self._query(
            "WHERE status = 'pending' ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        )

    def list_recent(
        self, *, limit: int = 50,
        status: SuggestionStatus | None = None,
    ) -> list[Suggestion]:
        if status is None:
            return self._query(
                "ORDER BY ts DESC LIMIT ?", (int(limit),),
            )
        return self._query(
            "WHERE status = ? ORDER BY ts DESC LIMIT ?",
            (status, int(limit)),
        )

    def get(self, sg_id: str) -> Suggestion | None:
        rows = self._query("WHERE id = ? LIMIT 1", (sg_id,))
        return rows[0] if rows else None

    def count_pending(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM suggestions "
                "WHERE status = 'pending'",
            ).fetchone()
            return int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def _query(self, where: str, params: tuple) -> list[Suggestion]:
        try:
            rows = self._conn.execute(
                f"SELECT * FROM suggestions {where}", params,
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("suggestion_inbox.query_failed err=%s", exc)
            return []
        return [self._row_to_suggestion(dict(r)) for r in rows]

    @staticmethod
    def _row_to_suggestion(row: dict[str, Any]) -> Suggestion:
        try:
            payload = json.loads(row.get("payload", "{}"))
        except Exception:  # noqa: BLE001
            payload = {}
        return Suggestion(
            id=row["id"],
            ts=float(row["ts"]),
            kind=row.get("kind", "") or "",
            source=row.get("source", "") or "",
            summary=row.get("summary", "") or "",
            payload=payload if isinstance(payload, dict) else {},
            risk=row.get("risk", "high") or "high",
            confidence=float(row.get("confidence", 0.0) or 0.0),
            verdict=row.get("verdict", "surface") or "surface",
            status=row.get("status", "pending") or "pending",
            decided_at=row.get("decided_at"),
            decided_by=row.get("decided_by"),
            applied_at=row.get("applied_at"),
            applied_outcome=row.get("applied_outcome"),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "Suggestion",
    "SuggestionInbox",
    "SuggestionStatus",
    "SuggestionVerdict",
]
