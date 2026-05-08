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
async def test_start_stop_idempotent(tmp_path) -> None:
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
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
async def test_disabled_does_nothing(tmp_path) -> None:
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
    await evo.start()
    trig = EvolutionEvaluationTrigger(evo, bus, enabled=False)
    await trig.start()
    assert not trig.is_active
    await evo.stop()


@pytest.mark.asyncio
async def test_burst_collapses_to_one_fire(tmp_path) -> None:
    """20 verdicts in a burst → debounce → exactly 1 evaluate() call."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
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
async def test_cooldown_blocks_second_fire_in_window(tmp_path) -> None:
    """Two bursts within cooldown_s → only first one fires."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
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
async def test_min_new_verdicts_threshold_blocks_tiny_bursts(tmp_path) -> None:
    """Bursts smaller than threshold don't fire."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
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
async def test_internal_session_skip(tmp_path) -> None:
    """Verdicts from evolution: / dream: / reflect: / skill-dream / _system
    sessions are skipped — the trigger only acts on real user-driven
    sessions to avoid recursion."""
    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)
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


# ── B-327: HEAD-context wiring + missing-registry visibility ────────


@pytest.mark.asyncio
async def test_b327_head_context_flows_through_registry(tmp_path, caplog) -> None:
    """B-327: when EvolutionAgent is constructed with ``registry=...``,
    ``evaluate()`` must look up HEAD via ``registry.active_version()``
    so the controller can gate gap-vs-head + detect rollback. This
    test pins the wiring end-to-end: trigger fires → evaluate() runs
    → registry.active_version() is called for the skill_id seen in
    the verdict stream.

    Pre-B-296 the controller never saw HEAD, so candidates that didn't
    actually beat the incumbent could still get proposed. Pre-B-327
    there was no test confirming the wire reached production.
    """
    bus = InProcessEventBus()

    # Capture which skill_ids the registry was queried for.
    queried: list[str] = []

    class _StubRegistry:
        def active_version(self, skill_id: str) -> int | None:
            queried.append(skill_id)
            # Return ``1`` so the controller has a real HEAD to gate
            # against (the arm's version=0 in _verdict() will compete).
            return 1

    evo = EvolutionAgent(
        "test", bus, audit_dir=tmp_path, registry=_StubRegistry(),
    )
    await evo.start()
    trig = EvolutionEvaluationTrigger(
        evo, bus,
        debounce_s=0.05,
        cooldown_s=0.0,
        min_new_verdicts=1,
    )
    await trig.start()
    try:
        # One verdict for skill_x → trigger fires → evaluate() iterates
        # per-skill → registry.active_version("skill_x") gets called.
        await bus.publish(_verdict(skill_id="skill_x"))
        await asyncio.sleep(0.15)
        assert trig.fire_count == 1
        assert "skill_x" in queried, (
            "EvolutionAgent.evaluate() should have asked the registry "
            f"for HEAD; queried={queried!r}"
        )
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_b327_no_registry_logs_warning(tmp_path, caplog) -> None:
    """B-327: when wired without a registry, ``start()`` must log a
    WARNING that explains gap-vs-head + ROLLBACK are degraded. Pre-B-327
    this was silent — operators who hit the half-done state had no
    log breadcrumb pointing at the missing registry.
    """
    import logging as _logging

    bus = InProcessEventBus()
    evo = EvolutionAgent("test", bus, audit_dir=tmp_path)  # no registry
    await evo.start()
    trig = EvolutionEvaluationTrigger(evo, bus, debounce_s=0.05)
    try:
        with caplog.at_level(
            _logging.WARNING,
            logger="xmclaw.daemon.evolution_evaluation_trigger",
        ):
            await trig.start()
        msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno >= _logging.WARNING
        ]
        assert any("evolution_eval.no_registry" in m for m in msgs), (
            "expected a no_registry warning at start; got: "
            + repr(msgs)
        )
    finally:
        await trig.stop()
        await evo.stop()


@pytest.mark.asyncio
async def test_b327_registry_attached_no_warning(tmp_path, caplog) -> None:
    """Inverse of the above: when a registry IS wired, no warning
    fires (avoid log noise on the happy path)."""
    import logging as _logging

    bus = InProcessEventBus()

    class _StubRegistry:
        def active_version(self, skill_id: str) -> int | None:
            return 1

    evo = EvolutionAgent(
        "test", bus, audit_dir=tmp_path, registry=_StubRegistry(),
    )
    await evo.start()
    trig = EvolutionEvaluationTrigger(evo, bus, debounce_s=0.05)
    try:
        with caplog.at_level(
            _logging.WARNING,
            logger="xmclaw.daemon.evolution_evaluation_trigger",
        ):
            await trig.start()
        msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno >= _logging.WARNING
        ]
        assert not any("evolution_eval.no_registry" in m for m in msgs), (
            f"unexpected no_registry warning when registry IS wired: {msgs}"
        )
    finally:
        await trig.stop()
        await evo.stop()
