"""CalendarWatcher — pushes upcoming-event percepts.

Source: a local ICS file (RFC 5545) at ``cfg.cognition.perception.
calendar.ics_path``. Extending to live calendar APIs (Google /
Outlook / Apple) is a follow-up — they all add OAuth + refresh
state which is out of scope for the foundation.

Cadence: 60 s default. Each tick reads the file (cheap), filters
events whose start is within ``window_minutes`` (default 30 min),
and pushes a percept per event. Already-pushed events (matched by
``UID`` + ``DTSTART``) are not re-pushed within the same daemon
process.

Salience heuristic:
  * <5 min away → 0.85 (urgent)
  * 5-15 min     → 0.7
  * 15-30 min    → 0.55
  * other        → not pushed (filter cuts it)

Privacy posture: reads only the file you point us at. No network.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception_bus import Percept

logger = logging.getLogger(__name__)


def _salience_for_minutes(mins: float) -> float | None:
    """Returns suggested salience or None if outside the alert window."""
    if mins < 0:
        return None
    if mins < 5:
        return 0.85
    if mins < 15:
        return 0.7
    if mins < 30:
        return 0.55
    return None


class CalendarWatcher(PerceptionSource):
    """Polls a single ICS file. Pushes upcoming events.

    Args:
        ics_path: path to the .ics file. Required — the watcher
            doesn't look for OAuth or live APIs.
        window_minutes: how far ahead to alert. Default 30.
        period_s: poll cadence. Default 60.
    """

    def __init__(
        self,
        *,
        bus: Any | None = None,
        ics_path: str | Path | None = None,
        window_minutes: int = 30,
        period_s: float = 60.0,
    ) -> None:
        super().__init__(bus=bus, period_s=period_s)
        self._ics_path = Path(ics_path) if ics_path else None
        self._window_seconds = max(60, int(window_minutes) * 60)
        self._pushed: set[str] = set()

    @property
    def name(self) -> str:
        return "calendar"

    def available(self) -> bool:
        if self._ics_path is None or not self._ics_path.exists():
            return False
        try:
            import icalendar  # noqa: F401
        except ImportError:
            return False
        except Exception:  # noqa: BLE001
            return False
        return True

    async def poll_once(self) -> list[Percept]:
        if self._ics_path is None or not self._ics_path.exists():
            return []
        try:
            import icalendar
        except ImportError:
            return []

        try:
            raw = self._ics_path.read_bytes()
            cal = icalendar.Calendar.from_ical(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calendar_watcher.parse_failed err=%s", exc)
            return []

        now = time.time()
        out: list[Percept] = []
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            try:
                summary = str(component.get("SUMMARY", "")).strip()
                uid = str(component.get("UID", "")).strip()
                dtstart_obj = component.get("DTSTART")
                if dtstart_obj is None:
                    continue
                dtstart = dtstart_obj.dt
                # Coerce to unix ts. icalendar yields datetime/date.
                if hasattr(dtstart, "timestamp"):
                    start_ts = float(dtstart.timestamp())
                else:
                    # date-only (all-day) — skip; not actionable as
                    # "imminent meeting".
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "calendar_watcher.component_skip err=%s", exc,
                )
                continue

            mins_to_start = (start_ts - now) / 60.0
            salience = _salience_for_minutes(mins_to_start)
            if salience is None:
                continue
            dedup_key = f"{uid}::{int(start_ts)}"
            if dedup_key in self._pushed:
                continue
            self._pushed.add(dedup_key)
            out.append(self._make_percept(
                source="calendar",
                kind="upcoming_event",
                payload={
                    "summary": summary[:300],
                    "uid": uid,
                    "start_ts": int(start_ts),
                    "minutes_to_start": round(mins_to_start, 1),
                },
                suggested_salience=salience,
            ))

        # GC the dedup set periodically — drop entries whose start
        # is more than 1 day in the past.
        cutoff = now - 86400.0
        self._pushed = {
            k for k in self._pushed
            if int(k.rsplit("::", 1)[-1]) >= cutoff
        }

        return out


__all__ = ["CalendarWatcher"]
