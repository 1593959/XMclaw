"""Desktop application entry point — browser + system tray."""
import os
import sys
import subprocess
import time
import webbrowser
import urllib.request
from pathlib import Path

# Project root (parent of xmclaw/ package)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"
_DAEMON_LOG = _LOGS_DIR / "daemon_desktop.log"

DAEMON_MODULE = "xmclaw.daemon.server"


def _get_daemon_url() -> str:
    """Read gateway URL from config, fallback to default."""
    try:
        import json
        config_path = _PROJECT_ROOT / "daemon" / "config.json"
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            gw = data.get("gateway", {})
            host = gw.get("host", "127.0.0.1")
            port = gw.get("port", 8765)
            return f"http://{host}:{port}"
    except Exception:
        pass
    return "http://127.0.0.1:8765"


def _is_daemon_running(url: str) -> bool:
    """Check if daemon is responding."""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2):
            return True
    except Exception:
        return False


def _wait_for_daemon(url: str, timeout: int = 15, interval: float = 0.5) -> bool:
    """Poll daemon health endpoint until ready or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_daemon_running(url):
            return True
        time.sleep(interval)
    return False


def _start_daemon_subprocess() -> subprocess.Popen:
    """Start the daemon as a background subprocess."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(_DAEMON_LOG, "w", encoding="utf-8")

    startup_info = None
    creation_flags = 0
    if sys.platform == "win32":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    # Use the project's own .venv Python to ensure correct environment.
    venv_python = _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    process = subprocess.Popen(
        [python_exe, "-m", DAEMON_MODULE],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(_PROJECT_ROOT),
        startupinfo=startup_info,
        creationflags=creation_flags,
    )
    return process


def main():
    """Entry point for xmclaw-desktop command."""
    # Bypass proxy for localhost
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    url = _get_daemon_url()
    daemon_proc = None

    print(f"[XMclaw Desktop] Checking daemon at {url}...", file=sys.stderr)

    if not _is_daemon_running(url):
        print("[XMclaw Desktop] Daemon not running. Starting...", file=sys.stderr)
        daemon_proc = _start_daemon_subprocess()
        time.sleep(1)

        # Check if daemon crashed immediately
        if daemon_proc.poll() is not None:
            try:
                log = _DAEMON_LOG.read_text(encoding="utf-8")
            except Exception:
                log = "(no log)"
            print(f"[XMclaw Desktop] FATAL: Daemon exited immediately (code {daemon_proc.returncode})", file=sys.stderr)
            print(f"Log:\n{log[-800:]}", file=sys.stderr)
            sys.exit(1)

        print("[XMclaw Desktop] Waiting for daemon (up to 15s)...", file=sys.stderr)
        if not _wait_for_daemon(url):
            try:
                log = _DAEMON_LOG.read_text(encoding="utf-8")
            except Exception:
                log = "(no log)"
            print(f"[XMclaw Desktop] FATAL: Daemon did not respond within 15s.", file=sys.stderr)
            print(f"Log:\n{log[-800:]}", file=sys.stderr)
            sys.exit(1)

        print("[XMclaw Desktop] Daemon is ready!", file=sys.stderr)
    else:
        print("[XMclaw Desktop] Daemon already running.", file=sys.stderr)

    # Open browser
    print(f"[XMclaw Desktop] Opening browser: {url}", file=sys.stderr)
    webbrowser.open(url)

    # Run system tray (blocks until quit)
    from xmclaw.desktop.tray import TrayApp
    tray = TrayApp(url=url, daemon_process=daemon_proc)
    print("[XMclaw Desktop] System tray active. Right-click tray icon for menu.", file=sys.stderr)
    tray.run()

    print("[XMclaw Desktop] Exiting.", file=sys.stderr)


if __name__ == "__main__":
    main()
