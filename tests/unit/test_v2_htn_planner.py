"""Unit tests for Jarvis Phase 6.3: HTN Planner.

Covers the dataclass shape contracts, the goal->plan flow with mocked
LLM + skill registry, cycle detection, topological execution, retry
budget, repair-then-second-failure semantics, the confidence cap, and
dispatcher routing per action_kind. All collaborators are duck-typed
fakes — this module never touches ``providers/`` or ``daemon/``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.cognition.planner import (
    Plan,
    PlanResult,
    PlanStep,
    PlanStepFailure,
    Planner,
)


# -------------------------------------------------------------------- fakes


class FakeLLM:
    """Minimal LLM duck. Returns canned JSON strings in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def complete(
        self, prompt: str, *, response_format: str = "json"
    ) -> str:
        self.calls.append(prompt)
        if not self._responses:
            return "{}"
        nxt = self._responses.pop(0)
        if isinstance(nxt, str):
            return nxt
        return json.dumps(nxt)


@dataclass
class FakeSkill:
    id: str
    name: str


class FakeSkillRegistry:
    """`find` returns a skill if intent matches one in the map."""

    def __init__(self, mapping: dict[str, FakeSkill]) -> None:
        self._mapping = mapping
        self.find_calls: list[str] = []

    def find(self, intent: str) -> FakeSkill | None:
        self.find_calls.append(intent)
        # Substring match — realistic enough for tests.
        for needle, skill in self._mapping.items():
            if needle in intent:
                return skill
        return None


class FakeReasoningEngine:
    def __init__(self, analogical_result: str = "previous: timeout") -> None:
        self.analogical_result = analogical_result
        self.analogical_calls: list[str] = []

    async def analogical(self, query: str) -> str:
        self.analogical_calls.append(query)
        return self.analogical_result


class FakeDispatcher:
    """Recordable dispatcher; can be configured to raise on specific steps."""

    def __init__(
        self,
        fail_on: dict[str, int] | None = None,
        fail_action_kinds: set[str] | None = None,
    ) -> None:
        # fail_on: {step_id: how_many_times_to_fail_before_succeeding}
        self.fail_on = dict(fail_on) if fail_on else {}
        self.fail_action_kinds = fail_action_kinds or set()
        self.dispatched: list[PlanStep] = []

    async def dispatch(self, step: PlanStep) -> dict[str, Any]:
        self.dispatched.append(step)
        if step.action_kind in self.fail_action_kinds:
            raise RuntimeError(f"action_kind {step.action_kind} forbidden")
        remaining = self.fail_on.get(step.id, 0)
        if remaining > 0:
            self.fail_on[step.id] = remaining - 1
            raise RuntimeError(f"transient failure on {step.id}")
        return {
            "step_id": step.id,
            "action_kind": step.action_kind,
            "ok": True,
        }


@dataclass
class FakeGoal:
    id: str
    name: str
    description: str
    priority: int = 5
    completion_criteria: dict | None = None


def _two_step_response(*, with_skill: bool = False) -> dict:
    intent_a = "search files for pattern" if with_skill else "draft outline"
    intent_b = "summarize results"
    return {
        "steps": [
            {
                "id": "s1",
                "intent": intent_a,
                "action_kind": "llm_turn",
                "depends_on": [],
                "expected_outcome": "got results",
            },
            {
                "id": "s2",
                "intent": intent_b,
                "action_kind": "llm_turn",
                "depends_on": ["s1"],
                "expected_outcome": "summary written",
            },
        ],
        "confidence": 0.7,
    }


# ----------------------------------------------------------- dataclass shape


def test_planstep_defaults_match_spec() -> None:
    s = PlanStep(id="x", action_kind="llm_turn", payload={"a": 1})
    assert s.depends_on == ()
    assert s.expected_outcome == ""
    assert s.retry_policy == {"max_retries": 2, "backoff_s": 1.0}


def test_plan_defaults_match_spec() -> None:
    p = Plan(id="p", goal_id="g", steps=())
    assert p.status == "draft"
    assert p.confidence == 0.5
    assert p.created_at == 0.0


def test_planresult_shape() -> None:
    r = PlanResult(plan_id="p", status="completed", step_results=())
    assert r.error is None
    assert r.step_results == ()


def test_planstepfailure_default_output() -> None:
    f = PlanStepFailure(step_id="s1", reason="boom")
    assert f.step_output == {}


# ----------------------------------------------------------------- plan()


@pytest.mark.asyncio
async def test_plan_returns_steps_on_valid_llm_json() -> None:
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm)
    goal = FakeGoal(id="g1", name="write doc", description="d")
    plan = await planner.plan(goal)
    assert plan.status == "draft"
    assert plan.goal_id == "g1"
    assert len(plan.steps) == 2
    assert plan.steps[0].id == "s1"
    assert plan.steps[1].depends_on == ("s1",)


