"""Unified security audit log.

Consolidates prompt-injection detections, tool-guard decisions,
anti-requirement violations, and approval events into a single
SQLite-backed stream so security incidents can be traced across a
session instead of being scattered across the event bus, TSV files,
and in-memory structures.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from xmclaw.core.bus import BehavioralEvent, EventType


def default_security_audit_db_path() -> Path:
    from xmclaw.utils.paths import data_dir
    return data_dir() / "v2" / "security_audit.db"


class SecurityAuditor:
    """SQLite-backed security event sink.

    Thread-safe for reads; writes should be serialised through the
    same connection (this class is intended to be used from a single
    event-loop thread).  All ``record_*`` methods are sync so they
    can be called from async handlers without additional awaits.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or default_security_audit_db_path())
        # SQLite refuses to open a file whose parent doesn't exist
        # with ``OperationalError: unable to open database file``.
        # Make the parent eagerly so tests pointing XMC_DATA_DIR at a
        # fresh tmp_path don't trip — and so a fresh install with no
        # ~/.xmclaw/v2/ directory yet doesn't crash on first start.
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    # ── schema ──────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                session_id  TEXT,
                event_type  TEXT    NOT NULL,
                severity    TEXT,
                source      TEXT,
                tool_name   TEXT,
                details     TEXT,          -- JSON blob
                acted       INTEGER        -- 0/1 boolean
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sec_evt_session
            ON security_events(session_id, timestamp)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sec_evt_type
            ON security_events(event_type, timestamp)
            """
        )
        self._conn.commit()

    # ── public record API ───────────────────────────────────────────────

    def record(
        self,
        *,
        event_type: str,
        severity: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
        acted: bool = False,
    ) -> None:
        """Write a single security event.  Never raises — failures are
        logged and swallowed so that audit-log trouble never blocks
        the agent's main path."""
        try:
            self._conn.execute(
                """
                INSERT INTO security_events
                (timestamp, session_id, event_type, severity,
                 source, tool_name, details, acted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    session_id,
                    event_type,
                    severity,
                    source,
                    tool_name,
                    json.dumps(details) if details else None,
                    1 if acted else 0,
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "security_auditor.record_failed err=%s",
                exc,
            )

    # ── convenience wrappers ────────────────────────────────────────────

    def record_prompt_injection(
        self,
        *,
        session_id: str | None,
        source: str,
        policy: str,
        categories: list[str],
        acted: bool,
        scanned_length: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.record(
            event_type="prompt_injection",
            severity="high" if acted else "medium",
            source=source,
            session_id=session_id,
            details={
                "policy": policy,
                "categories": categories,
                "scanned_length": scanned_length,
                **(details or {}),
            },
            acted=acted,
        )

    def record_tool_guard(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        action: str,
        findings: list[dict[str, Any]],
    ) -> None:
        severity = "critical" if action == "deny" else (
            "high" if action == "approve" else "medium"
        )
        self.record(
            event_type="tool_guard",
            severity=severity,
            source="tool_guard_engine",
            session_id=session_id,
            tool_name=tool_name,
            details={
                "action": action,
                "findings_count": len(findings),
                "findings": findings,
            },
            acted=(action in ("deny", "approve")),
        )

    def record_anti_req(
        self,
        *,
        session_id: str | None,
        kind: str,
        message: str,
        tool_name: str | None = None,
        hop: int | None = None,
    ) -> None:
        severity = "high" if kind in ("stuck_loop", "prompt_injection_blocked") else "medium"
        self.record(
            event_type="anti_req_violation",
            severity=severity,
            source="agent_loop",
            session_id=session_id,
            tool_name=tool_name,
            details={
                "kind": kind,
                "message": message,
                "hop": hop,
            },
            acted=True,
        )

    def record_approval(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        request_id: str,
        status: str,
        findings_summary: str | None = None,
    ) -> None:
        self.record(
            event_type=f"approval_{status}",
            severity="medium",
            source="approval_service",
            session_id=session_id,
            tool_name=tool_name,
            details={
                "request_id": request_id,
                "findings_summary": findings_summary,
            },
            acted=True,
        )

    # ── bus subscription ────────────────────────────────────────────────

    def subscribe_to_bus(self, bus) -> Any:
        """Subscribe to security-relevant events on *bus*.

        Returns the subscription handle so the caller can unsubscribe
        later if needed.
        """
        from xmclaw.core.bus.memory import accept_all

        async def _handler(event: BehavioralEvent) -> None:
            et = event.type
            payload = event.payload or {}

            if et == EventType.PROMPT_INJECTION_DETECTED:
                self.record_prompt_injection(
                    session_id=event.session_id,
                    source=payload.get("source", "unknown"),
                    policy=payload.get("policy", "unknown"),
                    categories=list(payload.get("categories", [])),
                    acted=payload.get("acted", False),
                    scanned_length=payload.get("scanned_length", 0),
                    details={k: v for k, v in payload.items()
                             if k not in {"source", "policy", "categories",
                                          "acted", "scanned_length"}},
                )

            elif et == EventType.ANTI_REQ_VIOLATION:
                self.record_anti_req(
                    session_id=event.session_id,
                    kind=payload.get("kind", "unknown"),
                    message=payload.get("message", ""),
                    tool_name=payload.get("tool") or payload.get("tool_name"),
                    hop=payload.get("hop"),
                )

        return bus.subscribe(accept_all, _handler)

    # ── query helpers ───────────────────────────────────────────────────

    def recent_events(
        self,
        *,
        session_id: str | None = None,
        event_type: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent security events ordered by timestamp desc."""
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM security_events {where} "
            f"ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)
        cur = self._conn.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(zip(cols, row))
            if d.get("details"):
                try:
                    d["details"] = json.loads(d["details"])
                except Exception:  # noqa: BLE001
                    pass
            d["acted"] = bool(d.get("acted"))
            out.append(d)
        return out

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
