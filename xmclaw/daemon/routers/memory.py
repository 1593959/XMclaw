"""Memory Editor API — read/write/search user-authored markdown notes.

Epic #18 Phase A. Mounted at ``/api/v2/memory``. Backs the web-UI
"memory editor" panel: read the list of notes, load one, save it
back, search across them.

Storage: plain markdown files under
:func:`xmclaw.utils.paths.file_memory_dir` (``~/.xmclaw/memory/``).

Distinct from the SQLite-vec long-term memory at
:func:`xmclaw.utils.paths.default_memory_db_path` — that one is
daemon-managed with embeddings; this one is human-authored notes the
user wants to persist and revisit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import file_memory_dir

router = APIRouter(prefix="/api/v2/memory", tags=["memory"])


def _safe_name(filename: str) -> str:
    """Collapse any path traversal to a bare filename and enforce ``.md``.

    ``Path(filename).name`` strips any leading dirs. Empty input after
    stripping becomes ``"note.md"`` so we never create a bare ``.md``
    file with no stem.
    """
    stem = Path(filename).name.strip()
    if not stem:
        return "note.md"
    if not stem.endswith(".md"):
        stem = f"{stem}.md"
    return stem


@router.get("")
async def list_memory() -> JSONResponse:
    """Return filename + size + mtime for every ``*.md`` note.

    Response shape: ``{"files": [...]}``. A missing directory yields
    an empty list — a fresh install is a valid state.
    """
    mdir = file_memory_dir()
    files: list[dict[str, Any]] = []
    if mdir.exists():
        for md in sorted(mdir.glob("*.md")):
            try:
                stat = md.stat()
            except OSError:
                continue
            files.append({
                "name": md.name,
                "path": str(md),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    return JSONResponse({"files": files})


@router.post("/search")
async def search_memory(request: Request) -> JSONResponse:
    """Substring search across the markdown corpus.

    Intentionally simple: case-insensitive substring, 80-char snippet
    around the first hit per file, score always ``1.0``. FTS5 /
    embedding search is Phase B — the web UI only needs "find a note
    I wrote last week" today, and grep nails that for a corpus measured
    in dozens of files.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    query = str(body.get("query", "")).strip()
    if not query:
        return JSONResponse({"results": []})

    results: list[dict[str, Any]] = []
    mdir = file_memory_dir()
    if mdir.exists():
        needle = query.lower()
        for md in sorted(mdir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            idx = low.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - 80)
            end = min(len(text), idx + len(query) + 80)
            snippet = text[start:end]
            results.append({
                "topic": md.stem,
                "snippet": snippet,
                "score": 1.0,
            })
    return JSONResponse({"results": results})


@router.get("/{filename}")
async def get_memory_file(filename: str) -> JSONResponse:
    """Return one note's full markdown body."""
    mdir = file_memory_dir()
    md = mdir / _safe_name(filename)
    if not md.exists() or not md.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"name": md.name, "content": text})


@router.post("/{filename}")
async def save_memory_file(filename: str, request: Request) -> JSONResponse:
    """Upsert a note's body.

    ``.md`` suffix is auto-appended. A missing ``memory/`` dir is
    created — the first save after a clean install must not 500.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    mdir = file_memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    name = _safe_name(filename)
    md = mdir / name
    content = str(body.get("content", ""))
    md.write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True, "name": name})
