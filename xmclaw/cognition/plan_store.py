"""Epic #26 Phase C (2026-05-19) — HTN plan persistence ledger.

Pre-Phase-C HTN plans lived entirely in memory. ``execute_plan``
took a Plan object, ran it through the dispatcher, returned a
PlanExecutionResult, and the plan VANISHED — no audit trail, no
"what was the agent doing 5 minutes ago?" view, no recovery from
daemon restart mid-plan.

This module adds an SQLite-backed ledger. ``PlanStore`` writes one
row per plan at start, updates ``status`` + ``step_count_completed``
as steps land, and stamps a terminal ``completed`` / ``failed``
/ ``budget_exceeded`` / ``orphaned_at_restart`` at the end. The
``ActionDispatcher`` consumes it in ``execute_plan``:

  1. ``store.start(plan)`` at entry (status="executing")
  2. ``store.update(plan_id, ...)`` after each step
  3. ``store.finalise(plan_id, status, ...)`` at exit / failure

On daemon boot, ``store.mark_orphaned()`` flips any
``status="executing"`` rows to ``"orphaned_at_restart"`` so the
UI shows them clearly + the cognitive_daemon can decide whether to
re-launch (Phase C+ work — for v1, orphaned = give up).

Schema (one table, intentionally flat):

    plan_history(
        plan_id      TEXT PRIMARY KEY,
        goal_id      TEXT,
        status       TEXT,    -- executing / completed / failed
                              --   / budget_exceeded / orphaned_at_restart
        started_at   REAL,
        finished_at  REAL,
        n_steps      INTEGER,
        n_completed  INTEGER,
        error        TEXT,    -- non-null when status != completed
        budget_usd   REAL,    -- None = uncapped
        spent_usd    REAL,    -- per-plan delta from cost_tracker
        confidence   REAL
    )
    CREATE INDEX idx_plan_status ON plan_history(status);
    CREATE INDEX idx_plan_started ON plan_history(started_at DESC);

No retention policy in v1 — the table grows with plan count
(typically <1MB even at 10K plans). Operators can wipe via
``DELETE FROM plan_history WHERE finished_at < ?`` if they care.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_history (
    plan_id      TEXT PRIMARY KEY,
    goal_id      TEXT,
    status       TEXT NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    n_steps      INTEGER NOT NULL DEFAULT 0,
    n_completed  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    budget_usd   REAL,
    spent_usd    REAL,
    confidence   REAL
);
CREATE INDEX IF NOT EXISTS idx_plan_status
    ON plan_history(status);
CREATE INDEX IF NOT EXISTS idx_plan_started
    ON plan_history(started_at DESC);
"""


# Valid status values — kept as a tuple constant so callers (and
# tests) can reference them without typos.
PLAN_STATUS_EXECUTING = "executing"
PLAN_STATUS_COMPLETED = "completed"
PLAN_STATUS_FAILED = "failed"
PLAN_STATUS_BUDGET_EXCEEDED = "budget_exceeded"
PLAN_STATUS_ORPHANED = "orphaned_at_restart"

_TERMINAL_STATUSES = frozenset({
    PLAN_STATUS_COMPLETED,
    PLAN_STATUS_FAILED,
    PLAN_STATUS_BUDGET_EXCEEDED,
    PLAN_STATUS_ORPHANED,
})


