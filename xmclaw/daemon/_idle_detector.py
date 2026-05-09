"""Cross-platform OS idle detection — Sprint 3 #3.

Letta proved (TerminalBench 2.0 +36.8% relative) that splitting the
foreground / sleep-time agents — only the sleep agent writes memory,
only the foreground reads — both improves user-felt latency AND
raises evolution quality. EvoMap's ``idleScheduler.js`` (MIT-licensed
JS reference) is the cross-platform idle-detect we port to Python
here.

Design notes
------------
- Three concrete detectors (Windows / macOS / Linux) + a no-op
  fallback that pretends every tick is idle. The fallback matches the
  current cron-firing behaviour, so behaviour on unsupported platforms
  is *identical* to today's.
- pyobjc is heavy. We do **not** add it as a hard dep; the macOS
  detector lazy-imports ``objc`` and degrades to the always-idle
  fallback when pyobjc is not installed. Document the fallback
  loudly via a one-line WARN log so an unexplained "always idle"
  doesn't surprise a user later.
- Each detector returns idle seconds as ``float``. ``-1.0`` means
  "I genuinely could not measure" — the SleepWorker treats that
  exactly like the always-idle fallback (every tick passes).
- Detectors are pure (no asyncio, no bus); the SleepWorker is the
  only async surface. Keeps the unit tests fast and lets the
  detectors be reused in CLI tooling later.

The two reference points cited in the task (Iron Rules in
``docs/EVOLUTION_HONEST_STATE.md`` and the Letta foreground/sleep
split) are the WHY. This file is the HOW.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class IdleDetector(ABC):
    """Abstract base for OS idle detection.

    Concrete impls return idle seconds as a float. ``-1.0`` (or any
    negative value) is the unmeasurable sentinel the SleepWorker
    treats as "always idle".
    """

    @abstractmethod
    def idle_seconds(self) -> float:
        """How long since the last user input event (mouse/keyboard).

        Negative return value = unmeasurable. The SleepWorker treats
        that the same as the always-idle fallback so behaviour on
        unsupported platforms matches the legacy cron firing.
        """

    @property
    def name(self) -> str:
        """Stable identifier used in logs / events."""
        return self.__class__.__name__


class _WindowsIdleDetector(IdleDetector):
    """Windows: ``GetLastInputInfo`` via ctypes (stdlib only).

    Pure stdlib — no new pip dep. ``GetLastInputInfo`` returns ticks
    since system boot of the last input event; ``GetTickCount`` gives
    the current tick count. Their difference (in ms) divided by 1000
    is the idle interval.

    Edge cases:
    - 32-bit tick count wraps every ~49 days. We mod-correct so the
      wrap is invisible.
    - ``windll`` is only present on Windows. Construction is gated by
      ``sys.platform == "win32"`` so importing this module on Linux/
      macOS doesn't trip an AttributeError.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsIdleDetector only runs on win32")
        # Lazy-imported so non-Windows hosts don't even touch ctypes.
        import ctypes  # noqa: PLC0415 — platform-gated import

        from ctypes import wintypes  # noqa: PLC0415

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._lii_cls = _LASTINPUTINFO
        # ``windll`` only exists on Windows; the platform gate above
        # guarantees we're on win32 so we silence the attr-defined
        # check broadly (mypy on Linux can't see win32-only attrs).
        self._user32 = getattr(ctypes, "windll").user32  # noqa: B009
        self._kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009

    def idle_seconds(self) -> float:
        try:
            lii = self._lii_cls()
            lii.cbSize = self._ctypes.sizeof(self._lii_cls)
            if not self._user32.GetLastInputInfo(self._ctypes.byref(lii)):
                return -1.0
            now = self._kernel32.GetTickCount()
            # 32-bit wrap correction.
            elapsed_ms = (now - lii.dwTime) & 0xFFFFFFFF
            return float(elapsed_ms) / 1000.0
        except Exception as exc:  # noqa: BLE001 — never crash the worker
            _log.warning("idle.windows_failed err=%s", exc)
            return -1.0


