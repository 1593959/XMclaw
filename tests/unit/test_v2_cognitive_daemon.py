"""Unit tests for Jarvis Phase 6.7: CognitiveDaemon main loop.

ActionDispatcher tests live in ``tests/unit/test_v2_action_dispatcher.py``
since the dispatcher gained real routing (LLM / skill / tool / percept-wait)
in Phase 6 wiring follow-up B.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from xmclaw.cognition.cognitive_daemon import (
    CognitiveDaemon,
    CognitiveDaemonConfig,
)
from xmclaw.cognition.perception_bus import Percept, PerceptionBus
from xmclaw.core.bus.events import EventType


# ── helpers ────────────────────────────────────────────────────────────


def make_percept(
    *,
    pid: str | None = None,
    source: str = "ws",
    kind: str = "user_msg",
    content: str = "hello",
    suggested: float | None = 0.9,
) -> Percept:
    return Percept(
        id=pid or PerceptionBus.new_id(),
        source=source,  # type: ignore[arg-type]
        kind=kind,
        timestamp=time.time(),
        payload={"content": content},
        suggested_salience=suggested,
    )


class FakeAttention:
    """Returns a fixed list of percepts on each tick. ``raise_once`` lets
    a test inject a one-shot exception to verify error capture."""

    def __init__(
        self,
        responses: list[list[Percept]] | None = None,
        raise_once: Exception | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.raise_once = raise_once
        self.tick_calls = 0

    async def tick(self) -> list[Percept]:
        self.tick_calls += 1
        if self.raise_once is not None:
            exc = self.raise_once
            self.raise_once = None
            raise exc
        if not self.responses:
            return []
        return self.responses.pop(0)


class FakePlan:
    def __init__(self, plan_id: str = "p1", n_steps: int = 1) -> None:
        self.id = plan_id
        self.steps = tuple(
            type("S", (), {
                "id": f"s{i}",
                "action_kind": "llm_turn",
                "expected_outcome": f"outcome_{i}",
            })() for i in range(n_steps)
        )
        self.status = "draft"


class FakePlanner:
    def __init__(self, plan: Any | None = None, raises: Exception | None = None) -> None:
        self.plan_obj = plan if plan is not None else FakePlan()
        self.raises = raises
        self.plan_calls: list[Any] = []

    async def plan(self, goal: Any) -> Any:
        self.plan_calls.append(goal)
        if self.raises is not None:
            raise self.raises
        return self.plan_obj


class FakeReasoning:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reason(self, query: str, mode: str = "auto") -> Any:
        self.calls.append(query)
        return type("R", (), {
            "mode": "meta",
            "conclusion": "",
            "confidence": 0.5,
            "evidence": (),
            "suggested_goals": (),
            "metadata": {},
        })()


class FakeDispatcher:
    def __init__(self, raises: Exception | None = None) -> None:
        self.executed_plans: list[Any] = []
        self.raises = raises

    async def execute_plan(self, plan: Any) -> dict[str, Any]:
        self.executed_plans.append(plan)
        if self.raises is not None:
            raise self.raises
        return {"plan_id": getattr(plan, "id", None), "status": "completed", "step_results": []}

    async def execute_step(self, step: Any) -> dict[str, Any]:
        return {"step_id": getattr(step, "id", None), "ok": True}


class FakePolicy:
    def __init__(self, level: int, self_experiment: bool = False) -> None:
        self.level = level
        self.self_experiment_enabled = self_experiment


class FakeGoalGenerator:
    def __init__(
        self,
        goals: list[Any] | None = None,
        policy_level: int = 100,
        self_experiment: bool = False,
    ) -> None:
        self.goals = goals if goals is not None else [object(), object()]
        self._policy = FakePolicy(policy_level, self_experiment=self_experiment)
        self.calls = 0

    async def generate_all(self) -> list[Any]:
        self.calls += 1
        return list(self.goals)


class FakeExperimentLoop:
    def __init__(self) -> None:
        self.tick_calls = 0

    async def tick(self) -> bool:
        self.tick_calls += 1
        return True


# ── ActionDispatcher tests live in tests/unit/test_v2_action_dispatcher.py
# (the dispatcher's stub-contract tests moved there when the v0 stub was
# replaced with real routing in Phase 6 wiring follow-up B).


# ── CognitiveDaemonConfig ─────────────────────────────────────────────


def test_config_defaults_proactive() -> None:
    """2026-05-10 default flip: daemon now ships **opt-out**.
    enabled=True + autonomy_level=50 (suggest tier — proposes things
    for review, never auto-applies). Operator dials down to 0
    (observe) or up to 100 (execute) per their trust level."""
    cfg = CognitiveDaemonConfig()
    assert cfg.enabled is True
    assert cfg.autonomy_level == 50
    assert cfg.heartbeat_hz == 1.0
    assert cfg.action_threshold == 0.6
    assert cfg.top_k_focus == 7


def test_config_is_frozen() -> None:
    cfg = CognitiveDaemonConfig()
    with pytest.raises(Exception):  # frozen dataclasses raise FrozenInstanceError
        cfg.enabled = True  # type: ignore[misc]


# ── tick_once: empty bus ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_once_empty_bus_returns_zero_summary() -> None:
    bus = PerceptionBus()
    attention = FakeAttention(responses=[[]])
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=bus,
        attention=attention,
    )
    summary = await daemon.tick_once()
    assert summary["n_percepts"] == 0
    assert summary["n_actionable"] == 0
    assert summary["n_goals_spawned"] == 0
    assert summary["n_plans_executed"] == 0
    assert summary["ran_experiment"] is False
    assert summary["errors"] == []
    assert summary["tick"] == 1


# ── tick_once: high-salience percept end-to-end ───────────────────────


@pytest.mark.asyncio
async def test_tick_once_high_salience_calls_full_pipeline() -> None:
    p = make_percept(content="urgent task")
    attention = FakeAttention(responses=[[p]])
    reasoning = FakeReasoning()
    planner = FakePlanner()
    dispatcher = FakeDispatcher()

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=attention,
        reasoning=reasoning,
        planner=planner,
        dispatcher=dispatcher,
    )
    summary = await daemon.tick_once()

    assert summary["n_percepts"] == 1
    assert summary["n_plans_executed"] == 1
    assert summary["errors"] == []
    assert reasoning.calls == ["urgent task"]
    assert len(planner.plan_calls) == 1
    assert len(dispatcher.executed_plans) == 1


@pytest.mark.asyncio
async def test_tick_once_skips_dispatch_when_plan_empty() -> None:
    """Empty plan from planner → no dispatcher call, but tick still ok."""
    p = make_percept()
    planner = FakePlanner(plan=FakePlan(n_steps=0))
    dispatcher = FakeDispatcher()

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[p]]),
        planner=planner,
        dispatcher=dispatcher,
    )
    summary = await daemon.tick_once()
    assert summary["errors"] == []
    # Empty plan should not get dispatched.
    assert dispatcher.executed_plans == []


@pytest.mark.asyncio
async def test_tick_once_works_without_reasoning() -> None:
    """No reasoning engine → planner still runs."""
    p = make_percept()
    planner = FakePlanner()
    dispatcher = FakeDispatcher()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[p]]),
        planner=planner,
        dispatcher=dispatcher,
    )
    summary = await daemon.tick_once()
    assert summary["errors"] == []
    assert summary["n_plans_executed"] == 1


@pytest.mark.asyncio
async def test_tick_once_no_planner_no_dispatcher_still_ticks() -> None:
    """Without planner+dispatcher we still capture the actionable count
    (working memory was updated by AttentionFilter)."""
    p = make_percept()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[p]]),
    )
    summary = await daemon.tick_once()
    assert summary["n_percepts"] == 1
    # Without planner/dispatcher, n_plans_executed counts the
    # truncated-pipeline case as a successful reaction (the daemon
    # treats no planner as "nothing to do, that's fine").
    assert summary["errors"] == []


# ── tick_once: errors are captured, never raised ──────────────────────


@pytest.mark.asyncio
async def test_tick_once_attention_exception_logged_and_captured() -> None:
    attention = FakeAttention(responses=[], raise_once=RuntimeError("att-fail"))
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=attention,
    )
    summary = await daemon.tick_once()
    assert summary["n_percepts"] == 0
    assert any("att-fail" in e for e in summary["errors"])
    # Critically: did NOT raise.


@pytest.mark.asyncio
async def test_tick_once_planner_exception_does_not_break_other_percepts() -> None:
    p1 = make_percept(pid="p1", content="first")
    p2 = make_percept(pid="p2", content="second")
    planner = FakePlanner(raises=RuntimeError("plan-fail"))
    dispatcher = FakeDispatcher()

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[p1, p2]]),
        planner=planner,
        dispatcher=dispatcher,
    )
    summary = await daemon.tick_once()
    # The planner failure was internal to _react_to_percept and does
    # NOT bubble up — both percepts processed cleanly.
    assert summary["n_percepts"] == 2
    assert summary["n_plans_executed"] == 2
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_tick_once_dispatcher_exception_logged_not_raised() -> None:
    p = make_percept()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[p]]),
        planner=FakePlanner(),
        dispatcher=FakeDispatcher(raises=RuntimeError("dispatch-fail")),
    )
    summary = await daemon.tick_once()
    # Dispatcher swallowed internally — counted as a plan we tried.
    assert summary["n_plans_executed"] == 1
    assert summary["errors"] == []


# ── autonomy gating ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_generator_not_called_at_autonomy_zero() -> None:
    gen = FakeGoalGenerator(goals=[object()], policy_level=0)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(autonomy_level=0, goal_gen_every_n_ticks=1),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        goal_generator=gen,
    )
    summary = await daemon.tick_once()
    assert summary["n_goals_spawned"] == 0
    assert gen.calls == 0


@pytest.mark.asyncio
async def test_goal_generator_called_at_autonomy_full() -> None:
    gen = FakeGoalGenerator(goals=[object(), object(), object()], policy_level=100)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(autonomy_level=100, goal_gen_every_n_ticks=1),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        goal_generator=gen,
    )
    summary = await daemon.tick_once()
    assert summary["n_goals_spawned"] == 3
    assert gen.calls == 1


@pytest.mark.asyncio
async def test_goal_generator_only_fires_every_n_ticks() -> None:
    gen = FakeGoalGenerator(goals=[object()], policy_level=100)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(autonomy_level=100, goal_gen_every_n_ticks=3),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[], [], [], [], [], []]),
        goal_generator=gen,
    )
    for _ in range(6):
        await daemon.tick_once()
    # Ticks 3 and 6 fire → 2 calls.
    assert gen.calls == 2


# ── self-experiment gating ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_experiment_only_fires_when_policy_allows() -> None:
    loop = FakeExperimentLoop()
    gen = FakeGoalGenerator(goals=[], policy_level=100, self_experiment=True)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(
            autonomy_level=100,
            self_experiment_every_n_ticks=1,
            goal_gen_every_n_ticks=10_000,  # don't conflate with goal-gen
        ),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        goal_generator=gen,
        experiment_loop=loop,
    )
    summary = await daemon.tick_once()
    assert summary["ran_experiment"] is True
    assert loop.tick_calls == 1


@pytest.mark.asyncio
async def test_self_experiment_skipped_when_policy_disallows() -> None:
    loop = FakeExperimentLoop()
    gen = FakeGoalGenerator(goals=[], policy_level=50, self_experiment=False)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(
            autonomy_level=50, self_experiment_every_n_ticks=1,
        ),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        goal_generator=gen,
        experiment_loop=loop,
    )
    summary = await daemon.tick_once()
    assert summary["ran_experiment"] is False
    assert loop.tick_calls == 0


@pytest.mark.asyncio
async def test_self_experiment_falls_back_to_config_level_without_policy() -> None:
    """If goal_generator is None we read autonomy_level off the config."""
    loop = FakeExperimentLoop()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(
            autonomy_level=80, self_experiment_every_n_ticks=1,
        ),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        experiment_loop=loop,
    )
    summary = await daemon.tick_once()
    assert summary["ran_experiment"] is True


# ── frequency control helpers ────────────────────────────────────────


def test_should_spawn_goals_respects_n_ticks() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(goal_gen_every_n_ticks=5),
        bus=PerceptionBus(),
        attention=FakeAttention(),
        goal_generator=FakeGoalGenerator(),
    )
    assert daemon._should_spawn_goals(1) is False
    assert daemon._should_spawn_goals(4) is False
    assert daemon._should_spawn_goals(5) is True
    assert daemon._should_spawn_goals(10) is True
    assert daemon._should_spawn_goals(11) is False


def test_should_spawn_goals_false_without_generator() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(goal_gen_every_n_ticks=1),
        bus=PerceptionBus(),
        attention=FakeAttention(),
    )
    assert daemon._should_spawn_goals(1) is False


def test_should_run_experiment_respects_n_ticks_and_policy() -> None:
    gen = FakeGoalGenerator(goals=[], policy_level=100, self_experiment=True)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(self_experiment_every_n_ticks=3),
        bus=PerceptionBus(),
        attention=FakeAttention(),
        goal_generator=gen,
        experiment_loop=FakeExperimentLoop(),
    )
    assert daemon._should_run_experiment(2) is False
    assert daemon._should_run_experiment(3) is True
    assert daemon._should_run_experiment(6) is True


def test_should_run_experiment_false_when_policy_disallows() -> None:
    gen = FakeGoalGenerator(goals=[], policy_level=50, self_experiment=False)
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(self_experiment_every_n_ticks=1),
        bus=PerceptionBus(),
        attention=FakeAttention(),
        goal_generator=gen,
        experiment_loop=FakeExperimentLoop(),
    )
    assert daemon._should_run_experiment(1) is False


# ── lifecycle ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_lifecycle_within_timeout() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(heartbeat_hz=100.0),  # fast ticks
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]] * 50),
    )
    await daemon.start()
    assert daemon.is_running
    # Let it tick a few times.
    await asyncio.sleep(0.05)
    started_at = time.time()
    await daemon.stop(timeout_s=5.0)
    elapsed = time.time() - started_at
    assert elapsed < 5.0
    assert not daemon.is_running


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(heartbeat_hz=10.0),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]] * 50),
    )
    await daemon.start()
    task1 = daemon._task
    await daemon.start()  # second call no-ops.
    assert daemon._task is task1
    await daemon.stop()


@pytest.mark.asyncio
async def test_stop_when_not_running_is_noop() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(),
    )
    await daemon.stop()  # must not raise.


@pytest.mark.asyncio
async def test_run_never_raises_even_when_collaborator_is_pathological() -> None:
    """Background _run() must survive any exception any tick can throw."""
    class Detonator:
        async def tick(self) -> list[Percept]:
            raise RuntimeError("kaboom")

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(heartbeat_hz=50.0),
        bus=PerceptionBus(),
        attention=Detonator(),
    )
    await daemon.start()
    await asyncio.sleep(0.05)
    # If _run raised out, the task would be done with an exception.
    task = daemon._task
    assert task is not None
    assert not task.done() or task.cancelled()
    await daemon.stop()


# ── tick_count drives summary['tick'] ────────────────────────────────


@pytest.mark.asyncio
async def test_tick_count_increments_per_call() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]] * 5),
    )
    s1 = await daemon.tick_once()
    s2 = await daemon.tick_once()
    s3 = await daemon.tick_once()
    assert (s1["tick"], s2["tick"], s3["tick"]) == (1, 2, 3)
    assert daemon.tick_count == 3


# ── Sprint 3 #5: SkillProposer periodic trigger ──────────────────


class FakeSkillProposer:
    """Returns scripted proposals."""

    def __init__(self, proposals: list[Any] | None = None) -> None:
        self.proposals = list(proposals or [])
        self.call_count = 0

    async def propose(self) -> list[Any]:
        self.call_count += 1
        return list(self.proposals)


class FakeEventBus:
    """Records published events."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


