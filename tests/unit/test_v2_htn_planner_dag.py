"""HTNPlanner (R2 DAG variant) unit tests — 2026-05-10.

Distinct from the legacy ``test_v2_htn_planner.py`` which covers
``xmclaw.cognition.planner`` (Phase 6.3, step-sequence planner).
This file pins the new ``xmclaw.cognition.htn_planner.HTNPlanner``
which produces a recursive ``BoundGoal`` tree the TaskScheduler can
turn into a real Task DAG.

Coverage:
  * decompose loop: atomic / compound / depth-cap / budget-cap /
    LLM-failure / unparseable JSON / markdown-fence stripping
  * edge sanitisation: cycle-strip, out-of-range drop, self-loop drop
  * leaf-dependency propagation: compound edges → leaf edges
  * topo sort + materialize against a fake scheduler (records the
    submitted Tasks + their dependencies)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.cognition.htn_planner import BoundGoal, HTNPlanner
from xmclaw.cognition.task_scheduler import Task


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _FakeLLMResp:
    content: str


@dataclass
class _ScriptedLLM:
    """Returns the next scripted JSON content per call. Enough for
    nested decompose runs (we drive the recursion deterministically)."""
    scripts: list[str] = field(default_factory=list)
    calls: int = 0

    async def complete(self, messages: list, tools: Any = None) -> Any:  # noqa: ARG002
        if self.calls >= len(self.scripts):
            raise RuntimeError(
                f"_ScriptedLLM ran out (calls={self.calls}, "
                f"scripted={len(self.scripts)})"
            )
        out = self.scripts[self.calls]
        self.calls += 1
        return _FakeLLMResp(content=out)


@dataclass
class _RecordingScheduler:
    submitted: list[Task] = field(default_factory=list)

    async def submit(self, task: Task) -> str:
        self.submitted.append(task)
        return task.id


@dataclass
class _Goal:
    """Goal duck — matches the upgraded Goal shape in
    ``cognition.state.Goal``."""
    id: str
    description: str
    success_criteria: str | None = None
    priority: int = 5


# ── Atomic path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_atomic_goal_returns_atomic_bound() -> None:
    llm = _ScriptedLLM(scripts=[json.dumps({
        "kind": "atomic",
        "task_prompt": "write a haiku",
        "estimated_cost_usd": 0.02,
    })])
    planner = HTNPlanner(llm=llm)
    bound = await planner.plan(_Goal(id="g1", description="haiku me"))
    assert bound.kind == "atomic"
    assert bound.task_prompt == "write a haiku"
    assert bound.estimated_cost_usd == 0.02
    assert bound.children == ()
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_atomic_clamps_estimated_cost_to_range() -> None:
    """Negative / massive cost gets clamped to [0, 1]."""
    llm = _ScriptedLLM(scripts=[json.dumps({
        "kind": "atomic",
        "task_prompt": "x",
        "estimated_cost_usd": 99.0,  # way too much
    })])
    bound = await HTNPlanner(llm=llm).plan(_Goal(id="g", description="x"))
    assert bound.estimated_cost_usd == 1.0


# ── Compound path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compound_with_two_atomic_subgoals_no_edges() -> None:
    """parent compound → 2 atomic children → 0 deps. Materialize
    submits both tasks with empty dep lists."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "s1", "success_criteria": "x", "priority": 5},
                {"description": "s2", "success_criteria": "y", "priority": 6},
            ],
            "edges": [],
        }),
        json.dumps({"kind": "atomic", "task_prompt": "do s1",
                    "estimated_cost_usd": 0.05}),
        json.dumps({"kind": "atomic", "task_prompt": "do s2",
                    "estimated_cost_usd": 0.05}),
    ])
    planner = HTNPlanner(llm=llm)
    bound = await planner.plan(_Goal(id="root", description="parent"))
    assert bound.kind == "compound"
    assert len(bound.children) == 2
    assert bound.children[0].kind == "atomic"
    assert bound.children[0].task_prompt == "do s1"
    assert bound.children[1].task_prompt == "do s2"

    sched = _RecordingScheduler()
    ids = await planner.materialize(bound, scheduler=sched)
    assert len(ids) == 2
    assert {t.prompt for t in sched.submitted} == {"do s1", "do s2"}
    for t in sched.submitted:
        assert t.dependencies == []