class PlanStore:
    """SQLite-backed ledger of HTN plan executions.

    Thread-safety: one connection per ``PlanStore`` instance,
    short-lived per-call cursors. Suitable for the daemon's
    single-writer model. Multi-process access would need a WAL
    flag + check_same_thread=False — not in scope for v1.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ── lifecycle methods ───────────────────────────────────────────

    def start(
        self,
        plan_id: str,
        *,
        goal_id: str = "",
        n_steps: int = 0,
        budget_usd: float | None = None,
        confidence: float | None = None,
    ) -> None:
        """Record plan start. Idempotent — re-calling with same
        plan_id is a no-op (the dispatcher may retry execute_plan
        on a restart-resume path)."""
        now = time.time()
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO plan_history (
                    plan_id, goal_id, status, started_at, n_steps,
                    n_completed, budget_usd, confidence
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    plan_id, goal_id, PLAN_STATUS_EXECUTING, now,
                    n_steps, budget_usd, confidence,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_store.start_failed plan_id=%s err=%s", plan_id, exc)

    def update_progress(
        self,
        plan_id: str,
        *,
        n_completed: int,
        spent_usd: float | None = None,
    ) -> None:
        """Update step progress + per-plan spend mid-execution. Called
        after each step lands so the UI can render live progress."""
        try:
            if spent_usd is not None:
                self._conn.execute(
                    """
                    UPDATE plan_history
                    SET n_completed = ?, spent_usd = ?
                    WHERE plan_id = ?
                    """,
                    (n_completed, float(spent_usd), plan_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE plan_history
                    SET n_completed = ?
                    WHERE plan_id = ?
                    """,
                    (n_completed, plan_id),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan_store.update_progress_failed plan_id=%s err=%s",
                plan_id, exc,
            )

    def finalise(
        self,
        plan_id: str,
        *,
        status: str,
        error: str | None = None,
        spent_usd: float | None = None,
        n_completed: int | None = None,
    ) -> None:
        """Stamp the terminal status + finished_at timestamp."""
        if status not in _TERMINAL_STATUSES:
            log.warning(
                "plan_store.finalise_invalid_status plan_id=%s status=%s",
                plan_id, status,
            )
            return
        now = time.time()
        try:
            sets = ["status = ?", "finished_at = ?", "error = ?"]
            params: list[Any] = [status, now, error]
            if spent_usd is not None:
                sets.append("spent_usd = ?")
                params.append(float(spent_usd))
            if n_completed is not None:
                sets.append("n_completed = ?")
                params.append(int(n_completed))
            params.append(plan_id)
            self._conn.execute(
                f"UPDATE plan_history SET {', '.join(sets)} "
                f"WHERE plan_id = ?",
                params,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan_store.finalise_failed plan_id=%s err=%s",
                plan_id, exc,
            )

    def mark_orphaned(self) -> int:
        """Boot-time sweep: any plan still in ``executing`` is now
        considered orphaned (the daemon was killed mid-plan). Flips
        them to ``orphaned_at_restart`` so the UI shows them clearly
        + the next start() call won't conflict on the primary key.

        Returns the number of rows flipped."""
        now = time.time()
        try:
            cur = self._conn.execute(
                """
                UPDATE plan_history
                SET status = ?, finished_at = ?, error = ?
                WHERE status = ?
                """,
                (
                    PLAN_STATUS_ORPHANED, now,
                    "daemon restarted while plan was in flight",
                    PLAN_STATUS_EXECUTING,
                ),
            )
            n = cur.rowcount
            if n > 0:
                log.info(
                    "plan_store.orphaned_at_restart count=%d", n,
                )
            return n
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_store.mark_orphaned_failed err=%s", exc)
            return 0

    # ── query methods ──────────────────────────────────────────────

    def list_recent(
        self, limit: int = 50, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the most recent plans, newest first.

        Each row is a flat dict suitable for JSON serialisation —
        consumed by ``GET /api/v2/cognition/plans`` and the
        ``Autonomous Tasks`` UI panel."""
        try:
            limit = max(1, min(int(limit), 500))
            if status is not None:
                rows = self._conn.execute(
                    """
                    SELECT plan_id, goal_id, status, started_at,
                           finished_at, n_steps, n_completed, error,
                           budget_usd, spent_usd, confidence
                    FROM plan_history
                    WHERE status = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT plan_id, goal_id, status, started_at,
                           finished_at, n_steps, n_completed, error,
                           budget_usd, spent_usd, confidence
                    FROM plan_history
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_store.list_recent_failed err=%s", exc)
            return []
        cols = [
            "plan_id", "goal_id", "status", "started_at", "finished_at",
            "n_steps", "n_completed", "error", "budget_usd",
            "spent_usd", "confidence",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def get(self, plan_id: str) -> dict[str, Any] | None:
        try:
            row = self._conn.execute(
                """
                SELECT plan_id, goal_id, status, started_at, finished_at,
                       n_steps, n_completed, error, budget_usd,
                       spent_usd, confidence
                FROM plan_history
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_store.get_failed plan_id=%s err=%s", plan_id, exc)
            return None
        if row is None:
            return None
        cols = [
            "plan_id", "goal_id", "status", "started_at", "finished_at",
            "n_steps", "n_completed", "error", "budget_usd",
            "spent_usd", "confidence",
        ]
        return dict(zip(cols, row))

    def counts_by_status(self) -> dict[str, int]:
        """Aggregate count per status for the UI summary header."""
        try:
            rows = self._conn.execute(
                """
                SELECT status, COUNT(*) FROM plan_history
                GROUP BY status
                """,
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_store.counts_by_status_failed err=%s", exc)
            return {}
        return dict(rows)


__all__ = [
    "PlanStore",
    "PLAN_STATUS_BUDGET_EXCEEDED",
    "PLAN_STATUS_COMPLETED",
    "PLAN_STATUS_EXECUTING",
    "PLAN_STATUS_FAILED",
    "PLAN_STATUS_ORPHANED",
]
