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


def _resolve_python_executable() -> str:
    r"""Return the real Python interpreter, not a Windows launcher stub.

    On Windows with a venv created from a conda/base environment,
    ``sys.executable`` points at ``.venv\Scripts\python.exe`` which is
    actually ``py.exe`` (``InternalName = "Python Launcher"``). When
    ``subprocess.Popen`` spawns it, the returned PID belongs to the
    launcher — the launcher then starts ``sys._base_executable`` (the
    real ``python.exe``) as a child and exits seconds later.

    This causes two production bugs:

    1. **Stale PID file**: ``xmclaw start`` writes the launcher's PID
       to ``daemon.pid``. ``xmclaw stop`` finds it dead, declares
       "stopped", but the real worker is still alive and holding the
       port + databases.
    2. **Orphan daemon stacking**: The operator re-runs ``xmclaw start``
       → a second daemon starts → both compete for the same SQLite
       files → WAL contention → multi-minute reply delays.

    Fix: on Windows, if ``sys._base_executable`` exists and differs from
    ``sys.executable``, use the base executable directly so the PID we
    record is the PID of the process that actually owns the socket.

    2026-06-19: Guard against venv escape. ``uv`` / ``venv`` set
    ``sys._base_executable`` to the global interpreter (e.g.
    ``AppData\Roaming\uv\python\...``). That interpreter has its own
    ``sys.path`` and does NOT see the venv's site-packages — spawning
    it causes ``ModuleNotFoundError: No module named 'xmclaw'``. When
    we detect a venv (``sys.prefix != sys.base_prefix``), stay inside
    it and use ``sys.executable`` directly.
    """
    exe = sys.executable
    if sys.platform == "win32" and hasattr(sys, "_base_executable"):
        base = sys._base_executable
        in_venv = getattr(sys, "prefix", "") != getattr(sys, "base_prefix", "")
        if not in_venv and base and base != exe and Path(base).exists():
            return base
    return exe


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


def _http_healthy(host: str, port: int, timeout: float = 3.0) -> bool:
    """Probe ``GET /health`` to confirm the daemon is serving.

    2026-05-11 perf fix: switched from ``httpx`` to stdlib
    ``urllib.request`` because on Windows + HTTP_PROXY env var set
    (common with clash / v2ray users), httpx's per-call cold-start
    (Client construction + proxy resolution + h2 negotiation)
    consistently takes 1–2.5 seconds *just to send the request*,
    even when ``trust_env=False``. ``curl`` and stdlib ``urllib``
    handle the same /health probe in <10ms. The legacy 1.0s
    timeout in this function caused EVERY ``xmclaw start`` poll
    to fail client-side — daemon was up but the CLI never saw it,
    burning all 30s of ``wait_seconds`` on local-Python overhead.

    Now: urllib (no cold-init tax) + 3s per-call timeout (margin
    for a busy event loop that's mid-warmup but still capable of
    serving /health within a normal HTTP turnaround).
    """
    try:
        import urllib.request
        url = f"http://{host}:{port}/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
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


# ── 2026-06-08 端口属主感知(修"重启没杀旧进程") ──────────────────────
# 根因:stop/start 只信 daemon.pid;一旦 pid 文件失准(指向已死/错的 pid),
# stop 杀错进程、真正占着端口的旧 daemon 永远活着,start 又因 _http_healthy 只
# 看"端口有人应答"而误判成功 → 两个 daemon 并存、旧的霸占端口跑旧代码。
# 解法:按「谁真正 LISTEN 在这个端口」来杀/判,而不是只认 pid 文件。

def _port_listener_pid(port: int) -> int | None:
    """返回正在 LISTEN ``port`` 的进程 pid;无则 None。psutil 优先,netstat/lsof 兜底。"""
    try:
        import psutil  # 可选依赖(cognition-process extra),有就用
        for c in psutil.net_connections(kind="inet"):
            la = getattr(c, "laddr", None)
            if la and getattr(la, "port", None) == port and c.status == psutil.CONN_LISTEN:
                return c.pid
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0] == "TCP"
                        and parts[1].rsplit(":", 1)[-1] == str(port)
                        and parts[3].upper() == "LISTENING"):
                    try:
                        return int(parts[4])
                    except ValueError:
                        continue
        else:
            out = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def _force_kill(pid: int) -> None:
    """强杀一个 pid(及其子树)。best-effort。"""
    if not pid or pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        pass


