"""System actions API — restart daemon + upgrade xmclaw.

Mounted at ``/api/v2/system``. Backs the Config page → 系统 panel.

Both endpoints are intentionally narrow:

* ``POST /api/v2/system/restart`` — schedule a graceful shutdown and
  spawn a detached subprocess that re-runs ``xmclaw start`` after a
  short delay. The current daemon answers the HTTP call, then the
  lifespan tears down. The relauncher process inherits no parent and
  picks up whatever ``xmclaw`` is on PATH.

* ``POST /api/v2/system/upgrade`` — invoke
  ``pip install --upgrade xmclaw`` in a *background* subprocess and
  return immediately. The actual upgrade is observable via
  ``GET /api/v2/system/upgrade/status`` (re-reads the captured
  stdout/stderr). After upgrade the user re-runs ``重启 DAEMON`` —
  we deliberately don't auto-restart, because pip's exit status alone
  isn't enough proof of a healthy install (e.g. PEP 668-managed envs).

Neither is a replacement for the ``xmclaw`` CLI — they exist so the web
UI can do the equivalent in one click without dropping into a terminal.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse

from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import data_dir

router = APIRouter(prefix="/api/v2/system", tags=["system"])
_log = get_logger(__name__)


# Where pip's stdout/stderr lands so the UI can poll for progress.
def _upgrade_log_path() -> Path:
    return data_dir() / "v2" / "upgrade.log"


# ──────────────────────────────────────────────────────────────────────
# Restart
# ──────────────────────────────────────────────────────────────────────


@router.post("/restart")
async def restart_daemon() -> JSONResponse:
    """Schedule a clean restart of the daemon.

    Strategy:

    1. Spawn a detached child that sleeps 1.5s, then runs
       ``xmclaw start`` (or ``python -m xmclaw.cli.entry start`` as a
       fallback). The sleep is enough for the current process to flush
       its response and tear down the FastAPI lifespan.
    2. Schedule a same-loop ``asyncio.sleep + os._exit(0)`` so the
       current daemon dies cleanly. We use ``os._exit`` rather than
       ``sys.exit`` because uvicorn intercepts ``SystemExit`` for
       shutdown, and we *want* to skip its slow-path shutdown here —
       the relauncher will start a fresh process.
    """
    cmd = _resolve_relaunch_cmd()
    if cmd is None:
        return JSONResponse(
            {"ok": False, "error": "could not resolve xmclaw entry point"},
            status_code=500,
        )

    creationflags = 0
    start_new_session = True
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the child must
        # outlive its parent, and stdin/stdout pipes from FastAPI's
        # working directory mustn't tie it back. CREATE_NO_WINDOW keeps
        # a console from flashing on Windows.
        creationflags = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | 0x08000000  # CREATE_NO_WINDOW
        )
        start_new_session = False

    # The relauncher: sleep 1.5s, then exec the start command. Using a
    # platform-appropriate shell-equivalent so we don't rely on cmd.exe
    # being importable from Python on locked-down Windows.
    if os.name == "nt":
        # Use cmd /c "timeout /t 2 >nul & <cmd>" so the shell is the
        # one waiting, not Python.
        relauncher = ["cmd", "/c", f"timeout /t 2 /nobreak >nul & {' '.join(cmd)}"]
    else:
        relauncher = ["sh", "-c", f"sleep 1.5; {' '.join(cmd)}"]

    try:
        subprocess.Popen(  # noqa: S603 — args are program-defined
            relauncher,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
            cwd=str(Path.home()),
        )
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"spawn failed: {exc}"},
            status_code=500,
        )

    # Schedule a hard exit so uvicorn lets go after the response flushes.
    asyncio.get_event_loop().call_later(0.8, lambda: os._exit(0))
    return JSONResponse({
        "ok": True, "message": "daemon restart scheduled",
        "relaunch_in_seconds": 2,
        "relaunch_cmd": cmd,
    })


def _resolve_relaunch_cmd() -> list[str] | None:
    """Pick the best command to relaunch the daemon.

    1. If ``xmclaw`` is on PATH (typical pip install), use it.
    2. Otherwise fall back to ``python -m xmclaw.cli.entry start``,
       which works for the editable-install dev environment.
    """
    import shutil
    xm = shutil.which("xmclaw")
    if xm:
        return [xm, "start"]
    return [sys.executable, "-m", "xmclaw.cli.entry", "start"]


# ──────────────────────────────────────────────────────────────────────
# Upgrade
# ──────────────────────────────────────────────────────────────────────


_UPGRADE_PROC: subprocess.Popen | None = None


@router.post("/upgrade")
async def upgrade_xmclaw() -> JSONResponse:
    """Trigger ``pip install --upgrade xmclaw`` in the background.

    Returns immediately with a job handle; poll
    ``/api/v2/system/upgrade/status`` for progress + final return code.
    """
    global _UPGRADE_PROC
    if _UPGRADE_PROC is not None and _UPGRADE_PROC.poll() is None:
        return JSONResponse({
            "ok": False,
            "error": "upgrade already in progress",
            "pid": _UPGRADE_PROC.pid,
        }, status_code=409)

    log_path = _upgrade_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Fresh log per attempt so the UI can show "this run" without
    # mixing in the prior one. B-74: atomic — keeps the log readable
    # if the daemon dies between the truncate-old and write-new steps.
    from xmclaw.utils.fs_locks import atomic_write_text
    atomic_write_text(
        log_path,
        f"# xmclaw upgrade started @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
    )

    # ``sys.executable -m pip`` because the installed-as-script ``pip``
    # may be in a different env than the daemon — using the daemon's own
    # interpreter guarantees we upgrade the env that's actually running
    # XMclaw.
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "xmclaw"]

    try:
        log_fp = log_path.open("a", encoding="utf-8")
        _UPGRADE_PROC = subprocess.Popen(  # noqa: S603
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            close_fds=True,
            cwd=str(Path.home()),
        )
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"spawn failed: {exc}"},
            status_code=500,
        )

    return JSONResponse({
        "ok": True,
        "pid": _UPGRADE_PROC.pid,
        "log_path": str(log_path),
        "cmd": cmd,
    })


@router.get("/upgrade/status")
async def upgrade_status() -> JSONResponse:
    """Snapshot of the current/last upgrade.

    Reads the captured pip output (last 100 lines) and reports whether
    the process is still running or has exited (with returncode).
    """
    log_path = _upgrade_log_path()
    log_lines: list[str] = []
    if log_path.exists():
        try:
            log_lines = log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            log_lines = []

    state: dict[str, Any] = {
        "running": False,
        "pid": None,
        "returncode": None,
        "log_tail": log_lines[-100:],
        "log_path": str(log_path),
    }
    if _UPGRADE_PROC is not None:
        rc = _UPGRADE_PROC.poll()
        state["pid"] = _UPGRADE_PROC.pid
        if rc is None:
            state["running"] = True
        else:
            state["returncode"] = rc
    return JSONResponse(state)