@pytest.mark.asyncio
async def test_skill_proposer_tick_emits_events() -> None:
    """Every skill_propose_every_n_ticks, the daemon calls propose()
    and publishes SKILL_CANDIDATE_PROPOSED events to the event bus."""
    from xmclaw.core.evolution.proposer import ProposedSkill

    bus = PerceptionBus()
    event_bus = FakeEventBus()
    proposer = FakeSkillProposer([
        ProposedSkill(
            skill_id="auto-test-skill",
            title="Test Skill",
            description="A test skill",
            body="step 1\nstep 2\n",
            triggers=("test",),
            confidence=0.7,
            evidence=("sess-1",),
            source_pattern="test_pattern",
        ),
    ])

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(skill_propose_every_n_ticks=3),
        bus=bus,
        attention=FakeAttention(responses=[[]] * 6),
        skill_proposer=proposer,
        event_bus=event_bus,
    )

    # tick 1: no proposal (1 % 3 != 0)
    s1 = await daemon.tick_once()
    assert s1["n_skill_proposals"] == 0
    assert proposer.call_count == 0

    # tick 2: no proposal
    s2 = await daemon.tick_once()
    assert s2["n_skill_proposals"] == 0

    # tick 3: proposal fires
    s3 = await daemon.tick_once()
    assert s3["n_skill_proposals"] == 1
    assert proposer.call_count == 1
    skill_events = [
        e for e in event_bus.published
        if getattr(e, "type", None) == EventType.SKILL_CANDIDATE_PROPOSED
    ]
    assert len(skill_events) == 1
    ev = skill_events[0]
    assert ev.payload["decision"] == "propose"
    assert ev.payload["winner_candidate_id"] == "auto-test-skill"
    assert "draft" in ev.payload


