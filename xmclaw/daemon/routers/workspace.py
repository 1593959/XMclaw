"""Workspace API — current project root list + primary picker.

Mounted at ``/api/v2/workspace``. Backs the new Web-UI workspace folder
picker (frontend ``pages/Workspace.js``). Distinct from the legacy
``/api/v2/workspaces`` (plural) router which surfaces saved
"agent preset bundles" — those are Phase 3+ agent profiles material.

Contract:
* ``GET /api/v2/workspace`` →
    ``{"roots": [{"path","name","vcs","commit_hash"}], "primary_index": N}``
* ``PUT /api/v2/workspace``  body ``{"action":"add","path":"...","name":?}``
                                     ``{"action":"remove","path":"..."}``
                                     ``{"action":"set_primary","index":N}``
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from starlette.responses import JSONResponse

from xmclaw.core.workspace import WorkspaceManager

router = APIRouter(prefix="/api/v2/workspace", tags=["workspace"])

_manager = WorkspaceManager()


def _state_payload() -> dict[str, Any]:
    state = _manager.get()
    return {
        "roots": [r.to_dict() for r in state.roots],
        "primary_index": state.primary_index,
    }


@router.get("")
async def get_workspace() -> JSONResponse:
    return JSONResponse(_state_payload())


@router.put("")
async def update_workspace(
    payload: dict[str, Any] = Body(...),
) -> JSONResponse:
    action = payload.get("action") if isinstance(payload, dict) else None
    if action == "add":
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return JSONResponse(
                {"error": "path required"}, status_code=400
            )
        name = payload.get("name")
        name_str = name.strip() if isinstance(name, str) and name.strip() else None
        try:
            root = _manager.add(path, name=name_str)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        body = _state_payload()
        body["added"] = root.to_dict()
        return JSONResponse(body)

    if action == "remove":
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return JSONResponse(
                {"error": "path required"}, status_code=400
            )
        try:
            removed = _manager.remove(path)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        body = _state_payload()
        body["removed"] = bool(removed)
        return JSONResponse(body)

    if action == "set_primary":
        idx_raw = payload.get("index")
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "index must be an integer"}, status_code=400
            )
        try:
            moved = _manager.set_primary(idx)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        body = _state_payload()
        body["moved"] = bool(moved)
        return JSONResponse(body)

    return JSONResponse(
        {"error": f"unknown action {action!r}"}, status_code=400
    )
