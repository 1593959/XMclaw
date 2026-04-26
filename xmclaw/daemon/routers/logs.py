"""Logs API — tail the daemon's log files for the Web UI Logs page.

Mounted at ``/api/v2/logs``. Returns the last N lines of one of the
known log files (daemon.log + future agent.log / errors.log split).
Optional grep-style filter by level / component substring.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import JSONResponse

from xmclaw.utils.paths import default_daemon_log_path, logs_dir

router = APIRouter(prefix="/api/v2/logs", tags=["logs"])

# Map UI file ids → on-disk path. ``daemon`` is the only one we
# currently emit; the others are reserved for the Phase 6+ structured
# log split (errors.log / gateway.log).
_FILES = {
    "daemon":  lambda: default_daemon_log_path(),
    "agent":   lambda: logs_dir() / "agent.log",
    "errors":  lambda: logs_dir() / "errors.log",
    "gateway": lambda: logs_dir() / "gateway.log",
}

_DEFAULT_LINES = 200
_MAX_LINES = 2000


def _tail_text(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path`` as a list of strings.

    Defensive: missing or unreadable files yield ``[]`` rather than
    raising, so the UI always gets a 200 with an empty list.
    """
    if not path.exists():
        return []
    try:
        # For files small enough to slurp, this is fine. The daemon log
        # rarely grows past a few MB before InnoSetup's installer scripts
        # rotate it. If we ever cross 100 MB we'll switch to a streaming
        # ring buffer.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if len(lines) > n:
        lines = lines[-n:]
    return lines


@router.get("")
async def get_logs(
    file: str = "daemon",
    lines: int = _DEFAULT_LINES,
    level: str = "ALL",
    component: str = "all",
) -> JSONResponse:
    resolver = _FILES.get(file)
    if resolver is None:
        return JSONResponse(
            {"error": f"unknown file {file!r}", "available": list(_FILES)},
            status_code=400,
        )
    n = max(1, min(int(lines), _MAX_LINES))
    raw = _tail_text(resolver(), n)

    out: list[str] = []
    level_u = (level or "ALL").upper().strip()
    comp = (component or "all").lower().strip()
    for line in raw:
        if level_u != "ALL" and level_u not in line.upper():
            continue
        if comp != "all" and comp not in line.lower():
            continue
        out.append(line)
    return JSONResponse({
        "file": file,
        "level": level_u,
        "component": comp,
        "count": len(out),
        "total": len(raw),
        "lines": out,
    })
