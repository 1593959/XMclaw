"""Environment-aware proactive triggers (Sprint 2 Wave 5).

Extends :class:`ProactiveAgent` with triggers that read external
context: calendar (ICS file), stale projects (from
:class:`AutobiographicalMemory`), GitHub-style notification feeds.

Each trigger is independent — registering one doesn't pull in others'
dependencies.

CalendarReminderTrigger
=======================

Reads an ICS calendar file (your local export from Outlook / iCal /
Google Calendar) and fires when an event starts within
``look_ahead_s`` (default 300s = 5 min).

ICS parsing is intentionally minimal — only ``BEGIN:VEVENT`` blocks
with ``DTSTART`` + ``SUMMARY`` are parsed. Recurrence (``RRULE``)
not yet handled; daily / weekly recurring events get listed once at
their first occurrence then go silent. Fine for v1; v2 adds rrule
via the ``icalendar`` package.

StaleProjectTrigger
===================

Reads :class:`AutobiographicalMemory.projects()` and fires when the
user has a project with ``status='active'`` whose ``last_touch_ts``
is older than ``stale_days`` (default 7 days). Reminds the user of
work they intended to do but went silent on. Useful when paired with
the project-extraction in Wave 2.

Both triggers respect ProactiveAgent's cooldown + quiet hours.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xmclaw.cognition.proactive_agent import (
    ProactiveContext,
    ProactiveTrigger,
    TriggerProposal,
)


# ── ICS parsing helpers (no external dep) ─────────────────────────


@dataclass(frozen=True, slots=True)
class _IcsEvent:
    summary: str
    dtstart: float   # epoch seconds
    location: str | None = None
    uid: str | None = None


_DTSTART_RE = re.compile(
    r"DTSTART(?:;[^:]*)?:(\d{8}T?\d{0,6}Z?)",
)


def _parse_ics_dt(raw: str) -> float | None:
    """Parse ``20260513T093000Z`` / ``20260513T093000`` / ``20260513``.
    Returns epoch seconds (UTC) or None on failure."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        import datetime as _dt
        is_utc = raw.endswith("Z")
        if is_utc:
            raw = raw[:-1]
        if "T" in raw:
            d_part, t_part = raw.split("T", 1)
            year = int(d_part[0:4])
            mon = int(d_part[4:6])
            day = int(d_part[6:8])
            hour = int(t_part[0:2]) if len(t_part) >= 2 else 0
            minute = int(t_part[2:4]) if len(t_part) >= 4 else 0
            second = int(t_part[4:6]) if len(t_part) >= 6 else 0
        else:
            year = int(raw[0:4])
            mon = int(raw[4:6])
            day = int(raw[6:8])
            hour = minute = second = 0
        if is_utc:
            dt = _dt.datetime(
                year, mon, day, hour, minute, second,
                tzinfo=_dt.timezone.utc,
            )
        else:
            # Treat as local time.
            dt = _dt.datetime(year, mon, day, hour, minute, second)
            dt = dt.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def _parse_ics(text: str) -> list[_IcsEvent]:
    """Minimal ICS parser. Splits on VEVENT blocks; only requires
    SUMMARY + DTSTART. Ignores RRULE for v1.

    Handles line-folding (lines that start with a space continue the
    previous one). Tolerates either CRLF or LF newlines.
    """
    if not text:
        return []
    # Un-fold folded lines.
    text = re.sub(r"\r?\n[ \t]", "", text)
    lines = text.replace("\r\n", "\n").split("\n")
    events: list[_IcsEvent] = []
    cur: dict[str, Any] = {}
    inside = False
    for line in lines:
        s = line.strip()
        if s == "BEGIN:VEVENT":
            inside = True
            cur = {}
            continue
        if s == "END:VEVENT":
            inside = False
            summary = cur.get("summary") or ""
            dtraw = cur.get("dtstart") or ""
            dt = _parse_ics_dt(dtraw) if dtraw else None
            if summary and dt is not None:
                events.append(_IcsEvent(
                    summary=summary, dtstart=dt,
                    location=cur.get("location"),
                    uid=cur.get("uid"),
                ))
            continue
        if not inside:
            continue
        if ":" not in s:
            continue
        key, value = s.split(":", 1)
        # Strip parameters: "DTSTART;TZID=America/Los_Angeles"
        key_main = key.split(";")[0].upper()
        if key_main == "SUMMARY":
            cur["summary"] = value
        elif key_main == "DTSTART":
            cur["dtstart"] = value
        elif key_main == "LOCATION":
            cur["location"] = value
        elif key_main == "UID":
            cur["uid"] = value
    return events


# ── CalendarReminderTrigger ───────────────────────────────────────


