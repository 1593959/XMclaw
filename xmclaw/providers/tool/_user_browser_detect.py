"""Detect the user's installed Chrome-family browser + profile dir.

Used by ``browser.BrowserTools`` to wire the
``browser_use_my_browser`` tool — the third path alongside the
existing headless (background) and visible-clean-profile (demo)
options. The goal: get the agent operating in the user's REAL
Chrome session (their cookies / logins / extensions / bookmarks)
so login-walled sites just work, while the user watches.

Three resolution tiers, by preference:

1. **CDP attach** — if the user already has Chrome running with
   ``--remote-debugging-port=9222`` (rare unless they manually set
   it up). We just attach via Playwright's
   ``connect_over_cdp(http://127.0.0.1:9222)``.

2. **Launch user's real profile** — if Chrome is NOT currently
   running on the target profile, spawn the system Chrome.exe
   pointed at the user's real ``User Data`` dir
   (``%LOCALAPPDATA%\\Google\\Chrome\\User Data`` on Windows).
   This is the canonical "agent uses my real browser" path —
   Playwright's ``launch_persistent_context(channel='chrome',
   user_data_dir=<user real>)`` gets the full profile state.

3. **Side-profile fallback** — if the user's main Chrome is
   running and we can't grab the lock, the existing
   ``persistent_profile=True`` machinery (which uses a side dir
   under ``~/.xmclaw/v2/browser_profiles/<name>/user-data``) is
   the fallback. The user logs in once there and that login
   persists thereafter; doesn't touch their daily Chrome session.

This module is **pure** — no Playwright, no async, no daemon
imports. Just stdlib path / registry / socket probes so it stays
trivially unit-testable.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserInstall:
    """One detected Chrome-family browser on the system."""
    name: str               # "chrome" | "edge" | "brave"
    exe_path: Path          # absolute path to the launcher binary
    user_data_dir: Path     # absolute path to the User Data root
    playwright_channel: str  # "chrome" | "msedge" | "chromium" (Brave uses chromium)


# ─── Detection: where are the binaries? ────────────────────────────

_WINDOWS_CHROME_CANDIDATES = [
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]
_WINDOWS_EDGE_CANDIDATES = [
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
]
_WINDOWS_BRAVE_CANDIDATES = [
    r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
]

_MACOS_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
_MACOS_EDGE_CANDIDATES = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
_MACOS_BRAVE_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

_LINUX_CHROME_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]
_LINUX_EDGE_CANDIDATES = [
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]
_LINUX_BRAVE_CANDIDATES = [
    "/usr/bin/brave-browser",
    "/snap/bin/brave",
]


def _expand_first_existing(candidates: list[str]) -> Optional[Path]:
    for c in candidates:
        expanded = os.path.expandvars(c)
        p = Path(expanded)
        if p.is_file():
            return p
    return None


def _user_data_dir(name: str) -> Optional[Path]:
    """Return the standard User Data dir for the given browser.

    Locations are well-known and stable across versions:

    *Windows*
      Chrome: ``%LOCALAPPDATA%\\Google\\Chrome\\User Data``
      Edge:   ``%LOCALAPPDATA%\\Microsoft\\Edge\\User Data``
      Brave:  ``%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data``

    *macOS*
      Chrome: ``~/Library/Application Support/Google/Chrome``
      Edge:   ``~/Library/Application Support/Microsoft Edge``
      Brave:  ``~/Library/Application Support/BraveSoftware/Brave-Browser``

    *Linux*
      Chrome: ``~/.config/google-chrome``
      Edge:   ``~/.config/microsoft-edge``
      Brave:  ``~/.config/BraveSoftware/Brave-Browser``
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        base = Path(local)
        return {
            "chrome": base / "Google" / "Chrome" / "User Data",
            "edge": base / "Microsoft" / "Edge" / "User Data",
            "brave": base / "BraveSoftware" / "Brave-Browser" / "User Data",
        }.get(name)
    if sys.platform == "darwin":
        home = Path.home()
        return {
            "chrome": home / "Library" / "Application Support" / "Google" / "Chrome",
            "edge": home / "Library" / "Application Support" / "Microsoft Edge",
            "brave": (
                home / "Library" / "Application Support"
                / "BraveSoftware" / "Brave-Browser"
            ),
        }.get(name)
    # Linux + others
    home = Path.home()
    return {
        "chrome": home / ".config" / "google-chrome",
        "edge": home / ".config" / "microsoft-edge",
        "brave": home / ".config" / "BraveSoftware" / "Brave-Browser",
    }.get(name)


