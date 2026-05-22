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
        quiet_start_hour=0, quiet_end_hour=0,
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
        quiet_start_hour=0, quiet_end_hour=0,
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
        quiet_start_hour=0, quiet_end_hour=0,
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
        quiet_start_hour=0, quiet_end_hour=0,
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
        quiet_start_hour=0, quiet_end_hour=0,
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
    agent = ProactiveAgent(publish=pub, quiet_start_hour=0, quiet_end_hour=0)
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
    agent = ProactiveAgent(publish=pub, quiet_start_hour=0, quiet_end_hour=0)
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


# ── Wave-32+ contextual message generation ──────────────────────────


@pytest.mark.asyncio
async def test_idle_trigger_pinned_message_still_works():
    """Operator override: ``message=...`` ctor arg pins the greeting.
    Used by tests + by operators who want the legacy fixed string."""
    t = IdleCheckInTrigger(idle_threshold_s=10.0, message="fixed text")
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
    )
    p = await t.propose(ctx)
    assert p.message == "fixed text"


@pytest.mark.asyncio
async def test_idle_trigger_fallback_rotates_when_no_llm():
    """Without an agent_loop wired, the trigger picks from the
    fallback template pool. Three consecutive proposes must hit
    three different templates (round-robin) — pin so a refactor
    that re-uses the first slot is caught."""
    t = IdleCheckInTrigger(idle_threshold_s=10.0, use_llm=False)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
    )
    msgs = [(await t.propose(ctx)).message for _ in range(3)]
    assert len(set(msgs)) == 3, f"templates not rotating: {msgs}"
    # No raw "你那边还好吗" anywhere — that's the line the user
    # explicitly called out as too robotic.
    for m in msgs:
        assert "你那边还好吗" not in m


@pytest.mark.asyncio
async def test_idle_trigger_fallback_substitutes_minutes():
    """The {minutes} interpolation in the template pool must fill in
    real minutes — otherwise the user sees a literal '{minutes}'."""
    t = IdleCheckInTrigger(idle_threshold_s=10.0, use_llm=False)
    # 1800s = 30 minutes idle.
    ctx = ProactiveContext(
        now=2000.0, last_user_message_ts=200.0,
        last_agent_message_ts=200.0,
        quiet_hours_active=False,
    )
    # Force the first template (which has {minutes}).
    p = await t.propose(ctx)
    assert "{minutes}" not in p.message
    # At least one template in the pool references the actual number.
    saw_number = "30" in p.message
    for _ in range(5):
        p = await t.propose(ctx)
        saw_number = saw_number or "30" in p.message
    assert saw_number, "minutes never interpolated in any rotation"


@pytest.mark.asyncio
async def test_idle_trigger_uses_llm_when_available():
    """When agent_loop._llm + _histories are wired, the trigger asks
    the LLM for a contextual one-liner referencing the recent
    conversation, not a template."""
    from xmclaw.core.ir import Message

    class _StubLLM:
        def __init__(self) -> None:
            self.last_prompt: str | None = None

        async def complete(self, messages, tools=None):
            self.last_prompt = messages[-1].content
            class _R:
                content = "auth flow 那块要继续吗"
                tool_calls = ()
            return _R()

    class _StubLoop:
        def __init__(self, llm, histories) -> None:
            self._llm = llm
            self._histories = histories

    llm = _StubLLM()
    loop = _StubLoop(
        llm,
        {"sess-1": [
            Message(role="user", content="帮我调试 auth flow"),
            Message(role="assistant", content="先看一下登录的 token 校验"),
        ]},
    )
    t = IdleCheckInTrigger(idle_threshold_s=10.0)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
        agent_loop=loop,
    )
    p = await t.propose(ctx)
    assert p.message == "auth flow 那块要继续吗"
    # The LLM prompt referenced both the user + assistant last turns.
    assert "auth flow" in llm.last_prompt
    assert "token" in llm.last_prompt


