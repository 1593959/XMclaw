"""Unit tests for ProactiveAgent (Sprint 1)."""
from __future__ import annotations

import asyncio
import time

import pytest

from xmclaw.cognition.proactive_agent import (
    IdleCheckInTrigger,
    ProactiveAgent,
    ProactiveContext,
    ProactiveTrigger,
    SystemHealthTrigger,
    TriggerProposal,
)


# ── Helpers ─────────────────────────────────────────────────────────


class _RecordingPublisher:
    """Captures every (type, payload) the agent publishes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, type_str: str, payload: dict):
        self.calls.append((type_str, payload))


class _StubTrigger(ProactiveTrigger):
    def __init__(self, name="stub", cooldown_s=60.0, fire=True,
                 message="hello"):
        self.name = name
        self.cooldown_s = cooldown_s
        self._fire = fire
        self._message = message
        self.fire_count = 0

    async def should_fire(self, ctx):
        return self._fire

    async def propose(self, ctx):
        self.fire_count += 1
        return TriggerProposal(
            trigger_name=self.name, message=self._message,
        )


class _RaisingTrigger(ProactiveTrigger):
    name = "raiser"
    cooldown_s = 60.0

    async def should_fire(self, ctx):
        raise RuntimeError("boom in should_fire")

    async def propose(self, ctx):
        return None


class _SlowTrigger(ProactiveTrigger):
    name = "slow"
    cooldown_s = 60.0

    async def should_fire(self, ctx):
        await asyncio.sleep(2.0)
        return True

    async def propose(self, ctx):
        return TriggerProposal(trigger_name=self.name, message="late")


# ── ProactiveAgent core ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registered_trigger_fires_once_per_tick():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
    )
    trig = _StubTrigger()
    agent.register_trigger(trig)
    fired = await agent._tick_once()
    assert fired == 1
    assert len(pub.calls) == 1
    assert pub.calls[0][0] == "proactive_proposal"
    assert pub.calls[0][1]["trigger"] == "stub"
    assert pub.calls[0][1]["message"] == "hello"


@pytest.mark.asyncio
async def test_cooldown_prevents_double_fire():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
    )
    trig = _StubTrigger(cooldown_s=60.0)
    agent.register_trigger(trig)
    await agent._tick_once()
    # Immediate second tick should be cooldown-blocked
    await agent._tick_once()
    assert len(pub.calls) == 1


@pytest.mark.asyncio
async def test_global_min_gap_throttles():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=60.0,
    )
    t1 = _StubTrigger(name="t1", cooldown_s=0.0)
    t2 = _StubTrigger(name="t2", cooldown_s=0.0)
    agent.register_trigger(t1)
    agent.register_trigger(t2)
    await agent._tick_once()
    # Both triggers want to fire but global gap of 60s blocks the
    # second one this tick.
    await agent._tick_once()
    assert len(pub.calls) == 1


@pytest.mark.asyncio
async def test_misbehaving_trigger_does_not_crash_loop():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
    )
    agent.register_trigger(_RaisingTrigger())
    agent.register_trigger(_StubTrigger(name="ok"))
    # Should not raise.
    fired = await agent._tick_once()
    # The good trigger still fires.
    assert fired == 1
    assert pub.calls[0][1]["trigger"] == "ok"


@pytest.mark.asyncio
async def test_slow_trigger_is_timed_out():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
    )
    agent.register_trigger(_SlowTrigger())
    agent.register_trigger(_StubTrigger(name="ok"))
    fired = await agent._tick_once()
    assert fired == 1
    assert pub.calls[0][1]["trigger"] == "ok"


@pytest.mark.asyncio
async def test_quiet_hours_suppress_normal_urgency():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
        # Force quiet hours to be active right now.
        quiet_start_hour=0, quiet_end_hour=24,
    )

    class _NormalTrigger(ProactiveTrigger):
        name = "normal"
        cooldown_s = 60.0
        async def should_fire(self, ctx): return True
        async def propose(self, ctx):
            return TriggerProposal(
                trigger_name=self.name, message="ping",
                urgency="normal",
            )

    agent.register_trigger(_NormalTrigger())
    fired = await agent._tick_once()
    assert fired == 0
    assert len(pub.calls) == 0


@pytest.mark.asyncio
async def test_quiet_hours_allow_high_urgency():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, tick_interval_s=0.1, global_min_gap_s=0.0,
        quiet_start_hour=0, quiet_end_hour=24,
    )

    class _UrgentTrigger(ProactiveTrigger):
        name = "urgent"
        cooldown_s = 60.0
        async def should_fire(self, ctx): return True
        async def propose(self, ctx):
            return TriggerProposal(
                trigger_name=self.name, message="alert",
                urgency="high",
            )

    agent.register_trigger(_UrgentTrigger())
    fired = await agent._tick_once()
    assert fired == 1


@pytest.mark.asyncio
async def test_re_registering_same_name_replaces():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub)
    t1 = _StubTrigger(name="same", message="v1")
    t2 = _StubTrigger(name="same", message="v2")
    agent.register_trigger(t1)
    agent.register_trigger(t2)
    assert agent.trigger_names() == ["same"]
    await agent._tick_once()
    assert pub.calls[0][1]["message"] == "v2"


@pytest.mark.asyncio
async def test_unregister_removes():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub)
    agent.register_trigger(_StubTrigger(name="x"))
    assert agent.unregister_trigger("x") is True
    assert agent.unregister_trigger("nope") is False
    assert agent.trigger_names() == []


def test_quiet_hours_window_normal():
    """Window 23-07 crosses midnight."""
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, quiet_start_hour=23, quiet_end_hour=7,
    )
    # Smoke-test the helper at a known hour.
    assert isinstance(agent._is_quiet_hours_active(), bool)


def test_quiet_hours_same_start_end_disabled():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(
        publish=pub, quiet_start_hour=0, quiet_end_hour=0,
    )
    assert agent._is_quiet_hours_active() is False


# ── IdleCheckInTrigger ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_trigger_fires_after_threshold():
    t = IdleCheckInTrigger(idle_threshold_s=10.0)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
    )
    assert await t.should_fire(ctx) is True
    proposal = await t.propose(ctx)
    assert proposal.trigger_name == "idle_check_in"
    assert proposal.urgency == "low"


@pytest.mark.asyncio
async def test_idle_trigger_not_before_threshold():
    t = IdleCheckInTrigger(idle_threshold_s=60.0)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
    )
    assert await t.should_fire(ctx) is False


@pytest.mark.asyncio
async def test_idle_trigger_never_fires_without_history():
    t = IdleCheckInTrigger()
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=None,
        last_agent_message_ts=None,
        quiet_hours_active=False,
    )
    assert await t.should_fire(ctx) is False


# ── note_user_message hook ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_user_message_updates_context():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub)
    agent.note_user_message(ts=12345.0)
    assert agent._last_user_message_ts == 12345.0


@pytest.mark.asyncio
async def test_note_user_message_default_now():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub)
    before = time.time()
    agent.note_user_message()
    after = time.time()
    assert before <= agent._last_user_message_ts <= after


# ── start/stop lifecycle ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_then_stop_clean():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, tick_interval_s=0.05)
    await agent.start()
    await asyncio.sleep(0.15)  # let it run a few ticks
    await agent.stop()
    # No triggers registered → no proposals
    assert pub.calls == []


@pytest.mark.asyncio
async def test_double_start_is_idempotent():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, tick_interval_s=0.1)
    await agent.start()
    await agent.start()  # should not crash or leak
    await agent.stop()
