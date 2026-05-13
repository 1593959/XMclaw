"""Sprint 2 Wave 11 — CronTrigger unit tests."""
from __future__ import annotations

import time

import pytest

from xmclaw.cognition.proactive_agent import ProactiveContext
from xmclaw.cognition.triggers_cron import (
    CronTrigger,
    build_cron_triggers_from_config,
)


def _ctx(now: float) -> ProactiveContext:
    return ProactiveContext(
        now=now,
        last_user_message_ts=now - 60.0,
        last_agent_message_ts=now - 60.0,
        quiet_hours_active=False,
    )


# ── basic should_fire / propose ──────────────────────────────────


@pytest.mark.asyncio
async def test_does_not_fire_before_next_slot():
    now = time.time()
    trig = CronTrigger(
        name="t1",
        schedule_expr="every 1h",
        message="测试",
    )
    # Right after construction, next_fire_ts is 1h in the future.
    assert await trig.should_fire(_ctx(now)) is False


@pytest.mark.asyncio
async def test_fires_when_clock_crosses_next_slot():
    now = time.time()
    trig = CronTrigger(
        name="t1",
        schedule_expr="every 60s",
        message="testing",
    )
    # 61s later → past the next_fire.
    later = now + 61.0
    assert await trig.should_fire(_ctx(later)) is True

    proposal = await trig.propose(_ctx(later))
    assert proposal is not None
    assert proposal.message == "testing"
    assert proposal.trigger_name == "t1"


@pytest.mark.asyncio
async def test_propose_advances_to_next_slot():
    now = time.time()
    trig = CronTrigger(
        name="t1",
        schedule_expr="every 60s",
        message="m",
    )
    later = now + 61.0
    await trig.propose(_ctx(later))
    # Now the next slot is ~60s after later → should not re-fire
    # immediately.
    assert await trig.should_fire(_ctx(later + 5.0)) is False
    # And after another minute it fires again.
    assert await trig.should_fire(_ctx(later + 75.0)) is True


@pytest.mark.asyncio
async def test_urgency_passes_through():
    trig = CronTrigger(
        name="t",
        schedule_expr="every 60s",
        message="m",
        urgency="high",
    )
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert p.urgency == "high"


@pytest.mark.asyncio
async def test_bad_urgency_defaults_to_normal():
    trig = CronTrigger(
        name="t",
        schedule_expr="every 60s",
        message="m",
        urgency="HUGE",  # not a valid value
    )
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert p.urgency == "normal"


# ── bad schedule handling ────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_schedule_never_fires():
    trig = CronTrigger(
        name="bad",
        schedule_expr="not-a-real-schedule",
        message="m",
    )
    assert trig._next_fire_ts is None
    assert await trig.should_fire(_ctx(time.time())) is False
    assert await trig.propose(_ctx(time.time())) is None


# ── build_cron_triggers_from_config ──────────────────────────────


def test_build_from_empty_config():
    assert build_cron_triggers_from_config(None) == []
    assert build_cron_triggers_from_config([]) == []


def test_build_skips_disabled_jobs():
    triggers = build_cron_triggers_from_config([
        {
            "name": "morning",
            "schedule": "every 1h",
            "message": "morning",
            "enabled": True,
        },
        {
            "name": "afternoon",
            "schedule": "every 2h",
            "message": "afternoon",
            "enabled": False,
        },
    ])
    names = [t.name for t in triggers]
    assert "morning" in names
    assert "afternoon" not in names


def test_build_skips_missing_required_fields():
    triggers = build_cron_triggers_from_config([
        {
            "name": "good",
            "schedule": "every 1h",
            "message": "yes",
        },
        {
            # missing schedule
            "name": "bad1",
            "message": "no schedule",
        },
        {
            # missing message
            "name": "bad2",
            "schedule": "every 1h",
        },
    ])
    assert len(triggers) == 1
    assert triggers[0].name == "good"


def test_build_dedupes_by_name():
    triggers = build_cron_triggers_from_config([
        {"name": "dup", "schedule": "every 1h", "message": "first"},
        {"name": "dup", "schedule": "every 2h", "message": "second"},
    ])
    assert len(triggers) == 1
    assert triggers[0].name == "dup"


def test_build_skips_bad_schedule():
    triggers = build_cron_triggers_from_config([
        {
            "name": "good",
            "schedule": "every 1h",
            "message": "y",
        },
        {
            "name": "bad_sched",
            "schedule": "fancy-cron-without-croniter",
            "message": "y",
        },
    ])
    names = [t.name for t in triggers]
    assert "good" in names
    assert "bad_sched" not in names


def test_build_falls_back_to_indexed_name_when_missing():
    triggers = build_cron_triggers_from_config([
        {
            # no name
            "schedule": "every 1h",
            "message": "y",
        },
    ])
    assert len(triggers) == 1
    assert triggers[0].name == "cron_0"


def test_build_skips_non_dict_entries():
    triggers = build_cron_triggers_from_config([
        "string-entry",
        {"name": "good", "schedule": "every 1h", "message": "y"},
        12345,
    ])
    assert len(triggers) == 1
