"""UndoCabinet — automatic reverse-op recorder for destructive tool calls.

Sprint 0 Track B of the Jarvis roadmap. Trust infrastructure: every
destructive action the agent takes is recorded with a reverse_op so
the user can undo within a configurable window.

Scope (Phase 1)
===============

* ``file_write`` / ``apply_patch`` — backup the pre-existing content
  (or note "didn't exist"). Reverse = restore from backup, or delete
  the newly-created file.
* ``file_delete`` — backup the deleted file's bytes. Reverse = write
  them back to the original path.
* ``bash`` — currently NOT covered. Shell commands are too diverse
  for automatic reversal. A future phase records the command +
  best-effort warning so the user knows what happened.

Storage
=======

* Per-daemon SQLite at ``~/.xmclaw/v2/undo/actions.sqlite``.
* Backups at ``~/.xmclaw/v2/undo/backups/<action_id>.bin`` — raw
  bytes, empty file when the source didn't pre-exist.
* Schema migration is forward-only; new columns get nullable defaults.

Lifecycle
=========

* Each action expires after ``UNDO_WINDOW_S`` (default 30 minutes).
  Expired backups get garbage-collected on each ``record_*`` or
  ``undo_recent`` call — no separate worker needed.
* Undo can be applied within the window via ``undo_recent`` tool.
* After undo, the action is marked ``status="undone"`` and stays in
  the log for audit purposes (but backup file is freed).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Default undo window: 30 minutes. Long enough for a user to realize
# the agent did something wrong; short enough to bound disk usage.
UNDO_WINDOW_S = 30 * 60


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS actions (
    id            TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    action        TEXT NOT NULL,
    path          TEXT,
    backup_path   TEXT,
    args_json     TEXT,
    pre_existed   INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    session_id    TEXT,
    agent_id      TEXT
);
CREATE INDEX IF NOT EXISTS actions_ts_idx ON actions (ts);
CREATE INDEX IF NOT EXISTS actions_status_idx ON actions (status);
"""


@dataclass(frozen=True, slots=True)
class UndoRecord:
    """One destructive-action entry in the cabinet."""

    id: str
    ts: float
    action: str       # "file_write" | "file_delete" | "apply_patch"
    path: str         # absolute path the action touched
    backup_path: str | None
    args_json: str
    pre_existed: bool
    status: str       # "active" | "undone" | "expired"
    session_id: str | None = None
    agent_id: str | None = None


