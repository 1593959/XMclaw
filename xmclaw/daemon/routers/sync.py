"""UI state sync API — Wave 13.

Most of XMclaw is already device-agnostic: session history lives in
events.db on the daemon, autobio memory in autobiographical.db, skills
in the registry. The remaining device-local state is what each
browser tab keeps in ``localStorage``:

  * ``xmc-active-session-id``   — which chat tab is "current"
  * ``xmc-llm-profile``         — picked model
  * ``xmc-density`` / theme     — UI prefs
  * ``xmc-audio-*``             — voice settings

Without sync, you can't "pick up on phone where you left off on
desktop" — even though the conversation history IS there, you have to
manually pick which session, set the model, etc.

This router provides a tiny key-value store the frontend can push to
on change and pull from on boot. Backed by ``~/.xmclaw/v2/ui_state.json``
with atomic writes + a per-process asyncio.Lock.

Single-user model: one user → one state document. Wave 18 may layer
per-user scoping on top (route into ``ui_state/<user_id>.json``).

Endpoints:

  GET  /api/v2/sync/ui-state          — full document {state, updated_ts}
  PUT  /api/v2/sync/ui-state          — full replace ({state}) — debounced client-side
  PATCH /api/v2/sync/ui-state         — merge update ({state}) — single-key writes

Wire: gated by pairing-token middleware like the rest of /api/v2/*.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import data_dir

router = APIRouter(prefix="/api/v2/sync", tags=["sync"])
_log = get_logger(__name__)
_WRITE_LOCK = asyncio.Lock()


def _state_path() -> Any:
    return data_dir() / "v2" / "ui_state.json"


def _read_state() -> dict[str, Any]:
    p = _state_path()
    try:
        if not p.exists():
            return {"state": {}, "updated_ts": 0.0}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"state": {}, "updated_ts": 0.0}
        return {
            "state": data.get("state") if isinstance(data.get("state"), dict) else {},
            "updated_ts": float(data.get("updated_ts") or 0.0),
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("sync.ui_state_read_failed err=%s", exc)
        return {"state": {}, "updated_ts": 0.0}


def _write_state(state: dict[str, Any]) -> dict[str, Any]:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    payload = {"state": state, "updated_ts": ts}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)
    return payload


def _coerce_state(raw: Any) -> dict[str, Any]:
    """Accept ``{"state": {...}}`` (canonical) or ``{...}`` (already
    the state body). Reject anything else."""
    if not isinstance(raw, dict):
        raise ValueError("body must be a JSON object")
    if "state" in raw and isinstance(raw["state"], dict):
        return raw["state"]
    return raw


@router.get("/ui-state")
async def get_ui_state() -> JSONResponse:
    return JSONResponse(_read_state())


@router.put("/ui-state")
async def put_ui_state(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"error": "invalid_json"}, status_code=400,
        )
    try:
        state = _coerce_state(body)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=400,
        )
    async with _WRITE_LOCK:
        result = await asyncio.to_thread(_write_state, state)
    return JSONResponse(result)


@router.patch("/ui-state")
async def patch_ui_state(request: Request) -> JSONResponse:
    """Merge update — only the keys in the body are touched, the rest
    are preserved. Cleaner client-side when you flip a single setting."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"error": "invalid_json"}, status_code=400,
        )
    try:
        patch = _coerce_state(body)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=400,
        )
    async with _WRITE_LOCK:
        existing = _read_state()
        merged = {**existing.get("state", {}), **patch}
        result = await asyncio.to_thread(_write_state, merged)
    return JSONResponse(result)


__all__ = ["router"]