def detect_browsers() -> list[BrowserInstall]:
    """Find every installed Chrome-family browser. Empty if none.

    Order: chrome → edge → brave (preference for the most common).
    Callers can pick the first hit, or filter by name.
    """
    if sys.platform == "win32":
        chrome = _expand_first_existing(_WINDOWS_CHROME_CANDIDATES)
        edge = _expand_first_existing(_WINDOWS_EDGE_CANDIDATES)
        brave = _expand_first_existing(_WINDOWS_BRAVE_CANDIDATES)
    elif sys.platform == "darwin":
        chrome = _expand_first_existing(_MACOS_CHROME_CANDIDATES)
        edge = _expand_first_existing(_MACOS_EDGE_CANDIDATES)
        brave = _expand_first_existing(_MACOS_BRAVE_CANDIDATES)
    else:
        chrome = _expand_first_existing(_LINUX_CHROME_CANDIDATES)
        edge = _expand_first_existing(_LINUX_EDGE_CANDIDATES)
        brave = _expand_first_existing(_LINUX_BRAVE_CANDIDATES)

    out: list[BrowserInstall] = []
    if chrome:
        udd = _user_data_dir("chrome")
        if udd:
            out.append(BrowserInstall(
                name="chrome", exe_path=chrome,
                user_data_dir=udd, playwright_channel="chrome",
            ))
    if edge:
        udd = _user_data_dir("edge")
        if udd:
            out.append(BrowserInstall(
                name="edge", exe_path=edge,
                user_data_dir=udd, playwright_channel="msedge",
            ))
    if brave:
        udd = _user_data_dir("brave")
        if udd:
            out.append(BrowserInstall(
                name="brave", exe_path=brave,
                user_data_dir=udd,
                # Playwright has no "brave" channel; Brave IS Chromium
                # under the hood and accepts the chromium driver. The
                # executable_path override is what makes us hit Brave
                # rather than bundled Chromium.
                playwright_channel="chromium",
            ))
    return out


def pick_browser(name: str | None = None) -> Optional[BrowserInstall]:
    """Pick one detected browser. ``name`` matches by ``name`` field;
    if None, returns the first install (chrome > edge > brave)."""
    installs = detect_browsers()
    if not installs:
        return None
    if name is None or name == "auto":
        return installs[0]
    for inst in installs:
        if inst.name == name:
            return inst
    return None


# ─── Runtime probes ────────────────────────────────────────────────


def probe_cdp_endpoint(port: int = 9222, timeout: float = 0.5) -> Optional[str]:
    """If a Chromium-family browser is listening on ``port`` with the
    DevTools protocol enabled, return its HTTP CDP base URL. Else
    ``None``.

    We hit ``/json/version`` which is the standard CDP discovery
    endpoint. Doesn't open the websocket — just confirms it's reachable
    and JSON-shaped (rejects random local services on the same port).
    """
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    # Real CDP responds with a dict containing "webSocketDebuggerUrl"
    # and "Browser". Anything else is some other service on the port.
    if not isinstance(data, dict) or "webSocketDebuggerUrl" not in data:
        return None
    return f"http://127.0.0.1:{port}"


def find_free_port(start: int = 9222, end: int = 9322) -> Optional[int]:
    """Find a TCP port in ``[start, end]`` that nothing is bound to.
    Used when we need to launch a fresh Chrome with --remote-debugging-port=
    and 9222 is already taken (or is taken by something that isn't CDP)."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def is_user_data_dir_locked(user_data_dir: Path) -> bool:
    """Heuristic check: is a Chrome-family process already using
    ``user_data_dir``? Chromium maintains a ``SingletonLock`` symlink
    (Unix) or ``lockfile`` (Windows) inside the User Data dir when it
    has the profile open. If those exist AND the referenced PID is
    alive, the directory is locked and a second
    ``launch_persistent_context`` against it will hang or fail.

    Returns ``True`` if the dir appears locked, ``False`` if free or
    if we can't tell (caller treats unknown as "try and see").
    """
    if not user_data_dir.exists():
        return False
    # Windows: Chrome drops "lockfile" in User Data (sometimes empty,
    # sometimes contains "ip pid"). Existence alone is the conservative
    # signal.
    if sys.platform == "win32":
        lockfile = user_data_dir / "lockfile"
        if lockfile.exists():
            return True
        # Newer Chrome also drops "Singleton*" files at the top level.
        for marker in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            if (user_data_dir / marker).exists():
                return True
        return False
    # Unix-likes: SingletonLock is a symlink to "<host>-<pid>".
    sl = user_data_dir / "SingletonLock"
    if sl.is_symlink():
        try:
            target = os.readlink(sl)
            # Format: "<hostname>-<pid>"
            pid_str = target.rsplit("-", 1)[-1]
            pid = int(pid_str)
        except (OSError, ValueError):
            return True  # symlink exists but unreadable → assume locked
        return _pid_alive(pid)
    return False


def _pid_alive(pid: int) -> bool:
    """True iff a process with the given PID is alive. Cross-platform,
    no third-party deps."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # ``os.kill(pid, 0)`` raises PermissionError for foreign
        # processes on Windows even when they're alive, which would
        # false-positive. Use OpenProcess via ctypes — clean signal.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


__all__ = [
    "BrowserInstall",
    "detect_browsers",
    "pick_browser",
    "probe_cdp_endpoint",
    "find_free_port",
    "is_user_data_dir_locked",
]
