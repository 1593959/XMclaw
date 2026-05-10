"""Factory for the multi-modal perception sources.

Reads ``cfg["cognition"]["perception"]`` and returns a list of
sources to ``start()``. The list may be empty when nothing is
configured / nothing is available — that's a normal "no extra
perception" config and not an error.

Config shape (2026-05-10 default flip — opt-out instead of opt-in):
    cognition:
      perception:
        screen:
          enabled: true         # default ON; auto-skipped when mss missing
          period_s: 30
          ocr_enabled: false    # OCR still off (heavy + privacy)
          ocr_max_chars: 2000
        window:
          enabled: true
          period_s: 5.0
        clipboard:
          enabled: true         # WARNING: reads clipboard contents
          period_s: 3.0
          preview_chars: 500
        calendar:
          enabled: true
          ics_path: ""          # still skipped without a real path
          window_minutes: 30
          period_s: 60

Operators who want to dial back set ``enabled: false`` per source.
The watchers without their optional dep installed are a silent no-op
either way (``available()`` returns False → factory drops them).
"""
from __future__ import annotations

import logging
from typing import Any

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception.calendar_watcher import CalendarWatcher
from xmclaw.cognition.perception.clipboard_watcher import (
    ClipboardWatcher,
)
from xmclaw.cognition.perception.screen_watcher import ScreenWatcher
from xmclaw.cognition.perception.window_watcher import (
    ActiveWindowWatcher,
)

logger = logging.getLogger(__name__)


def build_perception_sources_from_config(
    cfg: dict[str, Any] | None,
    *,
    bus: Any | None,
) -> list[PerceptionSource]:
    """Build sources per cfg. Each source's ``available()`` is
    consulted post-construction; unavailable ones are dropped here
    so the lifespan never tries to start them."""
    # 2026-05-10 default flip: even when ``cfg`` is None or doesn't
    # have a ``perception`` block, we still try every default-on
    # watcher. ``available()`` filters out the ones whose deps aren't
    # installed, so a clean install with zero perception cfg ends up
    # with whatever the host platform supports.
    perc: dict[str, Any] = {}
    if cfg:
        perc = ((cfg.get("cognition") or {}).get("perception") or {})

    sources: list[PerceptionSource] = []

    screen_cfg = perc.get("screen") or {}
    if screen_cfg.get("enabled", True):
        try:
            sources.append(ScreenWatcher(
                bus=bus,
                period_s=float(screen_cfg.get("period_s", 30.0)),
                ocr_enabled=bool(screen_cfg.get("ocr_enabled", False)),
                ocr_max_chars=int(
                    screen_cfg.get("ocr_max_chars", 2000),
                ),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("perception.screen.build_failed err=%s", exc)

    window_cfg = perc.get("window") or {}
    if window_cfg.get("enabled", True):
        try:
            sources.append(ActiveWindowWatcher(
                bus=bus,
                period_s=float(window_cfg.get("period_s", 5.0)),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("perception.window.build_failed err=%s", exc)

    clip_cfg = perc.get("clipboard") or {}
    if clip_cfg.get("enabled", True):
        try:
            sources.append(ClipboardWatcher(
                bus=bus,
                period_s=float(clip_cfg.get("period_s", 3.0)),
                preview_chars=int(clip_cfg.get("preview_chars", 500)),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("perception.clipboard.build_failed err=%s", exc)

    cal_cfg = perc.get("calendar") or {}
    if cal_cfg.get("enabled", True):
        ics_path = cal_cfg.get("ics_path")
        if ics_path:
            try:
                sources.append(CalendarWatcher(
                    bus=bus,
                    ics_path=ics_path,
                    window_minutes=int(cal_cfg.get("window_minutes", 30)),
                    period_s=float(cal_cfg.get("period_s", 60.0)),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "perception.calendar.build_failed err=%s", exc,
                )
        else:
            logger.warning(
                "perception.calendar.enabled_but_no_ics_path — skipping",
            )

    # Filter unavailable. Each source's ``available`` check is
    # guaranteed not to raise (per the ABC contract).
    return [s for s in sources if s.available()]


__all__ = ["build_perception_sources_from_config"]
