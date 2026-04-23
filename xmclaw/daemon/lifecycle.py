"""PID-file based daemon lifecycle for v2: start / stop / restart / status.

v1 had a richer lifecycle module; this is the minimal cross-platform
re-implementation for v2. The one thing we do not do is foreground /
background fiddling — ``xmclaw serve`` stays the foreground entrypoint
that ``uvicorn.run`` expects; ``xmclaw start`` spawns it detached and
watches the PID file.

Filesystem layout (under ``~/.xmclaw/v2/``):

    daemon.pid       plaintext PID of the running daemon
    daemon.meta      one-line JSON: {"host": ..., "port": ..., "ts": ...}
    daemon.log       stdout + stderr of the spawned daemon (rotated? no — append)

Stop semantics:
  * POSIX: SIGTERM -> wait up to ~5s -> SIGKILL.
  * Windows: ``taskkill /PID`` (graceful WM_CLOSE) -> wait ->
    ``taskkill /F`` force. CTRL_BREAK_EVENT was tried first but
    CPython 3.10 on some Windows builds throws WinError 87 on console-
    less processes; taskkill is universally reliable.

Status semantics:
  * ``running`` -> PID file present AND process alive AND /health answers 200.
  * ``stale``   -> PID file present but process is gone.
  * ``dead``    -> no PID file.

"running" is the only state where ``start`` declines to spawn; the
others both clean up and continue.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from xmclaw.utils.paths import default_pid_path as _central_default_pid_path


def default_pid_path() -> Path:
    """Honors ``XMC_V2_PID_PATH`` and ``XMC_DATA_DIR``; delegates to
    :func:`xmclaw.utils.paths.default_pid_path` (§3.1)."""
    return _central_default_pid_path()


def default_meta_path() -> Path:
    # Tied to the PID-file location so a narrow override cascades: if a
    # test reroutes pid to /tmp/foo.pid, meta lands at /tmp/foo.meta.
    return default_pid_path().with_name("daemon.meta")


def default_log_path() -> Path:
    # Sibling to the PID file for the same cascade reason as meta. The
    # central ``default_daemon_log_path`` is the workspace default; this
    # wrapper preserves the pid-override cascade contract.
    return default_pid_path().with_name("daemon.log")


@dataclass
class DaemonStatus:
    state: Literal["running", "stale", "dead"]
    pid: int | None
    host: str | None
    port: int | None
    healthy: bool


def _process_alive(pid: int) -> bool:
    """Cross-platform "is this PID alive?" check."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # tasklist is the most reliable check on Windows; the "0 exists"
        # quirks with os.kill(pid, 0) don't apply uniformly.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user.
        return True
    return True


def _http_healthy(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        import httpx
        r = httpx.get(f"http://{host}:{port}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def read_status() -> DaemonStatus:
    pid_path = default_pid_path()
    meta_path = default_meta_path()
    if not pid_path.exists():
        return DaemonStatus(state="dead", pid=None, host=None, port=None, healthy=False)
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return DaemonStatus(state="dead", pid=None, host=None, port=None, healthy=False)

    host, port = None, None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            host = meta.get("host")
            port = meta.get("port")
        except (OSError, json.JSONDecodeError):
            pass

    if not _process_alive(pid):
        return DaemonStatus(state="stale", pid=pid, host=host, port=port, healthy=False)

    healthy = False
    if host and port:
        healthy = _http_healthy(host, int(port))
    return DaemonStatus(state="running", pid=pid, host=host, port=port, healthy=healthy)


def _write_meta(host: str, port: int) -> None:
    meta_path = default_meta_path()
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"host": host, "port": port, "ts": time.time()}),
        encoding="utf-8",
    )


def _clear_files() -> None:
    for p in (default_pid_path(), default_meta_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def start_daemon(
    *,
    host: str,
    port: int,
    config: str,
    no_auth: bool = False,
    wait_seconds: float = 10.0,
) -> DaemonStatus:
    """Spawn ``xmclaw serve`` detached, wait for /health, write pid+meta.

    Raises RuntimeError if the daemon is already running or fails to
    become healthy within ``wait_seconds``.
    """
    status = read_status()
    if status.state == "running":
        raise RuntimeError(
            f"daemon already running (pid={status.pid}, "
            f"http://{status.host}:{status.port})"
        )
    if status.state == "stale":
        _clear_files()

    pid_path = default_pid_path()
    log_path = default_log_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "xmclaw.cli.main", "serve",
        "--host", host, "--port", str(port), "--config", config,
    ]
    if no_auth:
        cmd.append("--no-auth")

    log_f = log_path.open("ab")  # append binary; subprocess writes bytes
    popen_kwargs: dict = {
        "stdout": log_f, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so the daemon
        # survives the parent and accepts CTRL_BREAK_EVENT on stop.
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        popen_kwargs["close_fds"] = True
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    _write_meta(host, port)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not _process_alive(proc.pid):
            _clear_files()
            raise RuntimeError(
                f"daemon exited before becoming healthy -- see {log_path}"
            )
        if _http_healthy(host, port):
            return read_status()
        time.sleep(0.3)

    # Didn't come up in time — caller decides whether to stop.
    raise RuntimeError(
        f"daemon pid={proc.pid} did not answer /health within "
        f"{wait_seconds}s -- see {log_path}"
    )


def stop_daemon(*, grace_seconds: float = 5.0) -> DaemonStatus:
    """Signal the daemon, wait, clean up. Returns the post-stop status."""
    status = read_status()
    if status.state == "dead":
        return status
    if status.pid is None:
        _clear_files()
        return read_status()

    pid = status.pid
    if sys.platform == "win32":
        # Graceful first (no /F). taskkill sends WM_CLOSE to GUI apps
        # and a termination request to console apps; uvicorn's workers
        # handle it cleanly in most configs.
        subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _process_alive(pid):
            _clear_files()
            return read_status()
        time.sleep(0.2)

    # Didn't go down gracefully -- escalate to force kill.
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    # Final cleanup regardless of outcome.
    time.sleep(0.2)
    _clear_files()
    return read_status()