@pytest.mark.asyncio
async def test_skill_proposer_no_event_bus_silently_skips() -> None:
    """When event_bus is None, proposals are generated but not published."""
    from xmclaw.core.evolution.proposer import ProposedSkill

    bus = PerceptionBus()
    proposer = FakeSkillProposer([
        ProposedSkill(
            skill_id="auto-test-skill",
            title="Test Skill",
            description="A test skill",
            body="step 1\n",
            triggers=("test",),
            confidence=0.7,
            evidence=("sess-1",),
            source_pattern="test_pattern",
        ),
    ])

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(skill_propose_every_n_ticks=1),
        bus=bus,
        attention=FakeAttention(responses=[[]]),
        skill_proposer=proposer,
        event_bus=None,
    )

    s = await daemon.tick_once()
    assert s["n_skill_proposals"] == 0
    assert proposer.call_count == 1


def test_should_propose_skills_false_when_no_proposer() -> None:
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(skill_propose_every_n_ticks=1),
        bus=PerceptionBus(),
        attention=FakeAttention(),
        skill_proposer=None,
    )
    assert daemon._should_propose_skills(1) is False


def test_should_propose_skills_respects_interval() -> None:
    proposer = FakeSkillProposer()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(skill_propose_every_n_ticks=5),
        bus=PerceptionBus(),
        attention=FakeAttention(),
        skill_proposer=proposer,
    )
    assert daemon._should_propose_skills(1) is False
    assert daemon._should_propose_skills(5) is True
    assert daemon._should_propose_skills(10) is True


