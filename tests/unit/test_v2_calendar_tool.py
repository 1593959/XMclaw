"""Sprint 2 Wave 15 — CalendarToolProvider unit tests.

Covers:
  * Tool spec advertised correctly
  * Happy path: create event, file written, ICS parses back
  * Missing required args returns structured error (no crash)
  * Bad ISO 8601 → error
  * Auto-default end = start + 1h
  * Reverse end ≤ start rejected
  * Empty / oversized summary rejected
  * Append into pre-existing ICS file preserves prior events
  * Round-trip: written event is readable by Wave 5 _parse_ics
  * Concurrent appends don't lose events
  * Side effects record the ICS path so the Honest Grader can verify
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from pathlib import Path

import pytest

from xmclaw.cognition.triggers_environment import _parse_ics
from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.calendar import (
    CalendarToolProvider,
    _build_vevent,
    _escape_ics_text,
    _ics_format,
    _parse_iso,
    _validate_args,
)


# ── pure helpers ────────────────────────────────────────────────


def test_parse_iso_with_z():
    d = _parse_iso("2026-05-15T11:00:00Z")
    assert d.tzinfo is not None
    assert d.hour == 11


def test_parse_iso_with_offset():
    d = _parse_iso("2026-05-15T19:00:00+08:00")
    assert d.tzinfo is not None


def test_parse_iso_naive():
    d = _parse_iso("2026-05-15T19:00:00")
    assert d.tzinfo is None


def test_parse_iso_garbage_raises():
    with pytest.raises(ValueError):
        _parse_iso("not-a-date")


def test_ics_format_naive_no_z():
    d = dt.datetime(2026, 5, 15, 19, 0, 0)
    assert _ics_format(d) == "20260515T190000"


def test_ics_format_aware_normalizes_to_utc():
    d = dt.datetime(2026, 5, 15, 19, 0, 0, tzinfo=dt.timezone.utc)
    assert _ics_format(d) == "20260515T190000Z"


def test_escape_ics_text_backslash_semicolon_comma_newline():
    out = _escape_ics_text("a;b,c\n d \\e")
    assert out == "a\\;b\\,c\\n d \\\\e"


def test_build_vevent_includes_required_fields():
    block = _build_vevent(
        uid="test-uid",
        summary="测试",
        start=dt.datetime(2026, 5, 15, 19, 0, 0),
        end=dt.datetime(2026, 5, 15, 20, 0, 0),
        location=None,
        description=None,
    )
    assert "BEGIN:VEVENT" in block
    assert "UID:test-uid" in block
    assert "DTSTART:20260515T190000" in block
    assert "DTEND:20260515T200000" in block
    assert "SUMMARY:测试" in block
    assert "END:VEVENT" in block
    # No optional fields present
    assert "LOCATION:" not in block
    assert "DESCRIPTION:" not in block


def test_build_vevent_optional_fields():
    block = _build_vevent(
        uid="u",
        summary="s",
        start=dt.datetime(2026, 5, 15, 19, 0, 0),
        end=dt.datetime(2026, 5, 15, 20, 0, 0),
        location="会议室 A",
        description="带电脑\n带耳机",
    )
    assert "LOCATION:会议室 A" in block
    assert "DESCRIPTION:带电脑\\n带耳机" in block


# ── _validate_args ──────────────────────────────────────────────


def test_validate_requires_summary():
    with pytest.raises(ValueError, match="summary"):
        _validate_args({"start": "2026-05-15T19:00:00"})


def test_validate_requires_start():
    with pytest.raises(ValueError, match="start"):
        _validate_args({"summary": "s"})


def test_validate_summary_length_cap():
    with pytest.raises(ValueError, match="too long"):
        _validate_args({
            "summary": "x" * 201,
            "start": "2026-05-15T19:00:00",
        })


def test_validate_end_must_be_after_start():
    with pytest.raises(ValueError, match="end must be after start"):
        _validate_args({
            "summary": "s",
            "start": "2026-05-15T20:00:00",
            "end": "2026-05-15T19:00:00",
        })


def test_validate_default_end_is_start_plus_one_hour():
    _, start, end, _, _ = _validate_args({
        "summary": "s",
        "start": "2026-05-15T19:00:00",
    })
    assert (end - start) == dt.timedelta(hours=1)


def test_validate_bad_iso_in_start():
    with pytest.raises(ValueError, match="bad start"):
        _validate_args({
            "summary": "s",
            "start": "tomorrow at 7pm",
        })


# ── full invoke ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_creates_event_in_new_file(tmp_path: Path):
    ics_path = tmp_path / "cal.ics"
    provider = CalendarToolProvider(ics_path=ics_path)
    result = await provider.invoke(ToolCall(
        name="calendar_create_event",
        args={
            "summary": "团队周会",
            "start": "2026-05-15T19:00:00",
            "location": "会议室",
        },
        provenance="synthetic",
    ))
    assert result.ok is True
    assert result.error is None
    assert "uid" in result.content
    assert result.content["summary"] == "团队周会"
    assert str(ics_path) in result.side_effects
    # File exists and parses with Wave 5's _parse_ics.
    events = _parse_ics(ics_path.read_text(encoding="utf-8"))
    assert len(events) == 1
    assert events[0].summary == "团队周会"
    assert events[0].location == "会议室"


@pytest.mark.asyncio
async def test_invoke_appends_to_existing_file(tmp_path: Path):
    ics_path = tmp_path / "cal.ics"
    # Seed with an existing event.
    ics_path.write_text(
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:existing\r\n"
        "SUMMARY:已有事件\r\n"
        "DTSTART:20260515T100000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n",
        encoding="utf-8",
    )
    provider = CalendarToolProvider(ics_path=ics_path)
    result = await provider.invoke(ToolCall(
        name="calendar_create_event",
        args={
            "summary": "新事件",
            "start": "2026-05-15T19:00:00",
        },
        provenance="synthetic",
    ))
    assert result.ok is True
    events = _parse_ics(ics_path.read_text(encoding="utf-8"))
    summaries = sorted(e.summary for e in events)
    assert summaries == ["已有事件", "新事件"]


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_error(tmp_path: Path):
    provider = CalendarToolProvider(ics_path=tmp_path / "cal.ics")
    result = await provider.invoke(ToolCall(
        name="not_calendar_create_event",
        args={},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert "unknown tool" in result.error


@pytest.mark.asyncio
async def test_invoke_missing_summary_structured_error(tmp_path: Path):
    provider = CalendarToolProvider(ics_path=tmp_path / "cal.ics")
    result = await provider.invoke(ToolCall(
        name="calendar_create_event",
        args={"start": "2026-05-15T19:00:00"},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert "summary" in result.error.lower()


@pytest.mark.asyncio
async def test_invoke_bad_iso_structured_error(tmp_path: Path):
    provider = CalendarToolProvider(ics_path=tmp_path / "cal.ics")
    result = await provider.invoke(ToolCall(
        name="calendar_create_event",
        args={"summary": "x", "start": "next Tuesday"},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert "start" in result.error.lower()


def test_list_tools_advertises_calendar_create_event(tmp_path: Path):
    provider = CalendarToolProvider(ics_path=tmp_path / "cal.ics")
    specs = provider.list_tools()
    assert len(specs) == 1
    assert specs[0].name == "calendar_create_event"
    # Schema requires summary + start.
    required = specs[0].parameters_schema["required"]
    assert "summary" in required
    assert "start" in required


# ── concurrency ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_appends_preserve_all_events(tmp_path: Path):
    ics_path = tmp_path / "cal.ics"
    provider = CalendarToolProvider(ics_path=ics_path)

    async def add(i: int) -> None:
        await provider.invoke(ToolCall(
            name="calendar_create_event",
            args={
                "summary": f"事件{i}",
                "start": "2026-05-15T19:00:00",
            },
            provenance="synthetic",
            id=uuid.uuid4().hex,
        ))

    await asyncio.gather(*(add(i) for i in range(10)))
    events = _parse_ics(ics_path.read_text(encoding="utf-8"))
    assert len(events) == 10
    summaries = sorted(e.summary for e in events)
    assert summaries == sorted(f"事件{i}" for i in range(10))


# ── round-trip with CalendarReminderTrigger ─────────────────────


@pytest.mark.asyncio
async def test_round_trip_with_reminder_trigger(tmp_path: Path):
    """Create an event 2 min in the future, then ask
    CalendarReminderTrigger to scan — it should announce."""
    from xmclaw.cognition.proactive_agent import ProactiveContext
    from xmclaw.cognition.triggers_environment import (
        CalendarReminderTrigger,
    )

    ics_path = tmp_path / "cal.ics"
    provider = CalendarToolProvider(ics_path=ics_path)

    soon = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(minutes=2)
    result = await provider.invoke(ToolCall(
        name="calendar_create_event",
        args={
            "summary": "新会议",
            "start": soon.isoformat(),
        },
        provenance="synthetic",
    ))
    assert result.ok is True

    trig = CalendarReminderTrigger(
        ics_path=ics_path, look_ahead_s=300.0,
    )
    ctx = ProactiveContext(
        now=time.time(),
        last_user_message_ts=None,
        last_agent_message_ts=None,
        quiet_hours_active=False,
    )
    assert await trig.should_fire(ctx) is True
    proposal = await trig.propose(ctx)
    assert proposal is not None
    assert "新会议" in proposal.message
