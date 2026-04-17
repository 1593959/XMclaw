"""Daemon lifecycle management."""
import os
import sys
import subprocess
from pathlib import Path
from xmclaw.utils.paths import BASE_DIR

PID_FILE = BASE_DIR / "daemon" / "pid"


def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(1, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ValueError):
        return False


def start_daemon() -> int:
    if is_running():
        print("Daemon already running.")
        return 1

    if sys.platform == "win32":
        proc = subprocess.Popen(
            [sys.executable, "-m", "xmclaw.daemon.server"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"Daemon started with PID {proc.pid}")
        return 0
    else:
        print("Daemon start on non-Windows not yet implemented.")
        return 1


def stop_daemon() -> int:
    if not is_running():
        print("Daemon not running.")
        return 1
    try:
        pid = int(PID_FILE.read_text().strip())
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 1
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 0)
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 9)
        PID_FILE.unlink()
        print(f"Daemon stopped (PID {pid})")
        return 0
    except Exception as e:
        print(f"Failed to stop daemon: {e}")
        return 1


def daemon_status() -> int:
    if is_running():
        pid = int(PID_FILE.read_text().strip())
        print(f"Daemon is running (PID {pid})")
        return 0
    else:
        print("Daemon is not running.")
        return 1
