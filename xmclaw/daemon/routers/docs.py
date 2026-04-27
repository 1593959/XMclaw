"""Docs API — list and read XMclaw markdown docs from the repo.

GitHub blocks iframe embed via X-Frame-Options:DENY, so the original
Hermes pattern (iframe to docs URL) is dead-on-arrival for our docs.
This router serves ``docs/*.md`` directly as text + lists the
available files; the frontend Markdown pipeline renders them.

Mounted at ``/api/v2/docs``.

GET /api/v2/docs                    → {"docs": [{path, title}, ...]}
GET /api/v2/docs/{path}             → {"path": ..., "content": "..."}
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/docs", tags=["docs"])

# Search a list of plausible docs roots; return the first that exists.
# Mirrors the same pattern serve() uses to find the config file.
def _docs_root() -> Path | None:
    candidates: list[Path] = [
        Path("docs"),
        Path(__file__).resolve().parent.parent.parent.parent / "docs",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p.resolve()
    return None


_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB cap, same as files router


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("# ").strip()
    return ""


@router.get("")
async def list_docs() -> JSONResponse:
    root = _docs_root()
    if root is None:
        return JSONResponse({"docs": [], "error": "docs root not found"})
    out: list[dict] = []
    for md in sorted(root.glob("*.md")):
        try:
            head = _first_heading(md.read_text(encoding="utf-8", errors="replace")[:8192])
        except OSError:
            head = ""
        out.append({
            "path":  md.name,
            "title": head or md.stem,
            "size":  md.stat().st_size if md.exists() else 0,
        })
    return JSONResponse({"docs": out})


@router.get("/{path}")
async def get_doc(path: str) -> JSONResponse:
    root = _docs_root()
    if root is None:
        return JSONResponse({"error": "docs root not found"}, status_code=404)
    # Path-traversal guard: only allow plain ``foo.md`` filenames
    # (no slashes, no '..') — matches the safety posture of the files
    # router.
    if "/" in path or "\\" in path or ".." in path:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not path.endswith(".md"):
        path = path + ".md"
    target = root / path
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        size = target.stat().st_size
        if size > _MAX_BYTES:
            return JSONResponse(
                {"error": f"file larger than {_MAX_BYTES} bytes"},
                status_code=413,
            )
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({
        "path":    path,
        "title":   _first_heading(text) or target.stem,
        "size":    size,
        "content": text,
    })