class UndoCabinet:
    """Records reverse-ops for destructive file-system mutations.

    Designed as a separate object you instantiate once per daemon (the
    factory does this) and pass to the FS toolset. Stateless except
    for the SQLite + backups dir; safe to share across coroutines.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        window_s: float = UNDO_WINDOW_S,
    ) -> None:
        if root is None:
            from xmclaw.utils.paths import data_dir
            root = data_dir() / "v2" / "undo"
        self._root = Path(root)
        self._backups_dir = self._root / "backups"
        self._db_path = self._root / "actions.sqlite"
        self._window_s = float(window_s)
        self._root.mkdir(parents=True, exist_ok=True)
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_V1)

    # ── Internals ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _gc(self) -> int:
        """Expire actions older than the undo window. Drops backup
        files. Returns count of expired rows."""
        cutoff = time.time() - self._window_s
        count = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, backup_path FROM actions "
                "WHERE status='active' AND ts < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                bp = row["backup_path"]
                if bp:
                    try:
                        Path(bp).unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                conn.execute(
                    "UPDATE actions SET status='expired', backup_path=NULL "
                    "WHERE id=?",
                    (row["id"],),
                )
                count += 1
            conn.commit()
        return count

    # ── Recording ─────────────────────────────────────────────────

    def record_file_mutation(
        self,
        *,
        path: Path,
        action: str,
        args: dict[str, Any] | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Backup ``path`` BEFORE a destructive ``action`` is applied.

        Call this from inside the tool's invoke method just BEFORE the
        actual disk write / delete. Returns the action_id you can hand
        back to the user / surface in the tool result so they know
        "undo_recent on this id will reverse it".

        Handles the "path didn't exist" case (e.g. file_write creating
        a new file) — records ``pre_existed=False`` so undo deletes
        instead of restoring.
        """
        self._gc()
        action_id = uuid.uuid4().hex
        pre_existed = path.exists() and path.is_file()
        backup_path: Path | None = None
        if pre_existed:
            backup_path = self._backups_dir / f"{action_id}.bin"
            try:
                # Use raw bytes — works for text + binary alike.
                backup_path.write_bytes(path.read_bytes())
            except Exception:  # noqa: BLE001 — never block the agent's tool
                backup_path = None
                pre_existed = False  # treat as no backup, undo will delete
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO actions ("
                " id, ts, action, path, backup_path, args_json, "
                " pre_existed, status, session_id, agent_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    action_id,
                    time.time(),
                    action,
                    str(path),
                    str(backup_path) if backup_path else None,
                    json.dumps(args or {}, ensure_ascii=False),
                    1 if pre_existed else 0,
                    session_id,
                    agent_id,
                ),
            )
            conn.commit()
        return action_id

    # ── Inspection ────────────────────────────────────────────────

    def recent(
        self,
        *,
        within_s: float | None = None,
        status: str = "active",
    ) -> list[UndoRecord]:
        """List actions newer than ``within_s`` seconds with ``status``.
        Default within_s = the full undo window."""
        self._gc()
        if within_s is None:
            within_s = self._window_s
        cutoff = time.time() - max(0.0, float(within_s))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE status=? AND ts >= ? "
                "ORDER BY ts DESC",
                (status, cutoff),
            ).fetchall()
        out: list[UndoRecord] = []
        for r in rows:
            out.append(UndoRecord(
                id=r["id"],
                ts=float(r["ts"]),
                action=r["action"],
                path=r["path"] or "",
                backup_path=r["backup_path"],
                args_json=r["args_json"] or "{}",
                pre_existed=bool(r["pre_existed"]),
                status=r["status"],
                session_id=r["session_id"],
                agent_id=r["agent_id"],
            ))
        return out

    # ── Undo application ──────────────────────────────────────────

    def undo(self, action_id: str) -> dict[str, Any]:
        """Apply the reverse op for one action. Returns a structured
        report so the caller can show the user exactly what happened.

        Reverse-op rules:
          * pre_existed=True  → restore the file from backup
          * pre_existed=False → delete the file (it was created)

        Idempotent: undoing an already-undone or expired action is a
        no-op that returns ``{"applied": False, "reason": "..."}``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id=?", (action_id,),
            ).fetchone()
        if row is None:
            return {"applied": False, "reason": "action_id not found"}
        if row["status"] != "active":
            return {"applied": False, "reason": f"status={row['status']}"}

        path = Path(row["path"])
        pre_existed = bool(row["pre_existed"])
        backup_path = row["backup_path"]

        try:
            if pre_existed and backup_path and Path(backup_path).is_file():
                # Restore the file from backup.
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(backup_path).read_bytes())
                reverse_kind = "restored_from_backup"
            elif not pre_existed:
                # The action created the file — delete it.
                if path.exists():
                    path.unlink(missing_ok=True)
                reverse_kind = "deleted_created_file"
            else:
                return {
                    "applied": False,
                    "reason": "backup missing or unreadable",
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "applied": False,
                "reason": (
                    f"reverse op failed: {type(exc).__name__}: {exc}"
                ),
            }

        # Mark applied + drop backup to free disk.
        try:
            if backup_path:
                Path(backup_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET status='undone', backup_path=NULL "
                "WHERE id=?",
                (action_id,),
            )
            conn.commit()

        return {
            "applied": True,
            "action_id": action_id,
            "action": row["action"],
            "path": str(path),
            "reverse_kind": reverse_kind,
        }

    def undo_recent(
        self,
        *,
        within_s: float = 10.0,
        action_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Convenience: undo all active actions within the last
        ``within_s`` seconds, newest-first. ``action_filter`` limits to
        a single action name (e.g. ``"file_delete"``) — None means all.

        Returns a list of per-action ``undo()`` results.
        """
        actions = self.recent(within_s=within_s, status="active")
        if action_filter:
            actions = [a for a in actions if a.action == action_filter]
        results: list[dict[str, Any]] = []
        for a in actions:
            results.append(self.undo(a.id))
        return results


__all__ = ["UndoCabinet", "UndoRecord", "UNDO_WINDOW_S"]
