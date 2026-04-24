"""Approvals API — manage pending security approvals.

Mounted at ``/api/v2/approvals``. Created by the GuardedToolProvider
when a tool call triggers the ``needs_approval`` path.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/approvals", tags=["approvals"])


def _serialize(pa: Any) -> dict[str, Any]:
    return {
        "request_id": pa.request_id,
        "session_id": pa.session_id,
        "tool_name": pa.tool_name,
        "status": pa.status,
        "created_at": pa.created_at,
        "findings_summary": pa.findings_summary,
    }


@router.get("")
async def list_approvals(request: Request, session_id: str | None = None) -> JSONResponse:
    svc = request.app.state.approval_service
    records = await svc.list_pending(session_id=session_id)
    return JSONResponse({"pending": [_serialize(r) for r in records]})


@router.post("/{request_id}/approve")
async def approve_approval(request: Request, request_id: str) -> JSONResponse:
    svc = request.app.state.approval_service
    ok = await svc.approve(request_id)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "Request not found or already resolved"}, status_code=404)


@router.post("/{request_id}/deny")
async def deny_approval(request: Request, request_id: str) -> JSONResponse:
    svc = request.app.state.approval_service
    ok = await svc.deny(request_id)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "Request not found or already resolved"}, status_code=404)