@pytest.mark.asyncio
async def test_compound_with_edge_propagates_to_leaf_dep() -> None:
    """parent compound, edge (1, 0) means child 1 depends on child 0.
    After materialize the second submitted task carries the first
    task's id in its ``dependencies``."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "first", "priority": 5},
                {"description": "second", "priority": 5},
            ],
            "edges": [[1, 0]],   # second depends on first
        }),
        json.dumps({"kind": "atomic", "task_prompt": "do first",
                    "estimated_cost_usd": 0.05}),
        json.dumps({"kind": "atomic", "task_prompt": "do second",
                    "estimated_cost_usd": 0.05}),
    ])
    planner = HTNPlanner(llm=llm)
    bound = await planner.plan(_Goal(id="r", description="x"))
    sched = _RecordingScheduler()
    await planner.materialize(bound, scheduler=sched)
    assert len(sched.submitted) == 2
    first = next(t for t in sched.submitted if "first" in t.prompt)
    second = next(t for t in sched.submitted if "second" in t.prompt)
    assert second.dependencies == [first.id]
    assert first.dependencies == []


@pytest.mark.asyncio
async def test_topo_order_puts_dep_before_dependent() -> None:
    """Submission order matters — when A depends on B, B's task
    must already exist when we set A's deps."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "A", "priority": 5},
                {"description": "B", "priority": 5},
            ],
            "edges": [[0, 1]],   # A depends on B
        }),
        json.dumps({"kind": "atomic", "task_prompt": "A!",
                    "estimated_cost_usd": 0.05}),
        json.dumps({"kind": "atomic", "task_prompt": "B!",
                    "estimated_cost_usd": 0.05}),
    ])
    planner = HTNPlanner(llm=llm)
    bound = await planner.plan(_Goal(id="r", description="r"))
    sched = _RecordingScheduler()
    await planner.materialize(bound, scheduler=sched)
    prompts = [t.prompt for t in sched.submitted]
    assert prompts.index("B!") < prompts.index("A!")


# ── Caps ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_cap_forces_atomic() -> None:
    """max_depth=1 means after 1 expansion we treat sub-goals atomic
    (no LLM call for the leaves)."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [{"description": "deep", "priority": 5}],
            "edges": [],
        }),
    ])
    planner = HTNPlanner(llm=llm, max_depth=1)
    bound = await planner.plan(_Goal(id="r", description="x"))
    assert bound.kind == "compound"
    assert len(bound.children) == 1
    assert bound.children[0].kind == "atomic"
    assert bound.children[0].task_prompt == "deep"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_max_sub_goals_truncates_excess_children() -> None:
    sub_goals = [
        {"description": f"s{i}", "priority": 5} for i in range(10)
    ]
    llm = _ScriptedLLM(scripts=[
        json.dumps({"kind": "compound", "sub_goals": sub_goals, "edges": []}),
    ])
    planner = HTNPlanner(llm=llm, max_sub_goals=4, max_depth=1)
    bound = await planner.plan(_Goal(id="r", description="r"))
    assert bound.kind == "compound"
    assert len(bound.children) == 4


@pytest.mark.asyncio
async def test_budget_cap_forces_remaining_atomic() -> None:
    """When running cost exceeds the cap mid-recursion, remaining
    sub-goals get the budget-cap atomic treatment without an LLM call."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "expensive 1", "priority": 5},
                {"description": "expensive 2", "priority": 5},
            ],
            "edges": [],
        }),
        json.dumps({
            "kind": "atomic", "task_prompt": "do 1",
            "estimated_cost_usd": 0.6,
        }),
    ])
    planner = HTNPlanner(llm=llm, max_total_cost_usd=0.5, max_depth=3)
    bound = await planner.plan(_Goal(id="r", description="r"))
    assert bound.kind == "compound"
    assert bound.children[0].kind == "atomic"
    assert bound.children[1].kind == "atomic"
    assert bound.children[1].error is not None
    assert "budget cap" in bound.children[1].error
    assert llm.calls == 2  # root + first child only


# ── Failure modes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_failure_treated_atomic_with_error() -> None:
    class _BoomLLM:
        async def complete(self, *_a, **_kw):
            raise RuntimeError("net dead")

    bound = await HTNPlanner(llm=_BoomLLM()).plan(
        _Goal(id="r", description="x"),
    )
    assert bound.kind == "atomic"
    assert bound.error == "llm_failed_or_unparseable"
    assert bound.task_prompt == "x"


@pytest.mark.asyncio
async def test_unparseable_json_treated_atomic() -> None:
    llm = _ScriptedLLM(scripts=["totally not json {:::"])
    bound = await HTNPlanner(llm=llm).plan(_Goal(id="r", description="x"))
    assert bound.kind == "atomic"
    assert bound.error == "llm_failed_or_unparseable"


@pytest.mark.asyncio
async def test_llm_strips_markdown_fence() -> None:
    llm = _ScriptedLLM(scripts=[
        '```json\n{"kind":"atomic","task_prompt":"go","estimated_cost_usd":0.01}\n```',
    ])
    bound = await HTNPlanner(llm=llm).plan(_Goal(id="r", description="x"))
    assert bound.kind == "atomic"
    assert bound.task_prompt == "go"


