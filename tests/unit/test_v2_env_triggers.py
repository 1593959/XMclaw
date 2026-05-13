"""Unit tests for Sprint 2 environment-aware triggers."""
from __future__ import annotations

import time

import pytest

from xmclaw.cognition.proactive_agent import ProactiveContext
from xmclaw.cognition.triggers_environment import (
    CalendarReminderTrigger,
    StaleProjectTrigger,
    _parse_ics,
    _parse_ics_dt,
)


# ── ICS parser ───────────────────────────────────────────────────


def test_parse_ics_dt_utc():
    """Format: YYYYMMDDTHHMMSSZ"""
    t = _parse_ics_dt("20260513T093000Z")
    assert t is not None
    # Convert back to verify
    import datetime as dt
    parsed = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc)
    assert parsed.hour == 9
    assert parsed.minute == 30


def test_parse_ics_dt_local():
    t = _parse_ics_dt("20260513T093000")
    assert t is not None


def test_parse_ics_dt_date_only():
    t = _parse_ics_dt("20260513")
    assert t is not None


def test_parse_ics_dt_bad_input():
    assert _parse_ics_dt("") is None
    assert _parse_ics_dt("nonsense") is None
    assert _parse_ics_dt("garbage") is None


def test_parse_ics_minimal():
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:event-1\r\n"
        "SUMMARY:Team Standup\r\n"
        "DTSTART:20260513T093000Z\r\n"
        "LOCATION:Conf Room A\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = _parse_ics(ics)
    assert len(events) == 1
    e = events[0]
    assert e.summary == "Team Standup"
    assert e.uid == "event-1"
    assert e.location == "Conf Room A"


def test_parse_ics_handles_folded_lines():
    ics = (
        "BEGIN:VEVENT\n"
        "SUMMARY:Very long event title that gets\n"
        " folded across lines\n"
        "DTSTART:20260513T093000Z\n"
        "END:VEVENT\n"
    )
    events = _parse_ics(ics)
    assert len(events) == 1
    # Folded lines un-fold without extra space
    assert "folded" in events[0].summary


def test_parse_ics_multiple_events():
    ics = (
        "BEGIN:VEVENT\nSUMMARY:A\nDTSTART:20260513T090000Z\nEND:VEVENT\n"
        "BEGIN:VEVENT\nSUMMARY:B\nDTSTART:20260513T100000Z\nEND:VEVENT\n"
        "BEGIN:VEVENT\nSUMMARY:C\nDTSTART:20260513T110000Z\nEND:VEVENT\n"
    )
    events = _parse_ics(ics)
    assert [e.summary for e in events] == ["A", "B", "C"]


def test_parse_ics_skips_invalid_events():
    ics = (
        "BEGIN:VEVENT\nSUMMARY:Good\nDTSTART:20260513T090000Z\nEND:VEVENT\n"
        "BEGIN:VEVENT\nSUMMARY:NoDate\nEND:VEVENT\n"
        "BEGIN:VEVENT\nDTSTART:20260513T100000Z\nEND:VEVENT\n"
    )
    events = _parse_ics(ics)
    assert len(events) == 1
    assert events[0].summary == "Good"


def test_parse_ics_empty():
    assert _parse_ics("") == []


# ── CalendarReminderTrigger ──────────────────────────────────────


