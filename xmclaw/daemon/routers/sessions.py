"""Sessions API — list / delete persisted conversation sessions.

Mounted at ``/api/v2/sessions``. Backs the Web UI Sessions page (the
default route in the Hermes layout). Wraps :class:`xmclaw.daemon.
session_store.SessionStore` which persists per-session message lists
to ``~/.xmclaw/v2/sessions.db``.

Endpoints:
    GET    /api/v2/sessions          → ``{"sessions": [...]}``
    GET    /api/v2/sessions/{sid}    → full message list for one session
    DELETE /api/v2/sessions/{sid}    → drop the session (idempotent)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.session_store import SessionStore
from xmclaw.utils.paths import default_sessions_db_path

router = APIRouter(prefix="/api/v2/sessions", tags=["sessions"])


def _store() -> SessionStore | None:
    """Best-effort connect — None when DB init fails (read-only fs etc)."""
    try:
        return SessionStore(default_sessions_db_path())
    except Exception:  # noqa: BLE001
        return None


@router.get("")
async def list_sessions(limit: int = 50) -> JSONResponse:
    """Return up to ``limit`` recent sessions, newest first.

    Each entry: ``{session_id, message_count, updated_at}``. The Web UI
    sorts client-side too so the order is informational.
    """
    store = _store()
    if store is None:
        return JSONResponse({"sessions": [], "error": "session_store unavailable"})
    try:
        rows = store.list_recent(limit=max(1, min(int(limit), 500)))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"sessions": [], "error": str(exc)})
    return JSONResponse({"sessions": rows})


@router.get("/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """Return the full message list (system prompt excluded)."""
    store = _store()
    if store is None:
        return JSONResponse({"error": "session_store unavailable"}, status_code=500)
    try:
        messages = store.load(session_id) or []
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    out: list[dict[str, Any]] = []
    for m in messages:
        out.append({
            "role": m.role,
            "content": m.content or "",
            "tool_call_id": m.tool_call_id,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in m.tool_calls
            ],
        })
    return JSONResponse({"session_id": session_id, "messages": out})


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    store = _store()
    if store is None:
        return JSONResponse({"error": "session_store unavailable"}, status_code=500)
    try:
        store.delete(session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "session_id": session_id})