def _reclaim_port(port: int | None, *, exclude: int | None = None) -> int | None:
    """若 ``port`` 被某个(非 exclude 的)进程占着,杀掉它并等端口释放。
    返回被回收的 pid(或 None)。这是"重启没杀旧进程"的兜底:无论 pid 文件
    指向谁,真正霸占端口的僵尸都会在这里被清掉。"""
    if not port:
        return None
    owner = _port_listener_pid(int(port))
    if owner is None or owner == exclude:
        return None
    _force_kill(owner)
    for _ in range(25):  # 等最多 5s 让端口释放
        if _port_listener_pid(int(port)) in (None, exclude):
            break
        time.sleep(0.2)
    return owner


def start_daemon(
    *,
    host: str,
    port: int,
    config: str,
    no_auth: bool = False,
    wait_seconds: float = 180.0,
) -> DaemonStatus:
    """Spawn ``xmclaw serve`` detached, wait for /health, write pid+meta.

    Raises RuntimeError if the daemon is already running or fails to
    become healthy within ``wait_seconds``.

    2026-05-26: bumped default from 60s → 180s. Real-data finding: a
    fully-wired install with persona files + memory v2 + skill loader
    + perception bus + evolution observers takes ~90-100s to reach the
    "healthy" milestone (39s lifespan + 30s memory backfill + 20s
    persona render + skill warmup). The previous 60s window made the
    CLI report "did not answer" while the daemon was still booting
    successfully — operator would re-run ``xmclaw start``, stacking
    multiple half-booted daemons fighting for port 8766.
    """
    status = read_status()
    # 2026-06-08: 权威判断用「端口上是否有健康 daemon」,不再只看 daemon.pid 是否
    # 存活(它可能指向一个不占端口的僵尸/错 pid → 旧逻辑要么误判"已在跑"挡住
    # start,要么误判"没在跑"起出第二个 daemon 与旧的并存)。
    if _http_healthy(host, port):
        owner = _port_listener_pid(port)
        raise RuntimeError(
            f"daemon already running (pid={owner or status.pid}, "
            f"http://{host}:{port})"
        )
    # 端口上没有健康 daemon:清掉可能失准的 pid/meta,并回收任何「绑着端口但不
    # 健康」的旧 daemon(否则新进程绑不上端口 → 又一个僵尸)。这就是"重启没杀
    # 旧进程"的根治:无论 pid 文件指向谁,真正霸占端口的进程都会被清掉。
    if status.state in ("running", "stale"):
        _clear_files()
    reclaimed = _reclaim_port(port)
    if reclaimed is not None:
        try:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "daemon.start reclaimed_orphan pid=%s port=%s before spawn",
                reclaimed, port,
            )
        except Exception:  # noqa: BLE001
            pass

    pid_path = default_pid_path()
    log_path = default_log_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    python_exe = _resolve_python_executable()
    cmd = [
        python_exe, "-m", "xmclaw.cli.main", "serve",
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
    graceful = False
    while time.time() < deadline:
        if not _process_alive(pid):
            graceful = True
            break
        time.sleep(0.2)

    if not graceful:
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
        time.sleep(0.2)

    # 2026-06-08: 兜底——**所有路径都跑**(优雅杀成功的早退分支以前漏了这步)。
    # daemon.pid 可能失准(指向已死/错的 pid),真正占着端口的旧 daemon 还活着。
    # 按端口属主再回收一次,确保 stop 真的把端口腾出来。
    reclaimed = _reclaim_port(status.port)
    if reclaimed is not None:
        try:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "daemon.stop reclaimed_orphan pid=%s port=%s "
                "(pid file pointed at %s) — restart had been leaving this alive",
                reclaimed, status.port, pid,
            )
        except Exception:  # noqa: BLE001
            pass
    _clear_files()
    return read_status()
