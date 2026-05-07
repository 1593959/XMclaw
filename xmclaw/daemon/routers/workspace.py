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
    """B-152: per-root ``exists`` + ``looks_temp`` flags so the UI can
    visibly mark stale entries (path doesn't exist on disk anymore)
    and pytest-/Temp- artifacts (test runs that registered workspaces
    and didn't clean up).
    """
    from pathlib import Path
    state = _manager.get()
    roots: list[dict[str, Any]] = []
    for r in state.roots:
        d = r.to_dict()
        path_str = str(d.get("path", ""))
        try:
            d["exists"] = Path(path_str).is_dir()
        except (OSError, ValueError):
            d["exists"] = False
        # Detect well-known transient locations so the UI can show
        # a warning + offer cleanup. Both Windows and POSIX shapes.
        norm = path_str.replace("\\", "/").lower()
        d["looks_temp"] = (
            "/pytest-of-" in norm
            or "/appdata/local/temp/" in norm
            or norm.startswith("/tmp/")
            or "/.xmworktrees/" in norm        # B-235: new ephemeral worktrees
            or "/.claude/worktrees/" in norm   # legacy back-compat
        )
        roots.append(d)
    return {
        "roots": roots,
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

    if action == "prune_missing":
        # B-152: bulk-remove every registered root whose path doesn't
        # exist on disk anymore. Optional ``include_temp=true`` also
        # strips pytest-of-* / AppData/Local/Temp/ entries even when
        # they happen to still exist (test cleanup didn't fire).
        from pathlib import Path
        include_temp = bool(payload.get("include_temp"))
        state = _manager.get()
        removed_paths: list[str] = []
        for r in list(state.roots):
            path_str = str(r.path)
            missing = False
            try:
                missing = not Path(path_str).is_dir()
            except (OSError, ValueError):
                missing = True
            norm = path_str.replace("\\", "/").lower()
            looks_temp = (
                "/pytest-of-" in norm
                or "/appdata/local/temp/" in norm
                or norm.startswith("/tmp/")
                or "/.xmworktrees/" in norm        # B-235
                or "/.claude/worktrees/" in norm   # legacy back-compat
            )
            if missing or (include_temp and looks_temp):
                try:
                    if _manager.remove(path_str):
                        removed_paths.append(path_str)
                except OSError:
                    continue
        body = _state_payload()
        body["pruned"] = removed_paths
        return JSONResponse(body)

    return JSONResponse(
        {"error": f"unknown action {action!r}"}, status_code=400
    )