@pytest.mark.asyncio
async def test_plan_handles_malformed_llm_output() -> None:
    llm = FakeLLM(["not json at all <<<"])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.status == "failed"
    assert plan.steps == ()
    assert plan.confidence == 0.0


@pytest.mark.asyncio
async def test_plan_handles_empty_steps_list() -> None:
    llm = FakeLLM([{"steps": [], "confidence": 0.9}])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.status == "failed"
    assert plan.steps == ()


@pytest.mark.asyncio
async def test_plan_strips_json_fences() -> None:
    fenced = "```json\n" + json.dumps(_two_step_response()) + "\n```"
    llm = FakeLLM([fenced])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.status == "draft"
    assert len(plan.steps) == 2


@pytest.mark.asyncio
async def test_plan_prefers_skill_invoke_when_registry_matches() -> None:
    skill = FakeSkill(id="skill_search", name="file_search")
    registry = FakeSkillRegistry({"search files": skill})
    llm = FakeLLM([_two_step_response(with_skill=True)])
    planner = Planner(llm=llm, skill_registry=registry)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.steps[0].action_kind == "skill_invoke"
    assert plan.steps[0].payload["skill_id"] == "skill_search"
    # Second step has no matching skill; falls through.
    assert plan.steps[1].action_kind == "llm_turn"


@pytest.mark.asyncio
async def test_plan_falls_back_to_llm_turn_when_no_skill() -> None:
    registry = FakeSkillRegistry({})  # no skills installed
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm, skill_registry=registry)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    for step in plan.steps:
        assert step.action_kind == "llm_turn"


@pytest.mark.asyncio
async def test_plan_cycle_detection_raises_value_error() -> None:
    cyclic = {
        "steps": [
            {"id": "a", "intent": "a", "depends_on": []},
            {"id": "b", "intent": "b", "depends_on": ["a"]},
            {"id": "c", "intent": "c", "depends_on": ["b"]},
        ],
        "confidence": 0.5,
    }
    llm = FakeLLM([cyclic])
    planner = Planner(llm=llm)
    # depends_on are validated against already-materialised siblings.
    # To force a cycle we need to monkey-patch a step's depends_on
    # post-hoc. Easier: assert _has_cycle directly with a constructed
    # plan, then assert plan() raises when fed an actually cyclic
    # response (we hand-craft via repair-style monkey-patch).
    # Approach: check via direct construction since Planner only
    # accepts validated siblings.
    a = PlanStep(id="a", action_kind="llm_turn", payload={}, depends_on=("c",))
    b = PlanStep(id="b", action_kind="llm_turn", payload={}, depends_on=("a",))
    c = PlanStep(id="c", action_kind="llm_turn", payload={}, depends_on=("b",))
    cyclic_plan = Plan(id="p", goal_id="g", steps=(a, b, c))
    assert planner._has_cycle(cyclic_plan) is True
    with pytest.raises(ValueError):
        planner._topological_sort(cyclic_plan)
    # Sanity: above inline llm response is dropped (forward ref to "c"
    # is filtered during materialisation), so plan() returns valid plan
    # with no cycle — verifying the safety gate.
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert planner._has_cycle(plan) is False


@pytest.mark.asyncio
async def test_plan_confidence_capped() -> None:
    over = {
        "steps": [{"id": "x", "intent": "go", "depends_on": []}],
        "confidence": 0.99,
    }
    llm = FakeLLM([over])
    planner = Planner(llm=llm, confidence_cap=0.6)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.confidence <= 0.6


def test_planner_rejects_invalid_confidence_cap() -> None:
    with pytest.raises(ValueError):
        Planner(llm=object(), confidence_cap=0.0)
    with pytest.raises(ValueError):
        Planner(llm=object(), confidence_cap=1.5)


# --------------------------------------------------------------- execute()


@pytest.mark.asyncio
async def test_execute_runs_in_topological_order() -> None:
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    dispatcher = FakeDispatcher()
    result = await planner.execute(plan, dispatcher)
    assert result.status == "completed"
    assert [s.id for s in dispatcher.dispatched] == ["s1", "s2"]
    assert len(result.step_results) == 2


@pytest.mark.asyncio
async def test_execute_retries_failed_step_per_policy() -> None:
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    # s1 fails twice (within budget of max_retries=2), succeeds on 3rd.
    dispatcher = FakeDispatcher(fail_on={"s1": 2})
    # Patch backoff to 0 for fast test.
    fast_steps = tuple(
        PlanStep(
            id=s.id,
            action_kind=s.action_kind,
            payload=s.payload,
            depends_on=s.depends_on,
            expected_outcome=s.expected_outcome,
            retry_policy={"max_retries": 2, "backoff_s": 0.0},
        )
        for s in plan.steps
    )
    fast_plan = Plan(
        id=plan.id,
        goal_id=plan.goal_id,
        steps=fast_steps,
        status=plan.status,
        confidence=plan.confidence,
        created_at=plan.created_at,
    )
    result = await planner.execute(fast_plan, dispatcher)
    assert result.status == "completed"
    # 3 attempts on s1 + 1 on s2 = 4 dispatches total.
    assert len(dispatcher.dispatched) == 4