class _MacIdleDetector(IdleDetector):
    """macOS: ``IOHIDIdleTime`` via ``objc.loadBundle``.

    Soft-dep on pyobjc — that bundle is heavy and we don't want to
    drag it into every install just for idle detection. When pyobjc
    is unavailable, ``__init__`` raises ``RuntimeError`` and the
    factory falls back to the always-idle path with a one-line WARN
    log so the user can see what happened.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("MacIdleDetector only runs on darwin")
        try:
            import objc  # type: ignore[import-not-found]  # noqa: PLC0415
            from Foundation import NSBundle  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "pyobjc not installed — pip install 'xmclaw[idle-macos]' "
                "for native macOS idle detection",
            ) from exc

        # Bind IOHIDGetLastInputTime equivalent. The textbook idiom is
        # to walk the IOKit registry for the IOHIDSystem entry and
        # read its HIDIdleTime property in nanoseconds.
        iokit = NSBundle.bundleWithIdentifier_("com.apple.framework.IOKit")
        if iokit is None:
            raise RuntimeError("IOKit framework not found")
        functions = [
            ("IOServiceGetMatchingService", b"II@"),
            ("IOServiceMatching", b"@*"),
            ("IORegistryEntryCreateCFProperty", b"@I@@I"),
            ("IOObjectRelease", b"II"),
        ]
        objc.loadBundleFunctions(iokit, globals(), functions)
        self._iokit = iokit

    def idle_seconds(self) -> float:
        try:
            # Importing here (not at construction) keeps the failure
            # path inside idle_seconds when something goes sideways
            # mid-run — we still log + degrade rather than crashing
            # the worker.
            service = IOServiceGetMatchingService(  # type: ignore[name-defined]  # noqa: F821
                0, IOServiceMatching(b"IOHIDSystem"),  # type: ignore[name-defined]  # noqa: F821
            )
            if not service:
                return -1.0
            try:
                ns = IORegistryEntryCreateCFProperty(  # type: ignore[name-defined]  # noqa: F821
                    service, "HIDIdleTime", None, 0,
                )
                if ns is None:
                    return -1.0
                # Property is in nanoseconds.
                return float(ns) / 1e9
            finally:
                IOObjectRelease(service)  # type: ignore[name-defined]  # noqa: F821
        except Exception as exc:  # noqa: BLE001
            _log.warning("idle.macos_failed err=%s", exc)
            return -1.0


class _LinuxIdleDetector(IdleDetector):
    """Linux: ``xprintidle`` then ``loginctl IdleHint`` then fallback.

    Two paths because no single tool is universally available:
    - ``xprintidle`` is the X11-native idle reporter; correct on any
      desktop with an X server. Returns ms-since-last-input.
    - ``loginctl show-session $XDG_SESSION_ID --property=IdleHint``
      is the systemd path; reports a binary "idle yes/no" rather
      than seconds, so we map yes→very-idle (returns long_threshold
      sentinel) and no→0.0.

    If neither tool is available the constructor raises and the
    factory falls back to always-idle.
    """

    _xprintidle: str | None
    _loginctl: str | None
    _session: str
    _long_threshold_hint: float

    def __init__(self, *, long_threshold_hint: float = 1800.0) -> None:
        if sys.platform == "win32":
            raise RuntimeError("LinuxIdleDetector does not run on win32")
        self._xprintidle = shutil.which("xprintidle")
        self._loginctl = shutil.which("loginctl")
        # Pre-resolve XDG_SESSION_ID once; daemon process is long-lived
        # so the value is stable.
        self._session = os.environ.get("XDG_SESSION_ID", "")
        if not self._xprintidle and not (self._loginctl and self._session):
            raise RuntimeError(
                "no Linux idle source available — install xprintidle or "
                "run inside a systemd-logind session",
            )
        # When loginctl returns IdleHint=yes we synthesize this many
        # seconds so the SleepWorker's long-level threshold trips. The
        # caller can override at construction; the default matches the
        # default long_threshold_s.
        self._long_threshold_hint = float(long_threshold_hint)

    def idle_seconds(self) -> float:
        # xprintidle wins when present — the second-tier signal is
        # binary and noisier.
        if self._xprintidle:
            try:
                out = subprocess.run(
                    [self._xprintidle],
                    capture_output=True, text=True, timeout=2.0,
                    check=False,
                )
                if out.returncode == 0:
                    return float(out.stdout.strip()) / 1000.0
            except Exception as exc:  # noqa: BLE001
                _log.warning("idle.xprintidle_failed err=%s", exc)
        if self._loginctl and self._session:
            try:
                out = subprocess.run(
                    [
                        self._loginctl,
                        "show-session", self._session,
                        "--property=IdleHint",
                    ],
                    capture_output=True, text=True, timeout=2.0,
                    check=False,
                )
                if out.returncode == 0:
                    line = out.stdout.strip()
                    if "=" in line:
                        _, val = line.split("=", 1)
                        if val.strip().lower() == "yes":
                            return self._long_threshold_hint
                        return 0.0
            except Exception as exc:  # noqa: BLE001
                _log.warning("idle.loginctl_failed err=%s", exc)
        return -1.0


class _AlwaysIdleDetector(IdleDetector):
    """Fallback: always reports a huge idle interval.

    Matches the legacy cron-firing behaviour — the SleepWorker
    crosses both thresholds on every tick — so daemons on
    unsupported platforms (BSD without xprintidle, headless Linux
    without systemd, macOS without pyobjc) keep working exactly as
    they do today. Logged at boot so the user knows why.
    """

    SENTINEL = 86400.0  # 24h — comfortably past long_threshold_s

    def __init__(self, *, reason: str = "no detector available") -> None:
        self._reason = reason

    @property
    def reason(self) -> str:
        return self._reason

    def idle_seconds(self) -> float:
        return self.SENTINEL


def build_idle_detector() -> IdleDetector:
    """Auto-detect the best detector for this host.

    Order of preference: native (Windows / macOS / Linux) → fallback.
    Each native impl gets one chance; if construction raises we log
    the reason and fall through to the next tier.
    """
    if sys.platform == "win32":
        try:
            return _WindowsIdleDetector()
        except Exception as exc:  # noqa: BLE001 — fall through, not crash
            _log.warning("idle.windows_unavailable reason=%s", exc)
            return _AlwaysIdleDetector(reason=f"windows: {exc}")
    if sys.platform == "darwin":
        try:
            return _MacIdleDetector()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "idle.macos_unavailable reason=%s — install pyobjc to "
                "enable native idle detection",
                exc,
            )
            return _AlwaysIdleDetector(reason=f"macos: {exc}")
    # Treat anything else as Linux-ish.
    try:
        return _LinuxIdleDetector()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "idle.linux_unavailable reason=%s — install xprintidle for "
            "native idle detection",
            exc,
        )
        return _AlwaysIdleDetector(reason=f"linux: {exc}")


__all__ = [
    "IdleDetector",
    "_AlwaysIdleDetector",
    "_LinuxIdleDetector",
    "_MacIdleDetector",
    "_WindowsIdleDetector",
    "build_idle_detector",
]
