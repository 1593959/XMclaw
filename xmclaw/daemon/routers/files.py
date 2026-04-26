"""File Browser API — list directories and read files safely.

Epic #18 Phase A. Mounted at ``/api/v2/files``. Backs the web-UI
"file browser" panel: the user picks a directory and views entries, or
clicks a file to read its contents.

Safety model: every resolved path must sit under one of the
``tools.allowed_dirs`` configured in ``daemon/config.json``, or under
``$HOME`` as the fallback. Any path that escapes that whitelist
yields 403 — never a file read — so a malicious query string cannot
read ``/etc/passwd`` or an SSH key.

The allowed-roots list comes from ``request.app.state.config`` (wired
by :func:`xmclaw.daemon.app.create_app`). If state.config is missing,
home is the only allowed root.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import (
    agents_registry_dir,
    file_memory_dir,
    persona_dir,
    skills_dir,
    workspaces_dir,
)

router = APIRouter(prefix="/api/v2/files", tags=["files"])


# Reject anything bigger than 1 MiB so the panel cannot OOM the browser
# by opening a huge file by accident. 1 MiB is ~20k lines of source, far
# beyond what anyone reads in a UI pane — big enough for reasonable
# configs, small enough not to stream megabytes to a textarea.
_MAX_READ_BYTES = 1 * 1024 * 1024


def _allowed_roots(request: Request) -> list[Path]:
    """Resolve the whitelist of directories the browser may open.

    Pulls ``tools.allowed_dirs`` out of ``request.app.state.config`` and
    always appends ``$HOME`` as the fallback root. Symlink resolution
    happens at this step so a root like ``/home/user/project`` and a
    query for ``/home/user/project/../../etc`` both normalize before
    the containment check runs.
    """
    cfg = getattr(request.app.state, "config", None) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    tools = cfg.get("tools") or {}
    raw_dirs = tools.get("allowed_dirs") or []
    roots: list[Path] = []
    for d in raw_dirs:
        if not isinstance(d, (str, Path)):
            continue
        try:
            roots.append(Path(d).expanduser().resolve())
        except OSError:
            continue
    home = Path.home().resolve()
    if home not in roots:
        roots.append(home)
    return roots


def _safe_path(path_str: str, roots: list[Path]) -> Path | None:
    """Resolve ``path_str`` and confirm containment under ``roots``.

    Returns ``None`` when the path either cannot be resolved (broken
    symlink, permission denied at the parent) or falls outside every
    allowed root. Callers MUST treat a ``None`` return as 403, not 404
    — leaking "exists but forbidden" vs "does not exist" is a
    fingerprint for path enumeration.
    """
    try:
        p = Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for root in roots:
        try:
            p.relative_to(root)
        except ValueError:
            continue
        return p
    return None


def _entry(p: Path) -> dict[str, Any]:
    try:
        stat = p.stat()
        size = stat.st_size if p.is_file() else None
        mtime = stat.st_mtime
    except OSError:
        size = None
        mtime = None
    return {
        "name": p.name,
        "path": str(p),
        "is_dir": p.is_dir(),
        "size": size,
        "mtime": mtime,
    }


@router.get("/roots")
async def workspace_roots() -> JSONResponse:
    """Return the canonical XMclaw workspace roots for the UI file panel.

    Each entry: ``{key, label, path, exists}``. ``exists`` is False for
    a fresh install (no skills written yet, etc.) — the UI shows the
    section anyway so the user knows where things will land.
    """
    roots = [
        ("skills",     "技能",      skills_dir()),
        ("agents",     "智能体",    agents_registry_dir()),
        ("personas",   "人格",      persona_dir()),
        ("memory",     "记忆",      file_memory_dir()),
        ("workspaces", "工作区配置", workspaces_dir()),
    ]
    return JSONResponse({
        "roots": [
            {"key": k, "label": label, "path": str(p), "exists": p.is_dir()}
            for k, label, p in roots
        ],
    })


@router.get("")
async def browse(request: Request, path: str | None = None) -> JSONResponse:
    """Directory listing or file read, depending on ``path``.

    - No ``path`` → list the user's home directory (first allowed root).
    - ``path`` resolves to a directory → ``{"is_dir": true, "entries": [...]}``.
    - ``path`` resolves to a file ≤ 1 MiB → ``{"is_dir": false, "content": "..."}``.
    - ``path`` outside allowed roots → 403. Never 404 (see ``_safe_path``).
    - ``path`` larger than 1 MiB → 413.
    """
    roots = _allowed_roots(request)
    target_str = path or str(roots[0])
    target = _safe_path(target_str, roots)
    if target is None:
        return JSONResponse({"error": "path not allowed"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    if target.is_dir():
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        for child in children:
            # Skip dotfiles in the top-level home listing so the panel
            # doesn't drown in ``.bash_history`` / ``.cache`` / etc.
            # Sub-paths still show them — the user explicitly navigated.
            if target in roots and child.name.startswith("."):
                continue
            entries.append(_entry(child))
        return JSONResponse({
            "is_dir": True,
            "path": str(target),
            "entries": entries,
        })

    # File read path
    try:
        size = target.stat().st_size
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    if size > _MAX_READ_BYTES:
        return JSONResponse(
            {"error": f"file larger than {_MAX_READ_BYTES} bytes"},
            status_code=413,
        )
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({
        "is_dir": False,
        "path": str(target),
        "size": size,
        "content": content,
    })