@pytest.mark.asyncio
async def test_execute_calls_repair_after_retry_budget_exhausted() -> None:
    repair_response = {
        "steps": [
            {"id": "r1", "intent": "alt path", "depends_on": []},
        ],
        "confidence": 0.5,
    }
    llm = FakeLLM([_two_step_response(), repair_response])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    fast_steps = tuple(
        PlanStep(
            id=s.id,
            action_kind=s.action_kind,
            payload=s.payload,
            depends_on=s.depends_on,
            expected_outcome=s.expected_outcome,
            retry_policy={"max_retries": 1, "backoff_s": 0.0},
        )
        for s in plan.steps
    )
    fast_plan = Plan(
        id=plan.id, goal_id=plan.goal_id, steps=fast_steps,
        status="draft", confidence=plan.confidence, created_at=0.0,
    )
    # s1 fails forever. After 1 initial + 1 retry = 2 attempts, repair fires.
    dispatcher = FakeDispatcher(fail_on={"s1": 999})
    result = await planner.execute(fast_plan, dispatcher)
    # Repair produced a fresh plan with r1; r1 succeeds → status="repaired".
    assert result.status == "repaired"
    assert any(s.id == "r1" for s in dispatcher.dispatched)


@pytest.mark.asyncio
async def test_execute_repair_runs_only_once() -> None:
    repair_response = {
        "steps": [
            {"id": "r1", "intent": "alt path", "depends_on": []},
        ],
        "confidence": 0.5,
    }
    # Two LLM calls expected: original plan + one repair. NO third.
    llm = FakeLLM([_two_step_response(), repair_response])
    planner = Planner(llm=llm)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    fast_steps = tuple(
        PlanStep(
            id=s.id,
            action_kind=s.action_kind,
            payload=s.payload,
            depends_on=s.depends_on,
            expected_outcome=s.expected_outcome,
            retry_policy={"max_retries": 0, "backoff_s": 0.0},
        )
        for s in plan.steps
    )
    fast_plan = Plan(
        id=plan.id, goal_id=plan.goal_id, steps=fast_steps,
        status="draft", confidence=plan.confidence, created_at=0.0,
    )
    # Both s1 (original) and r1 (repaired) fail forever — second
    # failure must be terminal, not trigger a second repair.
    dispatcher = FakeDispatcher(fail_on={"s1": 999, "r1": 999})
    result = await planner.execute(fast_plan, dispatcher)
    assert result.status == "failed"
    # Verify only ONE repair LLM call (= 2 total LLM calls: plan + repair).
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_execute_failed_plan_returns_failed_immediately() -> None:
    llm = FakeLLM([])
    planner = Planner(llm=llm)
    failed = Plan(id="p", goal_id="g", steps=(), status="failed")
    result = await planner.execute(failed, FakeDispatcher())
    assert result.status == "failed"
    assert result.step_results == ()


# --------------------------------------------------------- dispatcher kinds


@pytest.mark.asyncio
async def test_each_action_kind_dispatches_correctly() -> None:
    skill = FakeSkill(id="sk_files", name="file_search")
    registry = FakeSkillRegistry({"search files": skill})
    response = {
        "steps": [
            {"id": "a", "intent": "search files", "depends_on": []},
            {
                "id": "b",
                "intent": "raw llm reasoning",
                "action_kind": "llm_turn",
                "depends_on": ["a"],
            },
            {
                "id": "c",
                "intent": "call calculator",
                "action_kind": "tool_call",
                "depends_on": ["b"],
            },
            {
                "id": "d",
                "intent": "wait_for_x",
                "action_kind": "wait_for_percept",
                "depends_on": ["c"],
            },
        ],
        "confidence": 0.5,
    }
    llm = FakeLLM([response])
    planner = Planner(llm=llm, skill_registry=registry)
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    kinds = [s.action_kind for s in plan.steps]
    assert kinds == ["skill_invoke", "llm_turn", "tool_call", "wait_for_percept"]

    dispatcher = FakeDispatcher()
    result = await planner.execute(plan, dispatcher)
    assert result.status == "completed"
    seen_kinds = [s.action_kind for s in dispatcher.dispatched]
    assert seen_kinds == ["skill_invoke", "llm_turn", "tool_call", "wait_for_percept"]


# ---------------------------------------------------------------- repair()


