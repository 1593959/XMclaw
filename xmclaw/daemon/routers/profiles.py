"""Profiles API — list and read persona markdown files.

Epic #18 Phase A. Mounted at ``/api/v2/profiles`` by
:func:`xmclaw.daemon.app.create_app`. Backs the web-UI "persona
picker" panel; reads the user's markdown personas from
:func:`xmclaw.utils.paths.persona_dir` (``~/.xmclaw/persona/profiles/``
by default, reroutable via ``XMC_DATA_DIR``).

Read-only on purpose: the writable surface (editor, create/delete) is
deferred to Phase B. Today the user edits profile markdown files
directly; this router just surfaces them to the panel.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse

from xmclaw.utils.paths import persona_dir

router = APIRouter(prefix="/api/v2/profiles", tags=["profiles"])


@router.get("")
async def list_profiles() -> JSONResponse:
    """Return every ``*.md`` under :func:`persona_dir`.

    Response shape: ``{"profiles": [{"id", "title", "path"}, ...]}``.
    A missing directory yields an empty list — a fresh install without
    any personas is a valid state, not a 404.
    """
    pdir = persona_dir()
    profiles: list[dict[str, Any]] = []
    if pdir.exists():
        for md in sorted(pdir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Title = first non-empty line stripped of ``#`` / whitespace,
            # falling back to the filename stem. Deliberately does not
            # parse YAML front-matter — that's a Phase B enhancement.
            title = md.stem
            for line in text.splitlines():
                line = line.strip()
                if line:
                    title = line.lstrip("# ").strip() or md.stem
                    break
            profiles.append({"id": md.stem, "title": title, "path": str(md)})
    return JSONResponse({"profiles": profiles})


@router.get("/{profile_id}")
async def get_profile(profile_id: str) -> JSONResponse:
    """Return the full markdown content for one profile.

    ``profile_id`` is the file stem (``coder.md`` → ``coder``). Path
    traversal is blocked by ``Path.name`` — any ``../`` style input
    collapses to a plain filename before we touch the disk.
    """
    pdir = persona_dir()
    safe_id = profile_id.replace("/", "_").replace("\\", "_")
    md = pdir / f"{safe_id}.md"
    if not md.exists() or not md.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"id": safe_id, "content": text})