@pytest.mark.asyncio
async def test_skill_proposer_exception_captured_in_summary() -> None:
    """A crashing skill_proposer must not crash the tick."""
    class BrokenProposer:
        async def propose(self) -> list[Any]:
            raise RuntimeError("proposer kaboom")

    bus = PerceptionBus()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(skill_propose_every_n_ticks=1),
        bus=bus,
        attention=FakeAttention(responses=[[]]),
        skill_proposer=BrokenProposer(),
        event_bus=FakeEventBus(),
    )

    s = await daemon.tick_once()
    assert s["n_skill_proposals"] == 0
    assert any("proposer kaboom" in err for err in s["errors"])


# ── tick event publishing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_event_published_to_event_bus() -> None:
    """After a NON-IDLE tick, a COGNITIVE_DAEMON_TICK event lands on
    the wired event_bus. Idle-tick suppression is covered by the
    sibling test below."""
    from xmclaw.core.bus.events import EventType

    bus = FakeEventBus()
    # 2026-05-18: feed one percept so the tick produces actionable
    # signal and trips the "interesting" gate that suppresses idle
    # ticks (cognitive_daemon.py:_run_tick). Without this the bus
    # would (correctly) see zero published events.
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[make_percept(pid="p1")]]),
        event_bus=bus,
    )
    await daemon.tick_once()
    tick_events = [
        e for e in bus.published
        if getattr(e, "type", None) == EventType.COGNITIVE_DAEMON_TICK
    ]
    assert len(tick_events) == 1
    payload = tick_events[0].payload
    assert "tick" in payload
    assert "timestamp" in payload


