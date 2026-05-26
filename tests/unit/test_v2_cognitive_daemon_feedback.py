"""CognitiveDaemon feedback closure — Wave-32+ (2026-05-18).

Pins the four feedback loops the user explicitly asked for after
seeing autonomous sessions burn LLM credits with no consumer of
their results:

  * P0 — Goal status update on execute_plan return + bounded retry
  * P1 — Result surfacing as proactive_proposal (LLM-gated)
  * P2 — SESSION_LIFECYCLE destroy emit so journal picks up
  * P3 — Recent autonomous outputs visible in main agent's system
        prompt (covered separately by test_v2_agent_loop_*.py)

Tests stub the dispatcher / event bus / state so we exercise the
feedback logic in isolation, not the whole 6-component dance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.cognition.cognitive_daemon import CognitiveDaemon, CognitiveDaemonConfig
from xmclaw.cognition.state import CognitiveState, Goal


# ── lightweight stubs ────────────────────────────────────────────────────


@dataclass
class _StepResult:
    step_id: str
    route: str
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class _PlanResult:
    plan_id: str
    step_results: tuple
    all_ok: bool
    error: str | None = None


class _StubBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


# ── helper builders ─────────────────────────────────────────────────────


def _make_daemon(state: CognitiveState | None = None, *, bus: _StubBus | None = None) -> CognitiveDaemon:
    """Build a minimal CognitiveDaemon that only exercises the
    feedback paths — no planner / dispatcher needed."""
    return CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=None,
        attention=None,
        cognitive_state=state,
        dispatcher=None,
        event_bus=bus,
        planner=None,
    )


# ── P0: goal state machine ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_marked_completed_on_all_ok() -> None:
    state = CognitiveState()
    goal = Goal(id="g1", description="research X")
    state.add_goal(goal)
    daemon = _make_daemon(state)
    await daemon._react_to_exec_result(
        {"id": "g1", "description": "research X"},
        _PlanResult(plan_id="p1", step_results=(), all_ok=True),
    )
    assert goal.status == "completed"


@pytest.mark.asyncio
async def test_goal_marked_needs_replan_on_first_failure() -> None:
    state = CognitiveState()
    goal = Goal(id="g2", description="x")
    state.add_goal(goal)
    daemon = _make_daemon(state)
    await daemon._react_to_exec_result(
        {"id": "g2", "description": "x"},
        _PlanResult(plan_id="p2", step_results=(), all_ok=False),
    )
    assert goal.status == "needs_replan"
    assert daemon._failed_goal_attempts["g2"] == 1


@pytest.mark.asyncio
async def test_goal_blocked_after_max_retries() -> None:
    """The third failure (default _max_goal_retries=2) flips status to
    blocked so the next tick stops re-trying. Pin the bound."""
    state = CognitiveState()
    goal = Goal(id="g3", description="x")
    state.add_goal(goal)
    daemon = _make_daemon(state)
    for _ in range(daemon._max_goal_retries):
        await daemon._react_to_exec_result(
            {"id": "g3", "description": "x"},
            _PlanResult(plan_id="p3", step_results=(), all_ok=False),
        )
    assert goal.status == "blocked"
    assert daemon._failed_goal_attempts["g3"] >= daemon._max_goal_retries


@pytest.mark.asyncio
async def test_goal_completion_resets_failure_counter() -> None:
    """A goal that finally succeeds drops its attempts counter so a
    LATER failure of the same id starts fresh (not "already blocked")."""
    state = CognitiveState()
    goal = Goal(id="g4", description="x")
    state.add_goal(goal)
    daemon = _make_daemon(state)
    await daemon._react_to_exec_result(
        {"id": "g4", "description": "x"},
        _PlanResult(plan_id="p4", step_results=(), all_ok=False),
    )
    assert daemon._failed_goal_attempts.get("g4") == 1
    await daemon._react_to_exec_result(
        {"id": "g4", "description": "x"},
        _PlanResult(plan_id="p5", step_results=(), all_ok=True),
    )
    assert goal.status == "completed"
    assert "g4" not in daemon._failed_goal_attempts


# ── P2: SESSION_LIFECYCLE destroy emission ──────────────────────────────


@pytest.mark.asyncio
async def test_session_destroy_emitted_for_llm_turn_steps() -> None:
    """Each llm_turn step with a session_id triggers SESSION_LIFECYCLE
    destroy on the bus → JournalWriter buffers it → skill_dream sees
    autonomous-session outcomes. Pre-fix this loop was broken because
    autonomous sessions never hit the WS-bound destroy event."""
    bus = _StubBus()
    daemon = _make_daemon(bus=bus)
    steps = (
        _StepResult(step_id="s1", route="llm_turn", ok=True, output={"session_id": "sess-a"}),
        _StepResult(step_id="s2", route="skill_invoke", ok=True, output={"skill_id": "x"}),
        _StepResult(step_id="s3", route="llm_turn", ok=True, output={"session_id": "sess-b"}),
    )
    await daemon._react_to_exec_result(
        {"id": "g1"},
        _PlanResult(plan_id="p1", step_results=steps, all_ok=True),
    )
    sids_destroyed = []
    for ev in bus.published:
        if getattr(ev, "type", None) and "SESSION_LIFECYCLE" in str(ev.type):
            payload = getattr(ev, "payload", {}) or {}
            if payload.get("phase") == "destroy":
                sids_destroyed.append(ev.session_id)
    assert "sess-a" in sids_destroyed
    assert "sess-b" in sids_destroyed
    # skill_invoke step has no session_id, no destroy emitted.
    assert len(sids_destroyed) == 2


@pytest.mark.asyncio
async def test_no_destroy_when_event_bus_missing() -> None:
    """The destroy emit must fail-soft when no bus is wired — test
    contexts shouldn't crash."""
    daemon = _make_daemon(bus=None)
    # Should not raise.
    await daemon._react_to_exec_result(
        {"id": "g1"},
        _PlanResult(
            plan_id="p1",
            step_results=(_StepResult(step_id="s1", route="llm_turn", ok=True, output={"session_id": "x"}),),
            all_ok=True,
        ),
    )