def _ics_for(events: list[tuple[str, float, str | None]]) -> str:
    """Construct an ICS string from (summary, epoch, uid?) tuples."""
    import datetime as dt
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for i, (summary, ts, uid) in enumerate(events):
        dtstart = dt.datetime.fromtimestamp(
            ts, tz=dt.timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid or f'evt-{i}'}")
        lines.append(f"SUMMARY:{summary}")
        lines.append(f"DTSTART:{dtstart}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


def _ctx(now: float, agent_loop=None) -> ProactiveContext:
    return ProactiveContext(
        now=now,
        last_user_message_ts=now - 60.0,
        last_agent_message_ts=now - 60.0,
        quiet_hours_active=False,
        agent_loop=agent_loop,
    )


@pytest.mark.asyncio
async def test_calendar_fires_for_imminent_event(tmp_path):
    now = time.time()
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(_ics_for([
        ("Team Standup", now + 120, "u1"),  # 2 min from now
    ]))
    trig = CalendarReminderTrigger(ics_path=ics_path, look_ahead_s=300.0)
    ctx = _ctx(now)
    assert await trig.should_fire(ctx) is True
    proposal = await trig.propose(ctx)
    assert proposal.trigger_name == "calendar_reminder"
    assert "Team Standup" in proposal.message
    assert proposal.urgency in ("normal", "high")


@pytest.mark.asyncio
async def test_calendar_does_not_fire_for_far_future(tmp_path):
    now = time.time()
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(_ics_for([
        ("Tomorrow", now + 86400, None),
    ]))
    trig = CalendarReminderTrigger(ics_path=ics_path, look_ahead_s=300.0)
    ctx = _ctx(now)
    assert await trig.should_fire(ctx) is False


@pytest.mark.asyncio
async def test_calendar_does_not_fire_for_past(tmp_path):
    now = time.time()
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(_ics_for([
        ("Done", now - 60, None),
    ]))
    trig = CalendarReminderTrigger(ics_path=ics_path, look_ahead_s=300.0)
    ctx = _ctx(now)
    assert await trig.should_fire(ctx) is False


@pytest.mark.asyncio
async def test_calendar_does_not_re_announce(tmp_path):
    now = time.time()
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(_ics_for([
        ("Meeting", now + 60, "uid-x"),
    ]))
    trig = CalendarReminderTrigger(ics_path=ics_path, look_ahead_s=300.0)
    ctx = _ctx(now)
    assert await trig.should_fire(ctx) is True
    p = await trig.propose(ctx)
    assert p is not None
    # Second time same event — already announced
    assert await trig.should_fire(ctx) is False


@pytest.mark.asyncio
async def test_calendar_missing_file_no_crash(tmp_path):
    trig = CalendarReminderTrigger(ics_path=tmp_path / "nonexistent.ics")
    ctx = _ctx(time.time())
    assert await trig.should_fire(ctx) is False


@pytest.mark.asyncio
async def test_calendar_high_urgency_within_2_min(tmp_path):
    now = time.time()
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(_ics_for([
        ("Urgent!", now + 60, "u-urg"),
    ]))
    trig = CalendarReminderTrigger(ics_path=ics_path)
    p = await trig.propose(_ctx(now))
    assert p.urgency == "high"


# ── StaleProjectTrigger ──────────────────────────────────────────


class _StubAutobio:
    def __init__(self, projects):
        self._projects = projects
    def projects(self, limit=20):
        return self._projects[:limit]


@pytest.mark.asyncio
async def test_stale_project_fires_for_idle(tmp_path):
    from xmclaw.cognition.autobiographical_memory import Project
    now = time.time()
    stale_ts = now - 10 * 86400.0  # 10 days ago
    autobio = _StubAutobio([
        Project(id="1", name="XMclaw", status="active",
                current_focus="proactive agent",
                last_touch_ts=stale_ts),
    ])

    class _FakeAgent:
        _autobio_memory = autobio

    trig = StaleProjectTrigger(stale_days=7.0)
    ctx = _ctx(now, agent_loop=_FakeAgent())
    assert await trig.should_fire(ctx) is True
    p = await trig.propose(ctx)
    assert "XMclaw" in p.message
    assert p.payload["days_idle"] >= 7


@pytest.mark.asyncio
async def test_stale_project_does_not_fire_for_fresh(tmp_path):
    from xmclaw.cognition.autobiographical_memory import Project
    now = time.time()
    autobio = _StubAutobio([
        Project(id="1", name="XMclaw", status="active",
                current_focus=None,
                last_touch_ts=now - 86400.0),  # 1 day ago
    ])

    class _FakeAgent:
        _autobio_memory = autobio

    trig = StaleProjectTrigger(stale_days=7.0)
    assert await trig.should_fire(
        _ctx(now, agent_loop=_FakeAgent())
    ) is False


@pytest.mark.asyncio
async def test_stale_project_skips_completed(tmp_path):
    from xmclaw.cognition.autobiographical_memory import Project
    now = time.time()
    autobio = _StubAutobio([
        Project(id="1", name="Old", status="completed",
                current_focus=None,
                last_touch_ts=now - 30 * 86400.0),
    ])

    class _FakeAgent:
        _autobio_memory = autobio

    trig = StaleProjectTrigger(stale_days=7.0)
    assert await trig.should_fire(
        _ctx(now, agent_loop=_FakeAgent())
    ) is False


@pytest.mark.asyncio
async def test_stale_project_no_autobio_no_fire(tmp_path):
    class _FakeAgent:
        _autobio_memory = None

    trig = StaleProjectTrigger()
    assert await trig.should_fire(
        _ctx(time.time(), agent_loop=_FakeAgent())
    ) is False


@pytest.mark.asyncio
async def test_stale_project_cooldown_via_announce(tmp_path):
    from xmclaw.cognition.autobiographical_memory import Project
    now = time.time()
    autobio = _StubAutobio([
        Project(id="1", name="X", status="active",
                current_focus=None,
                last_touch_ts=now - 10 * 86400.0),
    ])

    class _FakeAgent:
        _autobio_memory = autobio

    trig = StaleProjectTrigger(stale_days=7.0)
    ctx = _ctx(now, agent_loop=_FakeAgent())
    p1 = await trig.propose(ctx)
    assert p1 is not None
    # Same project already announced — should not fire again
    assert await trig.should_fire(ctx) is False
