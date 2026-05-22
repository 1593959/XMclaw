"""Sprint 2 Wave 16 — DailyDigestTrigger unit tests."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from xmclaw.cognition.proactive_agent import ProactiveContext
from xmclaw.cognition.triggers_digest import DailyDigestTrigger


def _ctx(now: float, agent_loop=None) -> ProactiveContext:
    return ProactiveContext(
        now=now,
        last_user_message_ts=now - 60.0,
        last_agent_message_ts=now - 60.0,
        quiet_hours_active=False,
        agent_loop=agent_loop,
    )


# ── schedule gating ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_does_not_fire_before_schedule():
    bus = MagicMock()
    trig = DailyDigestTrigger(
        bus=bus, schedule_expr="every 1h",
    )
    assert await trig.should_fire(_ctx(time.time())) is False


@pytest.mark.asyncio
async def test_bad_schedule_fallback_to_interval():
    bus = MagicMock()
    trig = DailyDigestTrigger(
        bus=bus, schedule_expr="invalid-cron",
    )
    # Epic #27 sweep #15: bad schedules now fallback to "every 1d"
    # instead of silently disabling the digest.
    assert trig._next_fire_ts is not None
    assert trig._used_interval_fallback is True
    # Should NOT fire immediately — next_fire_ts is in the future.
    assert await trig.should_fire(_ctx(time.time())) is False


@pytest.mark.asyncio
async def test_propose_advances_schedule():
    bus = MagicMock()
    bus.query.return_value = []
    trig = DailyDigestTrigger(
        bus=bus, schedule_expr="every 60s",
    )
    later = time.time() + 61.0
    proposal = await trig.propose(_ctx(later))
    assert proposal is not None
    # Should have advanced past `later` already.
    assert trig._next_fire_ts is not None
    assert trig._next_fire_ts > later


# ── digest content ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_with_no_events():
    bus = MagicMock()
    bus.query.return_value = []
    trig = DailyDigestTrigger(bus=bus, schedule_expr="every 60s")
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert "今日活动汇总" in p.message
    assert "今天没有主动认知活动" in p.message


@pytest.mark.asyncio
async def test_digest_counts_events_by_type():
    bus = MagicMock()

    def _ev(t: str):
        return SimpleNamespace(type=t, payload={}, ts=time.time())

    bus.query.return_value = [
        _ev("proactive_proposal"),
        _ev("proactive_proposal"),
        _ev("proactive_proposal"),
        _ev("reflection_cycle_ran"),
        _ev("goals_groomed"),
    ]
    trig = DailyDigestTrigger(bus=bus, schedule_expr="every 60s")
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert "主动发声: 3 次" in p.message
    assert "反思周期: 1 次" in p.message
    assert "目标梳理: 1 次" in p.message


@pytest.mark.asyncio
async def test_digest_includes_autobio_section():
    bus = MagicMock()
    bus.query.return_value = []
    autobio = MagicMock()
    autobio.people.return_value = [object(), object()]
    autobio.projects.return_value = [object()]
    agent_loop = SimpleNamespace(
        _autobio_memory=autobio,
        _cognitive_state=None,
    )
    trig = DailyDigestTrigger(
        bus=bus,
        schedule_expr="every 60s",
        agent_loop=agent_loop,
    )
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert "自传记忆" in p.message
    assert "2 个人" in p.message
    assert "1 个项目" in p.message


@pytest.mark.asyncio
async def test_digest_includes_goals_section():
    bus = MagicMock()
    bus.query.return_value = []
    cs = SimpleNamespace(current_goals=[
        SimpleNamespace(
            priority=8, description="完成 Wave 16", status="active",
        ),
    ])
    agent_loop = SimpleNamespace(
        _autobio_memory=None,
        _cognitive_state=cs,
    )
    trig = DailyDigestTrigger(
        bus=bus,
        schedule_expr="every 60s",
        agent_loop=agent_loop,
    )
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert "完成 Wave 16" in p.message
    assert "P8" in p.message


@pytest.mark.asyncio
async def test_digest_resilient_to_failing_subsystem():
    bus = MagicMock()
    bus.query.side_effect = RuntimeError("db locked")
    autobio = MagicMock()
    autobio.people.side_effect = RuntimeError("db locked")
    autobio.projects.side_effect = RuntimeError("db locked")
    agent_loop = SimpleNamespace(
        _autobio_memory=autobio,
        _cognitive_state=None,
    )
    trig = DailyDigestTrigger(
        bus=bus,
        schedule_expr="every 60s",
        agent_loop=agent_loop,
    )
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    # Failing subsystems silently drop their sections — the digest
    # itself still emits a (mostly empty but well-formed) message.
    assert p is not None
    assert "今日活动汇总" in p.message


@pytest.mark.asyncio
async def test_digest_bus_without_query_just_header():
    """A bus with no .query attribute (e.g. raw InProcessEventBus in
    tests that don't mock query) shouldn't crash."""
    bus = SimpleNamespace()  # no query attr at all
    trig = DailyDigestTrigger(bus=bus, schedule_expr="every 60s")
    later = time.time() + 61.0
    p = await trig.propose(_ctx(later))
    assert p is not None
    assert "今日活动汇总" in p.message
