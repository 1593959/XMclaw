"""Multi-modal perception sources (R4, 2026-05-10).

Each source is an async loop that pushes :class:`Percept` envelopes
onto the daemon's :class:`PerceptionBus`. Sources share the
:class:`PerceptionSource` ABC so the registry / lifespan can drive
them uniformly.

Today's sources (all opt-in via config; off by default):
  * :class:`ScreenWatcher` — periodic screen snapshot (mss + optional
    OCR via paddleocr/tesseract). Pushes a screen percept summarising
    text content. Privacy posture: never stored to disk.
  * :class:`ActiveWindowWatcher` — polls the foreground window; pushes
    when title/process changes. Pure native API; no screenshot.
  * :class:`ClipboardWatcher` — polls clipboard; pushes when content
    changes. URLs / code blocks get tagged so AttentionFilter can
    boost them.
  * :class:`CalendarWatcher` — reads an .ics file or Google Calendar
    if configured; pushes upcoming events.

All sources gracefully degrade when their optional deps are missing —
the daemon still boots without ``mss``/``paddleocr``/``pygetwindow``/
``pyperclip``/``icalendar`` installed; the source's ``start()`` just
becomes a no-op. This is **important** because the runtime is shared
across Windows / macOS / Linux + headless / Docker; we can't assume
any platform module is available.

Wiring entry point: :func:`build_perception_sources_from_config`
returns a list of started sources. The lifespan owns their stop.
"""
from __future__ import annotations

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception.calendar_watcher import CalendarWatcher
from xmclaw.cognition.perception.clipboard_watcher import (
    ClipboardWatcher,
)
from xmclaw.cognition.perception.factory import (
    build_perception_sources_from_config,
)
from xmclaw.cognition.perception.screen_watcher import ScreenWatcher
from xmclaw.cognition.perception.window_watcher import (
    ActiveWindowWatcher,
)

__all__ = [
    "ActiveWindowWatcher",
    "CalendarWatcher",
    "ClipboardWatcher",
    "PerceptionSource",
    "ScreenWatcher",
    "build_perception_sources_from_config",
]
