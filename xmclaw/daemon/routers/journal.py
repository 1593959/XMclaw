"""Journal API — read/write daily markdown notes (memory/journal/YYYY-MM-DD.md).

Mounted at ``/api/v2/journal``. Backs the Memory page → 日记 tab.

Storage: one markdown file per calendar day under
``~/.xmclaw/memory/journal/`` (a subdirectory of
:func:`xmclaw.utils.paths.file_memory_dir`). Files are
``YYYY-MM-DD.md`` so a chronological ``sorted()`` is the natural order
and date math stays trivial.

A daily journal is a deliberately *separate surface* from the generic
Memory editor (``/api/v2/memory``) — the user wants "what happened
today" / "what did the agent and I figure out together" as a thing you
flip through by date, not as a long-tail list of topic notes.
"""
from __future__ import annotations

import json
import re
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import file_memory_dir

router = APIRouter(prefix="/api/v2/journal", tags=["journal"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _journal_dir() -> Path:
    return file_memory_dir() / "journal"


def _safe_date(date_str: str) -> str | None:
    """Validate ``YYYY-MM-DD``. Returns the canonicalized string on hit.

    Accepts the literal token ``"today"`` and resolves it to the local
    calendar date — convenient for the UI's "open today" affordance
    without making the client depend on a clock that agrees with the
    daemon's.
    """
    if date_str == "today":
        return _date.today().isoformat()
    if not _DATE_RE.match(date_str):
        return None
    try:
        # Reject 2026-13-99 etc. by round-tripping through fromisoformat.
        return _date.fromisoformat(date_str).isoformat()
    except ValueError:
        return None


@router.get("")
async def list_journal() -> JSONResponse:
    """Return every dated entry, newest first.

    Shape: ``{"entries": [{"date", "size", "mtime", "preview"}, ...]}``
    where ``preview`` is the first 80 chars (after stripping markdown
    headers) so the UI can show a list with hints without re-fetching
    each file.
    """
    jdir = _journal_dir()
    entries: list[dict[str, Any]] = []
    if jdir.exists():
        for md in sorted(jdir.glob("*.md"), reverse=True):
            stem = md.stem
            if not _DATE_RE.match(stem):
                continue
            try:
                stat = md.stat()
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            preview_lines = [
                ln.lstrip("# ").strip() for ln in text.splitlines()
                if ln.strip()
            ]
            preview = (preview_lines[0] if preview_lines else "")[:80]
            entries.append({
                "date": stem,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "preview": preview,
            })
    return JSONResponse({"entries": entries})


@router.get("/{date_str}")
async def get_journal_entry(date_str: str) -> JSONResponse:
    """Return one day's content. ``date_str = "today"`` is supported.

    Missing entries return an empty content string with ``exists=False``
    so the UI can show "today is blank" instead of a 404 — this is the
    canonical "open today" path.
    """
    canonical = _safe_date(date_str)
    if canonical is None:
        return JSONResponse(
            {"ok": False, "error": "invalid date — expected YYYY-MM-DD"},
            status_code=400,
        )
    md = _journal_dir() / f"{canonical}.md"
    if not md.exists():
        return JSONResponse({
            "date": canonical, "content": "", "exists": False, "size": 0,
        })
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
        stat = md.stat()
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({
        "date": canonical,
        "content": text,
        "exists": True,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    })


@router.put("/{date_str}")
async def upsert_journal_entry(
    date_str: str, request: Request,
) -> JSONResponse:
    """Upsert one day's content. Body: ``{"content": "..."}``.

    Empty content deletes the file (cleaner than a stub
    ``YYYY-MM-DD.md`` with zero bytes for every day the user accidentally
    opened the editor on). Missing journal directory is created.
    """
    canonical = _safe_date(date_str)
    if canonical is None:
        return JSONResponse(
            {"ok": False, "error": "invalid date — expected YYYY-MM-DD"},
            status_code=400,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid json"}, status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "invalid json"}, status_code=400,
        )
    content = str(body.get("content", ""))
    jdir = _journal_dir()
    md = jdir / f"{canonical}.md"
    if not content.strip():
        # Clean up an empty save — don't litter the dir with blank files.
        if md.exists():
            try:
                md.unlink()
            except OSError:
                pass
        return JSONResponse({
            "ok": True, "date": canonical, "exists": False, "size": 0,
        })
    jdir.mkdir(parents=True, exist_ok=True)
    # B-74: atomic write — matches the journal_append tool path (B-71)
    # so daemon crash mid-save can't truncate today's journal.
    from xmclaw.utils.fs_locks import atomic_write_text
    atomic_write_text(md, content)
    return JSONResponse({
        "ok": True,
        "date": canonical,
        "exists": True,
        "size": len(content.encode("utf-8")),
    })
