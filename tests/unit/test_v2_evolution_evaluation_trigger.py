"""B-294: pin EvolutionEvaluationTrigger behaviour.

Without this trigger the entire self-improvement chain is dead in
production — verdicts pile up forever, evaluate() never fires. The
tests below cover the lifecycle (start/stop/idempotent), the debounce
(burst → 1 fire), the cooldown (no spam), and the
min-new-verdicts threshold (skip tiny bursts).
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.evolution_agent import EvolutionAgent
from xmclaw.daemon.evolution_evaluation_trigger import (
    EvolutionEvaluationTrigger,
)


def _verdict(skill_id: str = "skill_x", version: int = 0,
             score: float = 0.8, sid: str = "chat-test") -> object:
    return make_event(
        session_id=sid,
        agent_id="main",
        type=EventType.GRADER_VERDICT,
        payload={
            "skill_id": skill_id,
            "version": version,
            "score": score,
        },
    )


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(evo, bus, debounce_s=0.05)
    await trig.start()
    await trig.start()  # second call no-op
    assert trig.is_active
    await trig.stop()
    await trig.stop()
    assert not trig.is_active
    await evo.stop()


@pytest.mark.asyncio
async def test_disabled_does_nothing() -> None:
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(evo, bus, enabled=False)
    await trig.start()
    assert not trig.is_active
    await evo.stop()


@pytest.mark.asyncio
async def test_burst_collapses_to_one_fire() -> None:
    """20 verdicts in a burst → debounce → exactly 1 evaluate() call."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(
        evo, bus,
        debounce_s=0.05,       # short for tests
        cooldown_s=0.0,        # no cooldown gate
        min_new_verdicts=1,    # fire even on small bursts
    )
    await trig.start()
    try:
        for i in range(20):
            await bus.publish(_verdict(score=0.5 + 0.01 * i))
        # Wait for debounce + a small margin.
        await asyncio.sleep(0.2)
        assert trig.fire_count == 1
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_cooldown_blocks_second_fire_in_window() -> None:
    """Two bursts within cooldown_s → only first one fires."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(
        evo, bus,
        debounce_s=0.05,
        cooldown_s=2.0,
        min_new_verdicts=1,
    )
    await trig.start()
    try:
        await bus.publish(_verdict())
        await asyncio.sleep(0.15)
        assert trig.fire_count == 1
        # Second burst within cooldown.
        await bus.publish(_verdict())
        await asyncio.sleep(0.15)
        assert trig.fire_count == 1  # still only 1
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_min_new_verdicts_threshold_blocks_tiny_bursts() -> None:
    """Bursts smaller than threshold don't fire."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(
        evo, bus,
        debounce_s=0.05,
        cooldown_s=0.0,
        min_new_verdicts=5,
    )
    await trig.start()
    try:
        for _ in range(3):  # 3 < 5
            await bus.publish(_verdict())
        await asyncio.sleep(0.15)
        assert trig.fire_count == 0  # threshold not met
        # Now hit threshold.
        for _ in range(3):
            await bus.publish(_verdict())
        await asyncio.sleep(0.15)
        assert trig.fire_count == 1
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_internal_session_skip() -> None:
    """Verdicts from evolution: / dream: / reflect: / skill-dream / _system
    sessions are skipped — the trigger only acts on real user-driven
    sessions to avoid recursion."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus)
    await evo.start()
    trig = EvolutionEvaluationTrigger(
        evo, bus,
        debounce_s=0.05,
        cooldown_s=0.0,
        min_new_verdicts=1,
    )
    await trig.start()
    try:
        for sid in ("evolution:foo", "dream:x", "reflect:1",
                    "skill-dream", "_system_internal"):
            await bus.publish(_verdict(sid=sid))
        await asyncio.sleep(0.15)
        assert trig.fire_count == 0  # all skipped
        # User-driven verdict DOES fire.
        await bus.publish(_verdict(sid="chat-real"))
        await asyncio.sleep(0.15)
        assert trig.fire_count == 1
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_evaluate_failure_does_not_break_subsequent_fires() -> None:
    """If evaluate() raises, the trigger logs + swallows + keeps subscribed."""
    bus = InProcessEventBus()

    class _BadEvo:
        async def evaluate(self, **_kw):
            raise RuntimeError("simulated failure")

    bad_evo = _BadEvo()
    trig = EvolutionEvaluationTrigger(
        bad_evo, bus,
        debounce_s=0.05,
        cooldown_s=0.0,
        min_new_verdicts=1,
    )
    await trig.start()
    try:
        await bus.publish(_verdict())
        await asyncio.sleep(0.15)
        # No exception bubbled up; trigger still active.
        assert trig.is_active
        # fire_count stayed at 0 (we count only successful fires).
        assert trig.fire_count == 0
        # Counters NOT reset on failure — next burst tries again.
        # ``_verdicts_since_last_fire`` should NOT have been reset
        # so the threshold continues to track.
        assert trig.verdicts_since_last_fire >= 1
    finally:
        await trig.stop()