class CalendarReminderTrigger(ProactiveTrigger):
    """Fires when an event is starting within ``look_ahead_s``.

    Reads the ICS file on each tick (cached for 60s so we don't
    re-parse every 30s of agent ticks). Per-event cooldown via UID
    keyed in ``self._announced`` so a single 9 AM meeting only
    surfaces once even though we'd evaluate "within 5 min" three
    times.
    """

    def __init__(
        self,
        *,
        ics_path: str | Path,
        look_ahead_s: float = 300.0,
        cooldown_s: float = 3600.0,
    ) -> None:
        self.name = "calendar_reminder"
        self.cooldown_s = float(cooldown_s)
        self._ics_path = Path(ics_path).expanduser()
        self._look_ahead = float(look_ahead_s)
        self._cache_events: list[_IcsEvent] = []
        self._cache_until: float = 0.0
        self._announced: dict[str, float] = {}  # uid → ts announced

    def _load_events(self, now: float) -> list[_IcsEvent]:
        if now < self._cache_until:
            return self._cache_events
        try:
            text = self._ics_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._cache_events = []
            self._cache_until = now + 60.0
            return []
        except Exception:  # noqa: BLE001
            self._cache_events = []
            self._cache_until = now + 60.0
            return []
        self._cache_events = _parse_ics(text)
        self._cache_until = now + 60.0
        return self._cache_events

    def _next_event(
        self, now: float,
    ) -> _IcsEvent | None:
        """Return the SOONEST event starting within look_ahead_s + not
        already announced. ``None`` if nothing matches."""
        events = self._load_events(now)
        upcoming: list[_IcsEvent] = []
        for e in events:
            delta = e.dtstart - now
            if 0 <= delta <= self._look_ahead:
                key = e.uid or f"{e.summary}@{e.dtstart}"
                if key in self._announced:
                    continue
                upcoming.append(e)
        if not upcoming:
            return None
        upcoming.sort(key=lambda e: e.dtstart)
        return upcoming[0]

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        return self._next_event(ctx.now) is not None

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        ev = self._next_event(ctx.now)
        if ev is None:
            return None
        mins = max(0, int((ev.dtstart - ctx.now) / 60))
        loc_note = (
            f"（地点：{ev.location}）"
            if ev.location else ""
        )
        msg = (
            f"📅 {mins} 分钟后有日程：**{ev.summary}**{loc_note}。"
            f"要不要我先帮你准备相关资料？"
        )
        # Mark announced so we don't re-fire.
        key = ev.uid or f"{ev.summary}@{ev.dtstart}"
        self._announced[key] = ctx.now
        return TriggerProposal(
            trigger_name=self.name,
            message=msg,
            urgency="normal" if mins > 2 else "high",
            payload={
                "summary": ev.summary,
                "minutes_until": mins,
                "location": ev.location,
            },
        )


# ── StaleProjectTrigger ───────────────────────────────────────────


class StaleProjectTrigger(ProactiveTrigger):
    """Read autobiographical_memory.projects() and remind the user of
    work they said they'd do but went silent on.

    Fires when a project's ``last_touch_ts`` is older than
    ``stale_days`` AND its ``status`` is ``"active"`` (or unset —
    treat as active). Cooldown 24h per name so we don't nag.
    """

    def __init__(
        self,
        *,
        stale_days: float = 7.0,
        cooldown_s: float = 24 * 3600,
    ) -> None:
        self.name = "stale_project"
        self.cooldown_s = float(cooldown_s)
        self._stale_s = float(stale_days) * 86400.0
        self._announced: dict[str, float] = {}

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        if ctx.agent_loop is None:
            return False
        autobio = getattr(ctx.agent_loop, "_autobio_memory", None)
        if autobio is None:
            return False
        try:
            projects = autobio.projects(limit=20)
        except Exception:  # noqa: BLE001
            return False
        cutoff = ctx.now - self._stale_s
        for p in projects:
            if p.last_touch_ts is None:
                continue
            if p.last_touch_ts >= cutoff:
                continue
            if p.status and p.status.lower() not in ("active", ""):
                continue
            if p.name in self._announced:
                continue
            return True
        return False

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        if ctx.agent_loop is None:
            return None
        autobio = getattr(ctx.agent_loop, "_autobio_memory", None)
        if autobio is None:
            return None
        try:
            projects = autobio.projects(limit=20)
        except Exception:  # noqa: BLE001
            return None
        cutoff = ctx.now - self._stale_s
        stale = []
        for p in projects:
            if p.last_touch_ts is None:
                continue
            if p.last_touch_ts >= cutoff:
                continue
            if p.status and p.status.lower() not in ("active", ""):
                continue
            if p.name in self._announced:
                continue
            stale.append(p)
        if not stale:
            return None
        # Sort by staleness — oldest first.
        stale.sort(key=lambda p: p.last_touch_ts or 0.0)
        p = stale[0]
        days = int((ctx.now - (p.last_touch_ts or 0.0)) / 86400.0)
        focus_hint = (
            f"上次提到 “{p.current_focus}”。"
            if p.current_focus else ""
        )
        msg = (
            f"🗂 你的项目 **{p.name}** 已经 {days} 天没动了。"
            f"{focus_hint}要继续吗？"
        )
        self._announced[p.name] = ctx.now
        return TriggerProposal(
            trigger_name=self.name,
            message=msg,
            urgency="low",
            payload={
                "project": p.name,
                "days_idle": days,
                "current_focus": p.current_focus,
            },
        )


__all__ = [
    "CalendarReminderTrigger",
    "StaleProjectTrigger",
]