@pytest.mark.asyncio
async def test_idle_trigger_falls_back_when_llm_errors():
    """LLM call exceptions must not bubble — fall back to a
    template silently."""
    from xmclaw.core.ir import Message

    class _BrokenLLM:
        async def complete(self, *a, **kw):
            raise RuntimeError("boom")

    class _Loop:
        def __init__(self) -> None:
            self._llm = _BrokenLLM()
            self._histories = {"s": [
                Message(role="user", content="hi"),
                Message(role="assistant", content="hi back"),
            ]}

    t = IdleCheckInTrigger(idle_threshold_s=10.0)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
        agent_loop=_Loop(),
    )
    p = await t.propose(ctx)
    # Should be a fallback template — pin a property: not the old
    # robotic phrase, not empty, reasonable length.
    assert "你那边还好吗" not in p.message
    assert 5 <= len(p.message) <= 80


@pytest.mark.asyncio
async def test_idle_trigger_llm_path_skipped_when_use_llm_false():
    """use_llm=False forces the fallback path even when a working
    LLM is reachable — gives operators a kill switch for installs
    with expensive LLM keys."""
    from xmclaw.core.ir import Message

    calls = []
    class _CountingLLM:
        async def complete(self, *a, **kw):
            calls.append(1)
            class _R:
                content = "should not appear"
                tool_calls = ()
            return _R()

    class _Loop:
        def __init__(self) -> None:
            self._llm = _CountingLLM()
            self._histories = {"s": [
                Message(role="user", content="x"),
                Message(role="assistant", content="y"),
            ]}

    t = IdleCheckInTrigger(idle_threshold_s=10.0, use_llm=False)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
        agent_loop=_Loop(),
    )
    p = await t.propose(ctx)
    assert calls == [], "LLM was called despite use_llm=False"
    assert p.message != "should not appear"


@pytest.mark.asyncio
async def test_idle_trigger_strips_wrapping_quotes_from_llm():
    """Models sometimes wrap output in 「」 / quotes despite being
    told not to. Strip those so the UI doesn't show literal quote
    marks around the message."""
    from xmclaw.core.ir import Message

    class _QuotyLLM:
        async def complete(self, *a, **kw):
            class _R:
                content = '「调试卡住了？」'
                tool_calls = ()
            return _R()

    class _Loop:
        def __init__(self) -> None:
            self._llm = _QuotyLLM()
            self._histories = {"s": [
                Message(role="user", content="debug 这个问题"),
                Message(role="assistant", content="先看 trace"),
            ]}

    t = IdleCheckInTrigger(idle_threshold_s=10.0)
    ctx = ProactiveContext(
        now=1000.0, last_user_message_ts=985.0,
        last_agent_message_ts=985.0,
        quiet_hours_active=False,
        agent_loop=_Loop(),
    )
    p = await t.propose(ctx)
    assert p.message == "调试卡住了？"


# ── note_user_message hook ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_user_message_updates_context():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, quiet_start_hour=0, quiet_end_hour=0)
    agent.note_user_message(ts=12345.0)
    assert agent._last_user_message_ts == 12345.0


@pytest.mark.asyncio
async def test_note_user_message_default_now():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, quiet_start_hour=0, quiet_end_hour=0)
    before = time.time()
    agent.note_user_message()
    after = time.time()
    assert before <= agent._last_user_message_ts <= after


# ── start/stop lifecycle ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_then_stop_clean():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, tick_interval_s=0.05, quiet_start_hour=0, quiet_end_hour=0)
    await agent.start()
    await asyncio.sleep(0.15)  # let it run a few ticks
    await agent.stop()
    # No triggers registered → no proposals
    assert pub.calls == []


@pytest.mark.asyncio
async def test_double_start_is_idempotent():
    pub = _RecordingPublisher()
    agent = ProactiveAgent(publish=pub, tick_interval_s=0.1, quiet_start_hour=0, quiet_end_hour=0)
    await agent.start()
    await agent.start()  # should not crash or leak
    await agent.stop()