@pytest.mark.asyncio
async def test_idle_tick_does_not_publish_to_event_bus() -> None:
    """2026-05-18: when a tick has zero percepts / plans / reflections
    / experiments / errors, it must NOT publish to the event bus.
    Suppression keeps events.db from accumulating 86400 noise rows/day
    on a quiet machine. The TickStore (ticks.db) still gets the row
    via its own ``save`` call — that's the canonical trend source."""
    from xmclaw.core.bus.events import EventType

    bus = FakeEventBus()
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        event_bus=bus,
    )
    summary = await daemon.tick_once()
    # The tick itself ran (returned summary), just no event surfaced.
    assert summary["tick"] == 1
    assert summary["n_percepts"] == 0
    tick_events = [
        e for e in bus.published
        if getattr(e, "type", None) == EventType.COGNITIVE_DAEMON_TICK
    ]
    assert tick_events == []


@pytest.mark.asyncio
async def test_tick_event_not_published_when_bus_is_none() -> None:
    """If no event_bus is wired, tick runs silently without crashing."""
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        event_bus=None,
    )
    summary = await daemon.tick_once()
    assert summary["tick"] == 1


# ── Phase D: latency monitoring ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_summary_contains_latency_ms() -> None:
    """Every tick must record per-subsystem latency breakdown."""
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
    )
    summary = await daemon.tick_once()
    assert "latency_ms" in summary
    assert isinstance(summary["latency_ms"], dict)
    # Attention always runs; its latency must be recorded.
    assert "attention" in summary["latency_ms"]
    assert summary["latency_ms"]["attention"] >= 0.0


