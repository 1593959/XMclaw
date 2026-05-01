"""B-164 — RealtimeEvolutionTrigger unit tests.

The trigger is pure timing logic on top of any object that exposes an
async ``run_once()``. We stub the dream object directly so the tests
don't depend on file-system scans (that's covered by
``test_v2_skill_dream.py``).

Pins:
  * One ``LLM_RESPONSE`` event → after debounce, ``run_once`` fires once.
  * Multiple events within the debounce window collapse to one fire.
  * Successive bursts within the cooldown window collapse to one fire.
  * Internal sessions (system / dream / evolution / reflect) are filtered.
  * ``start`` / ``stop`` are idempotent.
  * ``stop`` cancels in-flight debounce without raising.
  * ``run_once`` raising does not kill the subscription — next event
    still triggers a fresh fire.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.daemon.skill_dream import RealtimeEvolutionTrigger


# ── stub ──────────────────────────────────────────────────────────


class _StubDream:
    """Minimal stand-in for SkillDreamCycle. Records every run_once
    call so tests can assert call count without timing flakiness."""

    def __init__(self) -> None:
        self.calls = 0
        self.raise_next = False

    async def run_once(self) -> int:  # noqa: D401 — match real signature
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("simulated proposer crash")
        self.calls += 1
        return 0


def _llm_response(session_id: str = "sess-1"):
    return make_event(
        session_id=session_id, agent_id="a",
        type=EventType.LLM_RESPONSE,
        payload={"text": "ok"},
    )


# ── happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_event_triggers_one_run() -> None:
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.05, cooldown_s=0.0,
    )
    await rt.start()

    await bus.publish(_llm_response())
    # Generous slack so Windows scheduler + lock acquisition fit.
    for _ in range(20):
        if rt.fire_count == 1:
            break
        await asyncio.sleep(0.05)
    await bus.drain()

    assert rt.fire_count == 1
    assert dream.calls == 1
    await rt.stop()


@pytest.mark.asyncio
async def test_burst_collapses_via_debounce() -> None:
    """5 rapid events within debounce window → 1 fire."""
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.1, cooldown_s=0.0,
    )
    await rt.start()

    for _ in range(5):
        await bus.publish(_llm_response())
        await asyncio.sleep(0.01)
    # Wait long enough that debounce + run_once both completed.
    for _ in range(20):
        if rt.fire_count == 1:
            break
        await asyncio.sleep(0.05)
    await bus.drain()

    assert rt.fire_count == 1
    assert dream.calls == 1
    await rt.stop()


@pytest.mark.asyncio
async def test_cooldown_collapses_successive_bursts() -> None:
    """Two bursts inside cooldown_s → only first fires."""
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.05, cooldown_s=2.0,
    )
    await rt.start()

    # Burst 1: trigger fires once.
    await bus.publish(_llm_response())
    for _ in range(20):
        if rt.fire_count == 1:
            break
        await asyncio.sleep(0.05)
    assert rt.fire_count == 1

    # Burst 2 within cooldown → debounce fires but cooldown skips.
    await bus.publish(_llm_response())
    await asyncio.sleep(0.3)  # > debounce, < cooldown
    await bus.drain()

    assert rt.fire_count == 1, "cooldown should have suppressed second fire"
    assert dream.calls == 1
    await rt.stop()


# ── filtering ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("internal_sid", [
    "_system",
    "_system:foo",
    "skill-dream:default",
    "dream:abc",
    "evolution:run-1",
    "reflect:sess-1:1234",
])
async def test_internal_sessions_filtered(internal_sid: str) -> None:
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.05, cooldown_s=0.0,
    )
    await rt.start()

    await bus.publish(_llm_response(session_id=internal_sid))
    await asyncio.sleep(0.3)
    await bus.drain()

    assert rt.fire_count == 0
    assert dream.calls == 0
    await rt.stop()


# ── lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_start_no_op() -> None:
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.05, cooldown_s=0.0, enabled=False,
    )
    await rt.start()
    assert not rt.is_active

    await bus.publish(_llm_response())
    await asyncio.sleep(0.2)
    await bus.drain()

    assert rt.fire_count == 0
    await rt.stop()


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=3600.0,  # so long nothing fires in test
    )
    await rt.start()
    await rt.start()  # second call no-op
    assert rt.is_active

    await rt.stop()
    await rt.stop()  # second call no-op
    assert not rt.is_active


@pytest.mark.asyncio
async def test_stop_cancels_pending_debounce() -> None:
    """Stop while debounce is pending: timer must cancel cleanly,
    no error leakage, run_once must NOT have fired."""
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=10.0, cooldown_s=0.0,  # never fires in test
    )
    await rt.start()
    await bus.publish(_llm_response())
    await asyncio.sleep(0.05)  # let the debounce task spawn
    await rt.stop()  # must not raise
    assert rt.fire_count == 0
    assert dream.calls == 0


@pytest.mark.asyncio
async def test_run_once_failure_does_not_kill_subscription() -> None:
    """If run_once raises, the trigger keeps listening for next turn."""
    bus = InProcessEventBus()
    dream = _StubDream()
    rt = RealtimeEvolutionTrigger(
        dream, bus, debounce_s=0.05, cooldown_s=0.0,
    )
    await rt.start()

    # First burst: arm raise_next so run_once raises once.
    dream.raise_next = True
    await bus.publish(_llm_response())
    await asyncio.sleep(0.4)
    assert rt.fire_count == 0, "raise should suppress fire_count++"
    assert rt.is_active, "subscription must survive run_once failure"

    # Second burst: clean run.
    await bus.publish(_llm_response())
    for _ in range(20):
        if rt.fire_count == 1:
            break
        await asyncio.sleep(0.05)
    await bus.drain()

    assert rt.fire_count == 1
    assert dream.calls == 1
    await rt.stop()