@pytest.mark.asyncio
async def test_compound_with_zero_subgoals_treated_atomic() -> None:
    llm = _ScriptedLLM(scripts=[
        json.dumps({"kind": "compound", "sub_goals": [], "edges": []}),
    ])
    bound = await HTNPlanner(llm=llm).plan(_Goal(id="r", description="x"))
    assert bound.kind == "atomic"
    assert bound.error == "compound_with_no_subgoals"


# ── Edge sanitisation ────────────────────────────────────────────


def test_strip_cycles_drops_back_edge() -> None:
    edges = [(0, 1), (1, 2), (2, 0)]
    out = HTNPlanner._strip_cycles(edges, n=3)
    assert (0, 1) in out
    assert (1, 2) in out
    assert (2, 0) not in out


def test_strip_cycles_drops_self_loops() -> None:
    out = HTNPlanner._strip_cycles([(0, 0)], n=2)
    assert out == []


@pytest.mark.asyncio
async def test_invalid_edges_dropped_silently() -> None:
    """Non-int / out-of-range / self-loop edges are dropped without
    failing the plan."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "a", "priority": 5},
                {"description": "b", "priority": 5},
            ],
            "edges": [
                [0, 0],         # self
                [0, 99],        # out of range
                ["a", "b"],     # non-int
                [0, 1],         # GOOD
            ],
        }),
        json.dumps({"kind": "atomic", "task_prompt": "a!",
                    "estimated_cost_usd": 0.01}),
        json.dumps({"kind": "atomic", "task_prompt": "b!",
                    "estimated_cost_usd": 0.01}),
    ])
    bound = await HTNPlanner(llm=llm).plan(_Goal(id="r", description="r"))
    assert bound.kind == "compound"
    assert bound.edges == ((0, 1),)


# ── Goal dataclass shape (R2 upgrade) ────────────────────────────


def test_upgraded_goal_has_new_fields_with_defaults() -> None:
    """Pin the Goal dataclass shape: new fields all have defaults so
    legacy callers passing only id+description+priority still work."""
    from xmclaw.cognition.state import Goal

    g = Goal(id="x", description="hi")
    assert g.priority == 5
    assert g.status == "active"
    # New R2 fields with their defaults:
    assert g.success_criteria is None
    assert g.deadline is None
    assert g.parent_goal_id is None
    assert g.sub_goal_ids == []
    assert g.task_ids == []
    assert g.assigned_agent == "main"
    assert g.estimated_cost_usd is None
    assert isinstance(g.updated_at, float)


def test_upgraded_goal_carries_full_payload() -> None:
    from xmclaw.cognition.state import Goal

    g = Goal(
        id="parent", description="ship feature X", priority=8,
        success_criteria="all 3 sub-tasks completed; tests green",
        deadline=2_000_000_000.0,
        sub_goal_ids=["s1", "s2", "s3"],
        task_ids=["t1"],
        assigned_agent="planner-bot",
        estimated_cost_usd=0.42,
    )
    assert g.success_criteria == "all 3 sub-tasks completed; tests green"
    assert g.sub_goal_ids == ["s1", "s2", "s3"]
    assert g.assigned_agent == "planner-bot"
    assert g.estimated_cost_usd == 0.42


# ── BoundGoal API ────────────────────────────────────────────────


def test_bound_goal_total_cost_sums_subtree() -> None:
    leaf1 = BoundGoal(
        goal_id="l1", description="x", success_criteria=None, priority=5,
        kind="atomic", task_prompt="a", estimated_cost_usd=0.10,
    )
    leaf2 = BoundGoal(
        goal_id="l2", description="x", success_criteria=None, priority=5,
        kind="atomic", task_prompt="b", estimated_cost_usd=0.05,
    )
    parent = BoundGoal(
        goal_id="p", description="p", success_criteria=None, priority=5,
        kind="compound", children=(leaf1, leaf2),
    )
    assert parent.total_estimated_cost_usd() == pytest.approx(0.15)
    assert leaf1.total_estimated_cost_usd() == pytest.approx(0.10)


def test_bound_goal_atomic_leaves_walks_tree() -> None:
    leaf1 = BoundGoal(
        goal_id="a", description="a", success_criteria=None, priority=5,
        kind="atomic", task_prompt="a",
    )
    leaf2 = BoundGoal(
        goal_id="b", description="b", success_criteria=None, priority=5,
        kind="atomic", task_prompt="b",
    )
    nested = BoundGoal(
        goal_id="n", description="n", success_criteria=None, priority=5,
        kind="compound", children=(leaf2,),
    )
    root = BoundGoal(
        goal_id="r", description="r", success_criteria=None, priority=5,
        kind="compound", children=(leaf1, nested),
    )
    leaves = root.atomic_leaves()
    assert [leaf.goal_id for leaf in leaves] == ["a", "b"]
