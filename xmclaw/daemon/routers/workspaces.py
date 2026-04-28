"""Workspaces API — CRUD for user-authored agent/tool preset bundles.

Epic #18 Phase A. Mounted at ``/api/v2/workspaces``. Backs the web-UI
"workspaces" panel: each workspace is a small JSON manifest describing
a named agent preset (model, tool toggles, persona). Lives under
:func:`xmclaw.utils.paths.workspaces_dir` (``~/.xmclaw/workspaces/``).

Not to be confused with the daemon's *runtime* workspace (the `v2/`
subdir that holds events.db, PID files, etc). These are human-authored
config bundles the web UI lets the user swap between.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import workspaces_dir

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])


_ALLOWED_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _sanitize_id(raw: str) -> str:
    """Reduce an arbitrary string to filename-safe ASCII.

    Any char outside ``[A-Za-z0-9_-]`` becomes ``_``. Empty inputs
    become ``"default"`` — the endpoint never writes a bare ``.json``
    and never accepts a filename with slashes.
    """
    cleaned = "".join(c if c in _ALLOWED_ID_CHARS else "_" for c in raw)
    return cleaned or "default"


@router.get("")
async def list_workspaces() -> JSONResponse:
    """Return every ``*.json`` under :func:`workspaces_dir`.

    Malformed JSON files are skipped, not fatal — a user hand-editing
    a file should not be able to 500 the whole list.
    """
    ws_dir = workspaces_dir()
    workspaces: list[dict[str, Any]] = []
    if ws_dir.exists():
        for cfg_file in sorted(ws_dir.glob("*.json")):
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            workspaces.append({
                "id": cfg_file.stem,
                "name": data.get("name", cfg_file.stem),
                "description": data.get("description", ""),
                "model": data.get("model", "default"),
            })
    return JSONResponse({"workspaces": workspaces})


@router.post("")
async def create_workspace(request: Request) -> JSONResponse:
    """Write a workspace manifest.

    Uses upsert semantics: POSTing with an existing ``id`` overwrites.
    The web UI has no separate ``PUT`` call, so bundling create/update
    under one verb avoids a round-trip to discover whether to pick
    create vs edit.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    raw_id = body.get("id") or body.get("name") or "default"
    ws_id = _sanitize_id(str(raw_id))

    ws_dir = workspaces_dir()
    ws_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = ws_dir / f"{ws_id}.json"

    payload = {
        "name": body.get("name", ws_id),
        "description": body.get("description", ""),
        "model": body.get("model", "default"),
        "tools": body.get("tools", {}),
    }
    # B-74: atomic write so a daemon crash mid-save can't leave the
    # workspace manifest half-written (which would prevent the daemon
    # from re-loading that workspace on next start).
    from xmclaw.utils.fs_locks import atomic_write_text
    atomic_write_text(
        cfg_file,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return JSONResponse({"ok": True, "id": ws_id})


@router.delete("/{ws_id}")
async def delete_workspace(ws_id: str) -> JSONResponse:
    """Remove a workspace manifest.

    404 on a missing file is deliberate — an idempotent DELETE that
    hides "already gone" from the UI means a concurrent tab's delete
    looks like success; better to surface the race.
    """
    safe_id = _sanitize_id(ws_id)
    cfg_file = workspaces_dir() / f"{safe_id}.json"
    if not cfg_file.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    cfg_file.unlink()
    return JSONResponse({"ok": True})
