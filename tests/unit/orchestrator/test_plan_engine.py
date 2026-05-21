"""Tests for PlanEngine."""
from __future__ import annotations


from xmclaw.orchestrator.plan_engine import PlanEngine, _guess_effort


class FakeBoundGoal:
    def __init__(self, goal_id, description, kind="atomic", cost=None, prompt="", error=None):
        self.goal_id = goal_id
        self.description = description
        self.kind = kind
        self.estimated_cost_usd = cost
        self.task_prompt = prompt
        self.error = error

    def atomic_leaves(self):
        if self.kind == "atomic":
            return [self]
        return []


class FakePlanner:
    async def plan(self, goal):
        return FakeBoundGoal(
            goal_id="g1",
            description="test goal",
            kind="atomic",
            cost=0.10,
            prompt="do the thing",
        )

    def _leaf_dependency_map(self, bound):
        return {}


class TestPlanEngine:
    async def test_create_plan_returns_execution_plan(self):
        engine = PlanEngine(planner=FakePlanner())
        plan = await engine.create_plan("test goal")
        assert plan is not None
        assert plan.goal == "test goal"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].task_id == "g1"

    def test_guess_effort(self):
        leaf = FakeBoundGoal("x", "y", cost=0.02)
        assert _guess_effort(leaf) == "trivial"
        leaf = FakeBoundGoal("x", "y", cost=0.10)
        assert _guess_effort(leaf) == "small"
        leaf = FakeBoundGoal("x", "y", cost=0.30)
        assert _guess_effort(leaf) == "medium"
        leaf = FakeBoundGoal("x", "y", cost=0.50)
        assert _guess_effort(leaf) == "large"