@pytest.mark.asyncio
async def test_slow_subsystem_warning_in_errors() -> None:
    """A subsystem that exceeds the threshold produces a warning in
    ``errors`` without crashing the tick."""
    class SlowAttention:
        async def tick(self):
            await asyncio.sleep(0.06)  # 60 ms > 1 ms threshold
            return []

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(slow_subsystem_threshold_ms=1.0),
        bus=PerceptionBus(),
        attention=SlowAttention(),
    )
    summary = await daemon.tick_once()
    assert any(
        "slow_subsystem: attention=" in err for err in summary["errors"]
    )
    assert summary["latency_ms"]["attention"] >= 50.0


@pytest.mark.asyncio
async def test_last_tick_summary_stashed_on_daemon() -> None:
    """After tick_once returns, the summary is available on
    ``_last_tick_summary`` for the /daemon endpoint to read."""
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
    )
    summary = await daemon.tick_once()
    assert getattr(daemon, "_last_tick_summary", None) is summary


@pytest.mark.asyncio
async def test_tick_saved_to_tick_store(tmp_path) -> None:
    """When a TickStore is wired, tick summaries are persisted."""
    from xmclaw.cognition.tick_store import TickStore

    store = TickStore(db_path=tmp_path / "ticks.db")
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(),
        bus=PerceptionBus(),
        attention=FakeAttention(responses=[[]]),
        tick_store=store,
    )
    await daemon.tick_once()
    rows = await store.list_ticks(limit=1)
    assert len(rows) == 1
    assert rows[0]["tick"] == 1
    assert "latency_ms" in rows[0]


# ── Phase E: config hot-reload + graceful shutdown ───────────────────


@pytest.mark.asyncio
async def test_update_config_replaces_config_at_runtime() -> None:
    """update_config() swaps the frozen CognitiveDaemonConfig."""
    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(autonomy_level=50, heartbeat_hz=1.0),
        bus=PerceptionBus(),
        attention=FakeAttention(),
    )
    assert daemon.config.autonomy_level == 50
    assert daemon.config.heartbeat_hz == 1.0

    daemon.update_config(
        CognitiveDaemonConfig(autonomy_level=75, heartbeat_hz=2.0)
    )
    assert daemon.config.autonomy_level == 75
    assert daemon.config.heartbeat_hz == 2.0


