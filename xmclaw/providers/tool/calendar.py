"""CalendarToolProvider — Wave 15.

Lets the agent CREATE calendar events by appending VEVENT blocks to
the same ICS file CalendarReminderTrigger (Wave 5) reads. The trigger's
60-second file cache picks up new entries on the next tick, so the
loop is:

  user → "帮我加个周三晚 7 点的提醒"
       → LLM calls calendar_create_event(summary, start, ...)
       → tool appends VEVENT to ICS file
       → CalendarReminderTrigger reads file ~60s later → reminder pipeline
       → Wave 5+9: appears in Web UI + pushed to feishu when imminent

Why we write the same ICS file the user exported instead of going
through Google Calendar / Outlook API:
  * No OAuth. Calendars exported to ICS are local first.
  * Round-trip already works: write here → trigger reads it. The
    existing CalendarReminderTrigger code path is the audience.
  * If the user wants two-way sync with Google/Outlook, that's a
    separate concern (their calendar app keeps re-exporting; or they
    use a sync tool like vdirsyncer). Out of scope for v1.

Tool:
  calendar_create_event(summary, start, [end], [location], [description])

  * summary: required string, ≤ 200 chars
  * start: required ISO 8601 ("2026-05-15T19:00:00" or with TZ "...+08:00")
  * end: optional ISO 8601 — defaults to start + 1h
  * location: optional string
  * description: optional string (multi-line OK; escaped per RFC 5545)

The tool returns the new event's UID + a human-readable confirmation,
and adds the ICS path to ToolResult.side_effects so the Honest Grader
can verify the write actually happened.

Concurrency: an asyncio.Lock serializes writes from a single daemon.
We don't claim safety across multiple daemons writing the same file,
but that's not a supported topology.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


_CALENDAR_CREATE_EVENT_SPEC = ToolSpec(
    name="calendar_create_event",
    description=(
        "Create a calendar event by appending it to the user's ICS "
        "calendar file. The CalendarReminderTrigger will pick it up "
        "and send a reminder when the time is near. Use when the user "
        "asks to add / schedule / remind them of something at a "
        "specific time."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short title of the event, e.g. '团队周会'.",
                "maxLength": 200,
            },
            "start": {
                "type": "string",
                "description": (
                    "Event start time in ISO 8601 format. "
                    "Local time: '2026-05-15T19:00:00'. "
                    "With timezone offset: '2026-05-15T19:00:00+08:00'. "
                    "UTC: '2026-05-15T11:00:00Z'."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "Optional event end time in ISO 8601 format. "
                    "Defaults to start + 1 hour."
                ),
            },
            "location": {
                "type": "string",
                "description": "Optional location string.",
            },
            "description": {
                "type": "string",
                "description": "Optional notes / agenda body.",
            },
        },
        "required": ["summary", "start"],
        "additionalProperties": False,
    },
)


class CalendarToolProvider(ToolProvider):
    """Exposes ``calendar_create_event`` to the agent.

    Args:
        ics_path: Path to the ICS file shared with
            CalendarReminderTrigger. Creates the parent directory and
            an empty calendar skeleton if the file doesn't exist yet.
    """

    def __init__(self, ics_path: str | Path) -> None:
        self._ics_path = Path(ics_path).expanduser()
        self._write_lock = asyncio.Lock()

    def list_tools(self) -> list[ToolSpec]:
        return [_CALENDAR_CREATE_EVENT_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name != "calendar_create_event":
            return ToolResult(
                call_id=call.id, ok=False,
                content=None,
                error=f"unknown tool: {call.name}",
            )
        t0 = time.perf_counter()
        try:
            summary, start, end, location, description = _validate_args(
                call.args,
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=str(exc),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        uid = uuid.uuid4().hex + "@xmclaw"
        vevent = _build_vevent(
            uid=uid, summary=summary, start=start, end=end,
            location=location, description=description,
        )
        try:
            async with self._write_lock:
                await asyncio.to_thread(
                    _append_vevent, self._ics_path, vevent,
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"failed to write ICS: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        # Friendly natural-language confirmation; the agent will relay
        # this verbatim to the user when no further work is needed.
        confirmation = (
            f"✅ 已添加日程：{summary} ({_format_for_user(start)})"
        )
        if location:
            confirmation += f"，地点 {location}"
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "uid": uid,
                "summary": summary,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "ics_path": str(self._ics_path),
                "message": confirmation,
            },
            side_effects=(str(self._ics_path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )


# ── pure helpers ───────────────────────────────────────────────────


def _validate_args(
    args: dict[str, Any],
) -> tuple[str, dt.datetime, dt.datetime, str | None, str | None]:
    """Validate + coerce the incoming args. Raises ValueError on bad
    input so the caller can return a structured ToolResult error."""
    if not isinstance(args, dict):
        raise ValueError("args must be a dict")
    summary = (args.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required and must be non-empty")
    if len(summary) > 200:
        raise ValueError("summary too long (max 200 chars)")
    start_raw = (args.get("start") or "").strip()
    if not start_raw:
        raise ValueError("start is required (ISO 8601)")
    try:
        start = _parse_iso(start_raw)
    except ValueError as exc:
        raise ValueError(f"bad start: {exc}") from exc
    end_raw = (args.get("end") or "").strip()
    if end_raw:
        try:
            end = _parse_iso(end_raw)
        except ValueError as exc:
            raise ValueError(f"bad end: {exc}") from exc
    else:
        end = start + dt.timedelta(hours=1)
    if end <= start:
        raise ValueError("end must be after start")
    location = (args.get("location") or "").strip() or None
    description = (args.get("description") or "").strip() or None
    return summary, start, end, location, description


def _parse_iso(s: str) -> dt.datetime:
    """Parse ISO 8601 — accept trailing Z or +HH:MM. Returns
    timezone-aware datetime when offset/Z is given, naive otherwise.
    The ICS emit step normalizes to UTC for naive inputs."""
    s = s.strip()
    if s.endswith("Z"):
        # Python's fromisoformat (3.10) doesn't accept Z; swap to +00:00
        s = s[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO 8601 datetime: {s!r}") from exc


def _format_for_user(d: dt.datetime) -> str:
    """Compact human-readable for the confirmation string."""
    return d.strftime("%Y-%m-%d %H:%M")


def _ics_format(d: dt.datetime) -> str:
    """Format a datetime for an ICS DTSTART/DTEND value. Naive →
    treated as local time, aware → converted to UTC + Z suffix."""
    if d.tzinfo is None:
        # Local time form per RFC 5545: YYYYMMDDTHHMMSS (no Z)
        return d.strftime("%Y%m%dT%H%M%S")
    utc = d.astimezone(dt.timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _escape_ics_text(s: str) -> str:
    """Escape per RFC 5545 §3.3.11. Backslash, semicolon, comma, and
    newline get escaped. Folding (line-length cap at 75 octets) is
    skipped — most modern parsers tolerate longer lines, and the
    Wave 5 _parse_ics already un-folds gracefully."""
    return (
        s.replace("\\", "\\\\")
         .replace(";", "\\;")
         .replace(",", "\\,")
         .replace("\n", "\\n")
         .replace("\r", "")
    )


def _build_vevent(
    *,
    uid: str,
    summary: str,
    start: dt.datetime,
    end: dt.datetime,
    location: str | None,
    description: str | None,
) -> str:
    """Render a VEVENT block. CRLF per RFC 5545."""
    dtstamp = dt.datetime.now(tz=dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ",
    )
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_ics_format(start)}",
        f"DTEND:{_ics_format(end)}",
        f"SUMMARY:{_escape_ics_text(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape_ics_text(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape_ics_text(description)}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def _append_vevent(ics_path: Path, vevent_block: str) -> None:
    """Append a single VEVENT block to the ICS file.

    Two cases:
      1. File doesn't exist → create with a minimal VCALENDAR wrapper
         containing only this event.
      2. File exists → insert before the final END:VCALENDAR line; if
         the file is malformed (no END:VCALENDAR), append our event +
         a fresh END:VCALENDAR so the result is at least valid.

    Atomic via tmp file + rename so a crash mid-write can't leave a
    half-written ICS that breaks the trigger's parser."""
    ics_path.parent.mkdir(parents=True, exist_ok=True)
    if not ics_path.exists():
        content = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//XMclaw//calendar_create_event//EN\r\n"
            f"{vevent_block}"
            "END:VCALENDAR\r\n"
        )
    else:
        existing = ics_path.read_text(encoding="utf-8")
        # Drop trailing whitespace so the END marker is the last thing.
        existing_trimmed = existing.rstrip()
        end_marker = "END:VCALENDAR"
        idx = existing_trimmed.rfind(end_marker)
        if idx < 0:
            # Malformed — wrap it.
            content = (
                existing_trimmed + "\r\n"
                + vevent_block
                + "END:VCALENDAR\r\n"
            )
        else:
            content = (
                existing_trimmed[:idx]
                + vevent_block
                + end_marker + "\r\n"
            )
    tmp = ics_path.with_suffix(ics_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(ics_path)


__all__ = ["CalendarToolProvider"]