# ── P1: result surfacing (flag-gated) ────────────────────────────────────


@pytest.mark.asyncio
async def test_surface_results_skipped_when_flag_off() -> None:
    """Default feature flag is OFF — no LLM call, no proactive_proposal
    event. Pin the gate so an out-of-box install doesn't burn judging
    calls."""
    bus = _StubBus()
    daemon = _make_daemon(bus=bus)
    steps = (
        _StepResult(step_id="s1", route="llm_turn", ok=True, output={
            "session_id": "sess-a",
            "agent_result": {"text": "Found 3 important patterns!"},
        }),
    )
    await daemon._react_to_exec_result(
        {"id": "g1", "description": "research"},
        _PlanResult(plan_id="p1", step_results=steps, all_ok=True),
    )
    proactive_events = [
        ev for ev in bus.published
        if "PROACTIVE_PROPOSAL" in str(getattr(ev, "type", ""))
    ]
    assert proactive_events == []


# 2026-05-26 (audit B2): session_flags cap regression


def test_session_flags_evict_when_cap_hit() -> None:
    """``session_flags`` used to grow unbounded; after hundreds of
    sessions ``cognitive_state.json`` bloated with stale entries.
    Now we cap at ``SESSION_FLAGS_CAP`` and evict the oldest 25%
    when crossing the line."""
    state = CognitiveState()
    cap = state.SESSION_FLAGS_CAP
    # Fill exactly to cap → no eviction yet.
    for i in range(cap):
        state.set_session_flag(f"sid-{i:05d}", "k", "v")
    assert len(state.session_flags) == cap
    # One more → eviction fires (drops oldest 25%).
    state.set_session_flag("sid-final", "k", "v")
    assert len(state.session_flags) <= cap
    # The newest entry survived.
    assert "sid-final" in state.session_flags
    # Some of the oldest entries are gone.
    assert "sid-00000" not in state.session_flags
