"""Session-workspace HTTP surface (F1).

Mounted at ``/api/v2/session_workspaces``. The chat-page right-side
``WorkspacePanel`` calls these to render the file tree, read individual
files, and inspect the git-backed change history.

State lives in :class:`xmclaw.daemon.workspace_manager.WorkspaceManager`,
attached to ``app.state.workspace_manager`` at lifespan startup. Routes
delegate there for all I/O — this module is HTTP shape only.

Path is ``session_workspaces`` (not ``workspaces``) on purpose: the
existing ``/api/v2/workspaces`` endpoints (Epic #18) deal with
user-authored agent persona / tool-preset bundles. Reusing that prefix
would confuse both call sites.
"""
from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.responses import FileResponse, JSONResponse

router = APIRouter(
    prefix="/api/v2/session_workspaces",
    tags=["session_workspaces"],
)


def _mgr(request: Request) -> Any:
    return getattr(request.app.state, "workspace_manager", None)


@router.get("/{session_id}/tree")
async def get_tree(session_id: str, request: Request) -> JSONResponse:
    """Flat list of files in the session's workspace.

    Each entry: ``{rel_path, kind, size, mtime}``. The UI builds the
    visual tree from the slash-separated ``rel_path``. Returns an empty
    list (not 404) when the workspace doesn't exist yet — that's a
    normal pre-write state, not an error.
    """
    mgr = _mgr(request)
    if mgr is None:
        return JSONResponse({"ok": True, "entries": []})
    try:
        mgr.ensure_dir(session_id)
        entries = mgr.list_tree(session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({"ok": True, "entries": entries})


@router.get("/{session_id}/file")
async def get_file(
    session_id: str,
    request: Request,
    path: str = Query(..., description="workspace-relative path"),
) -> JSONResponse:
    """Read a single file. UTF-8 decoded; binary files return ``kind=binary``
    with empty content and the UI shows a placeholder."""
    mgr = _mgr(request)
    if mgr is None:
        return JSONResponse(
            {"ok": False, "error": "manager_unavailable"}, status_code=503,
        )
    result = mgr.read_file(session_id, path)
    if not result.get("ok"):
        # Distinguish 404 from 403-like errors so the UI can show the
        # right message without leaking enumeration info to the caller.
        err = result.get("error", "")
        status = 404 if err == "not_found" else 400
        return JSONResponse(result, status_code=status)
    return JSONResponse(result)


@router.get("/{session_id}/raw")
async def get_raw(
    session_id: str,
    request: Request,
    path: str = Query(..., description="workspace-relative path"),
) -> Any:
    """Serve the file bytes with a real mime type — backs ``<img>`` /
    ``<iframe>`` previews (images, PDF, HTML, SVG) in the WorkspacePanel.

    HTML/SVG note: served with ``Content-Disposition: inline`` but the UI
    renders them inside a sandboxed iframe (no same-origin, no top-nav),
    so a prompt-injected ``<script>`` in an agent-written file can't
    reach the app's token or DOM.
    """
    mgr = _mgr(request)
    if mgr is None:
        return JSONResponse(
            {"ok": False, "error": "manager_unavailable"}, status_code=503,
        )
    target = mgr.resolve_safe(session_id, path)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "not_found"}, status_code=404,
        )
    mime, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        target,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/{session_id}/commits")
async def get_commits(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    """List recent auto-commits ``{sha, ts, subject, files}`` — newest first."""
    mgr = _mgr(request)
    if mgr is None:
        return JSONResponse({"ok": True, "commits": []})
    try:
        commits = await mgr.list_commits(session_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({"ok": True, "commits": commits})


@router.get("/{session_id}/diff")
async def get_diff(
    session_id: str,
    request: Request,
    commit: str = Query(..., description="commit sha"),
) -> JSONResponse:
    """Unified diff for one auto-commit. UI parses + colours the raw output."""
    mgr = _mgr(request)
    if mgr is None:
        return JSONResponse(
            {"ok": False, "error": "manager_unavailable"}, status_code=503,
        )
    result = await mgr.commit_diff(session_id, commit)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)
