"""ApprovalService — manages pending security approvals with GC.

In-memory store.  Pending approvals expire after 30 min or when the
pending cap (200) is reached.  Completed records expire after 1 hr or
when the completed cap (500) is reached.

``consume_approval`` implements the one-shot replay pattern: once a
user approves a specific tool call, resending the same message lets the
:class:`GuardedToolProvider` bypass the guard for that exact parameter
set.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal


Status = Literal["pending", "approved", "denied", "timeout"]

_GC_PENDING_MAX_AGE_SECONDS = 30 * 60
_GC_COMPLETED_MAX_AGE_SECONDS = 60 * 60
_GC_MAX_PENDING = 200
_GC_MAX_COMPLETED = 500


@dataclass
class PendingApproval:
    request_id: str
    session_id: str
    tool_name: str
    tool_params: dict[str, Any]
    findings_summary: str
    status: Status
    created_at: float
    resolved_at: float | None = None


class ApprovalService:
    """Thread-safe in-memory approval store with automatic GC."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._completed: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        session_id: str,
        tool_name: str,
        tool_params: dict[str, Any],
        findings_summary: str,
    ) -> str:
        """Create a pending approval and return its request_id."""
        request_id = uuid.uuid4().hex[:12]
        record = PendingApproval(
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_params=tool_params,
            findings_summary=findings_summary,
            status="pending",
            created_at=time.monotonic(),
        )
        async with self._lock:
            self._pending[request_id] = record
            self._gc()
        return request_id

    async def approve(self, request_id: str) -> bool:
        """Mark a pending request as approved."""
        async with self._lock:
            record = self._pending.pop(request_id, None)
            if record is None:
                return False
            record.status = "approved"
            record.resolved_at = time.monotonic()
            self._completed[request_id] = record
            self._gc()
        return True

    async def deny(self, request_id: str) -> bool:
        """Mark a pending request as denied."""
        async with self._lock:
            record = self._pending.pop(request_id, None)
            if record is None:
                return False
            record.status = "denied"
            record.resolved_at = time.monotonic()
            self._completed[request_id] = record
            self._gc()
        return True

    async def get(self, request_id: str) -> PendingApproval | None:
        async with self._lock:
            return self._pending.get(request_id) or self._completed.get(request_id)

    async def list_pending(
        self, session_id: str | None = None
    ) -> list[PendingApproval]:
        async with self._lock:
            records = list(self._pending.values())
        if session_id is not None:
            records = [r for r in records if r.session_id == session_id]
        # newest first
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def consume_approval(
        self, session_id: str, tool_name: str, tool_params: dict[str, Any]
    ) -> bool:
        """Check whether the user has already approved this exact call.

        Looks in ``_completed`` for an ``approved`` record with matching
        *session_id*, *tool_name*, and *tool_params*.  If found, the
        record is deleted (one-shot) and ``True`` is returned so the
        caller can bypass the guard.
        """
        async with self._lock:
            for req_id, record in list(self._completed.items()):
                if (
                    record.status == "approved"
                    and record.session_id == session_id
                    and record.tool_name == tool_name
                    and record.tool_params == tool_params
                ):
                    del self._completed[req_id]
                    return True
        return False

    # ------------------------------------------------------------------
    # GC — private, must be called while holding the lock
    # ------------------------------------------------------------------

    def _gc(self) -> None:
        now = time.monotonic()

        # Pending: timeout old records
        for req_id, record in list(self._pending.items()):
            if now - record.created_at > _GC_PENDING_MAX_AGE_SECONDS:
                record.status = "timeout"
                record.resolved_at = now
                self._completed[req_id] = record
                del self._pending[req_id]

        # Pending: cap eviction (oldest first)
        if len(self._pending) > _GC_MAX_PENDING:
            sorted_pending = sorted(
                self._pending.items(), key=lambda kv: kv[1].created_at
            )
            evict_count = len(self._pending) - _GC_MAX_PENDING
            for req_id, record in sorted_pending[:evict_count]:
                record.status = "timeout"
                record.resolved_at = now
                self._completed[req_id] = record
                del self._pending[req_id]

        # Completed: age eviction
        for req_id, record in list(self._completed.items()):
            resolved = record.resolved_at or record.created_at
            if now - resolved > _GC_COMPLETED_MAX_AGE_SECONDS:
                del self._completed[req_id]

        # Completed: cap eviction (oldest first)
        if len(self._completed) > _GC_MAX_COMPLETED:
            sorted_completed = sorted(
                self._completed.items(),
                key=lambda kv: kv[1].resolved_at or kv[1].created_at,
            )
            evict_count = len(self._completed) - _GC_MAX_COMPLETED
            for req_id, _ in sorted_completed[:evict_count]:
                del self._completed[req_id]