@pytest.mark.asyncio
async def test_stop_graceful_waits_for_tick_to_finish() -> None:
    """stop() lets the in-flight tick complete before returning."""
    tick_started = asyncio.Event()
    tick_finished = asyncio.Event()

    class SlowAttention:
        async def tick(self) -> list[Percept]:
            tick_started.set()
            await asyncio.sleep(0.2)
            tick_finished.set()
            return []

    daemon = CognitiveDaemon(
        config=CognitiveDaemonConfig(heartbeat_hz=100.0),
        bus=PerceptionBus(),
        attention=SlowAttention(),
    )
    await daemon.start()
    await tick_started.wait()
    # Tick is now in-flight (sleeping 0.2s). stop() should wait for it.
    t0 = time.time()
    await daemon.stop(timeout_s=5.0)
    elapsed = time.time() - t0
    assert tick_finished.is_set(), "tick did not finish before stop returned"
    assert elapsed >= 0.15, f"stop returned too quickly ({elapsed:.3f}s)"
    assert not daemon.is_running


# ── Epic #27 sweep #12 (2026-05-19): slow_subsystem self-heal ─────


class _SlowReflectionCycle:
    """Reflection cycle that takes ``delay_s`` per run — used to
    deliberately exceed the threshold + trigger the cooldown."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.runs = 0

    async def run_due(self, _tick: int) -> list:
        self.runs += 1
        await asyncio.sleep(self.delay_s)
        return []


@pytest.mark.asyncio
async def test_slow_subsystem_enters_cooldown_after_strike_threshold() -> None:
    """After 3 consecutive slow reflection ticks, the daemon puts
    ``reflection`` into a cooldown — subsequent ticks skip it
    until the cooldown elapses. Pre-fix the daemon just emitted
    a warning and kept invoking the slow subsystem on every tick,
    eating heartbeat time forever."""
    slow = _SlowReflectionCycle(delay_s=0.05)  # 50ms
    cfg = CognitiveDaemonConfig(
        heartbeat_hz=100.0,
        slow_subsystem_thresholds={"reflection": 20.0},  # 20ms threshold
    )
    daemon = CognitiveDaemon(
        config=cfg,
        bus=PerceptionBus(),
        attention=FakeAttention(),
        reflection_cycle=slow,
    )
    # 3 ticks: each one runs reflection + breaches threshold.
    for _ in range(3):
        await daemon.tick_once()
    assert slow.runs == 3
    assert "reflection" in daemon._cooldown_until_tick

    # 4th + 5th tick: reflection should NOT run (cooldown active).
    await daemon.tick_once()
    await daemon.tick_once()
    assert slow.runs == 3, (
        f"reflection ran during cooldown (runs={slow.runs}, "
        f"cooldown_until={daemon._cooldown_until_tick.get('reflection')})"
    )


@pytest.mark.asyncio
async def test_slow_subsystem_strikes_reset_on_healthy_tick() -> None:
    """A normal-latency tick between slow ones resets the strike
    counter — interleaved slowness doesn't open the cooldown."""
    delays = [0.05, 0.001, 0.05, 0.001, 0.05]  # slow, fast, slow, fast, slow

    class _FlickeringRefl:
        def __init__(self) -> None:
            self.idx = 0
            self.runs = 0

        async def run_due(self, _tick: int) -> list:
            self.runs += 1
            d = delays[self.idx % len(delays)]
            self.idx += 1
            await asyncio.sleep(d)
            return []

    flicker = _FlickeringRefl()
    cfg = CognitiveDaemonConfig(
        heartbeat_hz=100.0,
        slow_subsystem_thresholds={"reflection": 20.0},
    )
    daemon = CognitiveDaemon(
        config=cfg,
        bus=PerceptionBus(),
        attention=FakeAttention(),
        reflection_cycle=flicker,
    )
    for _ in range(5):
        await daemon.tick_once()
    # Despite 3 slow ticks total, strikes never reached 3 consecutively
    # (the fast ticks reset). Cooldown should NOT be active.
    assert "reflection" not in daemon._cooldown_until_tick
    assert flicker.runs == 5  # all 5 invoked, none skipped
