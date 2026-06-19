"""Sessions API — list / search / delete persisted conversation sessions.

Mounted at ``/api/v2/sessions``. Backs the Web UI Sessions page (the
default route in the upstream agent layout). Wraps :class:`xmclaw.daemon.
session_store.SessionStore` which persists per-session message lists
to ``~/.xmclaw/v2/sessions.db``.

Endpoints:
    GET    /api/v2/sessions             → ``{"sessions": [...]}``
    GET    /api/v2/sessions/search?q=…  → ``{"sessions": [...]}`` (B-339)
    GET    /api/v2/sessions/{sid}       → full message list for one session
    DELETE /api/v2/sessions/{sid}       → drop the session (idempotent)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.session_store import (
    SessionStore,
    is_internal_session_id,
)
from xmclaw.utils.paths import default_sessions_db_path

router = APIRouter(prefix="/api/v2/sessions", tags=["sessions"])


def _store() -> SessionStore | None:
    """Best-effort connect — None when DB init fails (read-only fs etc)."""
    try:
        return SessionStore(default_sessions_db_path())
    except Exception:  # noqa: BLE001
        return None


@router.get("")
async def list_sessions(
    limit: int = 50,
    include_internal: bool = False,
) -> JSONResponse:
    """Return up to ``limit`` recent sessions, newest first.

    Each entry: ``{session_id, message_count, updated_at, preview}``.
    The Web UI sorts client-side too so the order is informational.

    Wave-32+ (2026-05-19): ``include_internal`` defaults to False so
    the main Sessions list shows only user-authored chats. Internal
    sessions (reflection clones, HTN planner autonomous turns,
    integration smoke runs) are hidden by default but accessible
    via ``?include_internal=true`` for debugging. The cap on
    ``limit`` is applied AFTER filtering so the visible page size
    stays predictable even when many internal sessions are
    interleaved with chats.
    """
    store = _store()
    if store is None:
        return JSONResponse({"sessions": [], "error": "session_store unavailable"})
    requested = max(1, min(int(limit), 500))
    try:
        # Over-fetch by 2x when filtering so a heavily-internal store
        # still surfaces enough real chats. Tighter than nothing,
        # cheaper than scanning the whole table.
        fetch_n = requested if include_internal else min(500, requested * 4)
        rows = store.list_recent(limit=fetch_n)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"sessions": [], "error": str(exc)})
    if not include_internal:
        rows = [
            r for r in rows
            if not is_internal_session_id(str(r.get("session_id") or ""))
        ]
    rows = rows[:requested]
    return JSONResponse({"sessions": rows})


@router.get("/search")
async def search_sessions(
    q: str = "", limit: int = 30, include_internal: bool = False,
) -> JSONResponse:
    """B-339 (audit #12): substring search across all persisted
    session histories. Returns the same shape as ``GET /``, but each
    entry adds ``match_snippet`` — a short context window around the
    query hit so the UI can render previews of where the match
    landed without round-tripping per-session message lists.

    Pre-B-339 the Sessions page filtered client-side by substring
    against session_id + already-expanded messages; sessions the
    user hadn't clicked open weren't searchable at all. The
    ``Phase B-9 will add a real FTS5 search route`` comment in
    Sessions.js was stale — this endpoint closes the gap with a
    SQL LIKE scan (FTS5 + triggers is a future optimization).

    Empty query → ``{"sessions": []}`` (no error). Trims surrounding
    whitespace; ``limit`` caps at 200.
    """
    store = _store()
    if store is None:
        return JSONResponse({"sessions": [], "error": "session_store unavailable"})
    requested = max(1, min(int(limit), 200))
    try:
        fetch_n = requested if include_internal else min(200, requested * 4)
        rows = store.search_messages(q, limit=fetch_n)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"sessions": [], "error": str(exc)})
    if not include_internal:
        rows = [
            r for r in rows
            if not is_internal_session_id(str(r.get("session_id") or ""))
        ]
    rows = rows[:requested]
    return JSONResponse({"sessions": rows, "query": q})


def _reconstruct_from_events(session_id: str, *, bus=None) -> list[dict[str, Any]]:
    """2026-06-16 recovery: rebuild a session's visible message list from
    the DURABLE event log when ``session_store`` has nothing.

    ``session_store`` only persists at turn-END. If the user refreshes
    mid-turn (an in-progress task), the session isn't in the store yet, so
    ``store.load`` returns []  and the conversation looks LOST — even
    though every message is durable in events.db. This reads those events
    and reconstructs ``user`` + ``assistant`` (with tool_calls) messages so
    a refresh restores the conversation instead of dropping it.

    When ``bus`` is provided (e.g. from ``request.app.state.bus``), it is
    reused to avoid the SQLite lock-competition risk of a second
    SqliteEventBus instance.  When absent, a transient read-only instance is
    created as a fallback."""
    _bus = bus
    _created = False
    if _bus is None:
        from xmclaw.core.bus.sqlite import SqliteEventBus, default_events_db_path
        _bus = SqliteEventBus(default_events_db_path())
        _created = True
    try:
        evs = _bus.query(session_id=session_id, limit=5000)
    except Exception:  # noqa: BLE001
        return []
    finally:
        if _created:
            try:
                _bus.close()
            except Exception:  # noqa: BLE001
                pass
    out: list[dict[str, Any]] = []
    for e in sorted(evs, key=lambda x: getattr(x, "ts", 0) or 0):
        t = e.type.value if hasattr(e.type, "value") else str(e.type)
        p = getattr(e, "payload", {}) or {}
        if t == "user_message":
            c = p.get("content")
            if isinstance(c, str) and c.strip():
                out.append({"role": "user", "content": c, "tool_call_id": None, "tool_calls": []})
        elif t == "llm_response":
            txt = p.get("text") or p.get("content") or ""
            tcs = [
                {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args") or {}}
                for tc in (p.get("tool_calls") or []) if isinstance(tc, dict)
            ]
            if (isinstance(txt, str) and txt.strip()) or tcs:
                out.append({"role": "assistant", "content": txt or "", "tool_call_id": None, "tool_calls": tcs})
    return out


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> JSONResponse:
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
    # Recovery fallback: store empty (e.g. refreshed mid-turn) → rebuild
    # from the durable event log so the conversation isn't lost.
    recovered = False
    if not out:
        # Prefer the shared bus instance (if available) to avoid a second
        # SqliteEventBus connection that competes for the WAL lock.
        bus = getattr(request.app.state, "bus", None)
        out = _reconstruct_from_events(session_id, bus=bus)
        recovered = bool(out)
    return JSONResponse({
        "session_id": session_id, "messages": out, "recovered_from_events": recovered,
    })


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> JSONResponse:
    """Delete a session EVERYWHERE so it can't be re-surfaced.

    Must purge all three stores or the session "comes back": the
    session_store row, the DURABLE event log (the task list reconstructs
    from it — leaving events means the session reappears after delete),
    and the live in-memory state (agent history + the WS replay buffer).
    """
    store = _store()
    errors: list[str] = []
    # 1. session_store (persisted message list).
    if store is not None:
        try:
            store.delete(session_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"store: {exc}")
    # 2. durable event log — otherwise /api/v2/tasks re-lists it.
    bus = getattr(request.app.state, "bus", None)
    if bus is not None and hasattr(bus, "delete_session"):
        try:
            bus.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"events: {exc}")
    # 3. live in-memory state: agent history + WS replay buffer.
    agent = getattr(request.app.state, "agent", None)
    if agent is not None and hasattr(agent, "clear_session"):
        try:
            await agent.clear_session(session_id)
        except Exception:  # noqa: BLE001
            pass
    logs = getattr(request.app.state, "session_logs", None)
    if isinstance(logs, dict):
        logs.pop(session_id, None)
    if errors and store is None:
        return JSONResponse({"error": "; ".join(errors)}, status_code=500)
    return JSONResponse({"ok": True, "session_id": session_id, "errors": errors})
