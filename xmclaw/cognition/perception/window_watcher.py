"""ActiveWindowWatcher — pushes a percept whenever the foreground
window's title or process changes.

Cheap polling-based: no screenshot, no OCR, just a native API for
``GetForegroundWindow`` / ``GetWindowText`` (Windows) /
``NSWorkspace`` (macOS) / ``xdotool`` (Linux). We rely on
``pygetwindow`` as the cross-platform abstraction; if it's not
installed the watcher reports ``available()=False`` and stays dormant.

Privacy posture: we DO record window titles. If a title contains a
URL, password fragment, or otherwise sensitive substring, that lands
in the percept payload. Operators who don't want this should leave
the source disabled (it's off by default).

Salience heuristic:
  * fresh (just-now) window switch → 0.55
  * unchanged window → not pushed at all (no noise)
"""
from __future__ import annotations

import logging
from typing import Any

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception_bus import Percept

logger = logging.getLogger(__name__)


class ActiveWindowWatcher(PerceptionSource):
    """Polls the foreground window. Period default 5 s — fast enough
    for "user just switched apps" semantics, slow enough to not flood
    AttentionFilter."""

    def __init__(
        self,
        *,
        bus: Any | None = None,
        period_s: float = 5.0,
    ) -> None:
        super().__init__(bus=bus, period_s=period_s)
        self._last_title: str | None = None

    @property
    def name(self) -> str:
        return "window"

    def available(self) -> bool:
        try:
            import pygetwindow  # noqa: F401
        except ImportError:
            return False
        except Exception:  # noqa: BLE001
            # Some platforms raise unrelated errors at import time
            # (e.g. macOS without screen-recording permission). Treat
            # as unavailable — the source will sit dormant.
            return False
        return True

    async def poll_once(self) -> list[Percept]:
        try:
            import pygetwindow as gw
        except ImportError:
            return []
        try:
            win = gw.getActiveWindow()
        except Exception as exc:  # noqa: BLE001
            logger.debug("window_watcher.get_active_failed err=%s", exc)
            return []
        if win is None:
            return []
        title = getattr(win, "title", None) or ""
        title = title.strip()
        if not title or title == self._last_title:
            return []
        self._last_title = title
        # Salience moderate — "user switched windows" is interesting
        # but not urgent. AttentionFilter can boost when goal context
        # matches the window title.
        return [
            self._make_percept(
                source="window",
                kind="active_window_changed",
                payload={"title": title[:300]},
                suggested_salience=0.55,
            ),
        ]


__all__ = ["ActiveWindowWatcher"]