@pytest.mark.asyncio
async def test_repair_uses_reasoning_engine_when_provided() -> None:
    rebuild = {
        "steps": [{"id": "r1", "intent": "retry safer", "depends_on": []}],
        "confidence": 0.5,
    }
    llm = FakeLLM([rebuild])
    engine = FakeReasoningEngine(analogical_result="similar repair: shorter timeout")
    planner = Planner(llm=llm, reasoning_engine=engine)
    failed = Plan(
        id="p", goal_id="g",
        steps=(PlanStep(id="x", action_kind="llm_turn", payload={}),),
        status="failed",
    )
    failure = PlanStepFailure(step_id="x", reason="timeout")
    repaired = await planner.repair(failed, failure)
    assert repaired.status == "repaired"
    assert repaired.steps and repaired.steps[0].id == "r1"
    assert engine.analogical_calls, "reasoning engine MUST be consulted"
    assert "similar repair" in llm.calls[0]


@pytest.mark.asyncio
async def test_repair_degrades_gracefully_when_engine_raises() -> None:
    class BrokenEngine:
        async def analogical(self, _q: str) -> str:
            raise RuntimeError("engine offline")

    rebuild = {
        "steps": [{"id": "r1", "intent": "fallback", "depends_on": []}],
        "confidence": 0.5,
    }
    llm = FakeLLM([rebuild])
    planner = Planner(llm=llm, reasoning_engine=BrokenEngine())
    failed = Plan(id="p", goal_id="g", steps=(), status="failed")
    failure = PlanStepFailure(step_id="x", reason="boom")
    repaired = await planner.repair(failed, failure)
    assert repaired.steps and repaired.steps[0].id == "r1"


@pytest.mark.asyncio
async def test_repair_returns_failed_plan_when_llm_garbage() -> None:
    llm = FakeLLM(["not json"])
    planner = Planner(llm=llm)
    failed = Plan(id="p", goal_id="g", steps=(), status="failed")
    failure = PlanStepFailure(step_id="x", reason="any")
    repaired = await planner.repair(failed, failure)
    assert repaired.status == "failed"
    assert repaired.steps == ()


# ------------------------------------------------------------ topology unit


def test_topological_sort_orders_dependencies() -> None:
    a = PlanStep(id="a", action_kind="llm_turn", payload={})
    b = PlanStep(id="b", action_kind="llm_turn", payload={}, depends_on=("a",))
    c = PlanStep(id="c", action_kind="llm_turn", payload={}, depends_on=("b",))
    plan = Plan(id="p", goal_id="g", steps=(c, a, b))  # out of order
    planner = Planner(llm=object())
    ordered = planner._topological_sort(plan)
    ids = [s.id for s in ordered]
    # a must come before b before c regardless of input order.
    assert ids.index("a") < ids.index("b") < ids.index("c")


def test_has_cycle_returns_false_on_acyclic() -> None:
    a = PlanStep(id="a", action_kind="llm_turn", payload={})
    b = PlanStep(id="b", action_kind="llm_turn", payload={}, depends_on=("a",))
    plan = Plan(id="p", goal_id="g", steps=(a, b))
    assert Planner(llm=object())._has_cycle(plan) is False


def test_has_cycle_handles_empty_plan() -> None:
    plan = Plan(id="p", goal_id="g", steps=())
    assert Planner(llm=object())._has_cycle(plan) is False


# ----------------------------------------------------------- goal coercion


@pytest.mark.asyncio
async def test_plan_accepts_dict_goal() -> None:
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm)
    plan = await planner.plan({"id": "dict_goal", "description": "do thing"})
    assert plan.goal_id == "dict_goal"


@pytest.mark.asyncio
async def test_plan_synthesises_goal_id_when_missing() -> None:
    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm)

    class GoalLike:
        name = "n"
        description = "d"

    plan = await planner.plan(GoalLike())
    assert plan.goal_id  # auto-generated, non-empty


@pytest.mark.asyncio
async def test_plan_failed_when_llm_raises() -> None:
    class ExplodingLLM:
        async def complete(self, _prompt: str, **_kw: Any) -> str:
            raise RuntimeError("network down")

    planner = Planner(llm=ExplodingLLM())
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    assert plan.status == "failed"
    assert plan.steps == ()


@pytest.mark.asyncio
async def test_skill_registry_failure_falls_back_to_llm_turn() -> None:
    class ExplodingRegistry:
        def find(self, _intent: str) -> Any:
            raise RuntimeError("registry corrupt")

    llm = FakeLLM([_two_step_response()])
    planner = Planner(llm=llm, skill_registry=ExplodingRegistry())
    plan = await planner.plan(FakeGoal(id="g", name="n", description="d"))
    # Failure inside find() must NOT poison the plan — degrade to llm_turn.
    for s in plan.steps:
        assert s.action_kind == "llm_turn"
