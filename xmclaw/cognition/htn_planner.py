"""HTNPlanner — Hierarchical Task Network planner (R2, 2026-05-10).

Turns a high-level Goal into a DAG of Tasks the existing
``TaskScheduler`` can run.  Mirrors classical HTN by recursively
decomposing complex goals into sub-goals and finally into atomic
operators (= a single ``Task`` with a prompt the agent can execute
in one ``run_turn``).

Why a fresh module instead of extending ``cognition/planner.py``:
    The legacy ``planner.py`` is a flat per-percept "what should I do
    NOW" planner — useful for the CognitiveDaemon's reactive loop,
    but designed around single-step actions, not multi-step goal
    decomposition.  HTN's key affordance is "decompose THEN schedule"
    which is a different concern; mixing them would muddy both.

Decomposition strategy
======================

Each ``decompose(goal)`` call asks the LLM ONE question:

    "Is this goal atomic? If yes, return one task prompt. If no,
     return 2-6 sub-goals and the dependency edges between them."

The LLM returns strict JSON:
    * ``{"kind": "atomic", "task_prompt": "...", "estimated_cost_usd":
       0.05}`` — terminal node; gets bound to a ``Task``.
    * ``{"kind": "compound", "sub_goals": [{"description": "...",
       "success_criteria": "...", "priority": int}, ...],
       "edges": [[i, j], ...]}`` — i depends on j (i runs after j).
       Each sub-goal recurses through ``decompose`` next tick.

The planner is **bounded** by:
    * ``max_depth`` (default 3) — sub-tree height cap; deeper than
      this we treat the goal as atomic regardless of the LLM's
      opinion.  Prevents runaway recursion.
    * ``max_sub_goals`` (default 6) — LLM response truncated.
    * ``max_total_cost_usd`` (default 1.0) — sum of estimated costs;
      planner refuses to expand further when exceeded and downgrades
      remaining sub-goals to atomic.

These caps are **honesty knobs**, not safety knobs — the operator
should still review high-cost decompositions before running.

Why JSON schema (not free text):
    LLM-driven decomposition is fragile.  A strict schema means we
    can validate + reject malformed responses without parsing prose.
    The classic free-form HTN-from-LLM literature (e.g. Tree-of-
    Thoughts, ReAct planning) needs heavier safeguards because the
    plan is co-mingled with reasoning text; here the planner is a
    pure DAG-builder.

API
===

    planner = HTNPlanner(llm=...)
    bound = await planner.plan(goal)        # → BoundGoal tree
    await planner.materialize(bound, scheduler=task_scheduler)
                                             # creates Tasks per atomic
                                             # leaf, registers DAG with
                                             # the scheduler.

The two-step shape (``plan`` then ``materialize``) is intentional:
``plan`` is pure (no side effects on the scheduler), so callers can
inspect / approve the decomposition before commit.  R5 (proactive
agency double-confirm) hooks here.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


_DECOMPOSE_PROMPT = """\
你是 HTN (Hierarchical Task Network) 任务分解器。

GOAL:
{description}

完成判定标准:
{success_criteria}

判断这个 goal 是 **atomic** (一个 agent turn 能完成) 还是
**compound** (要拆成 2-6 个子 goal)，并输出 JSON。

== Atomic 输出 ==
{{
  "kind": "atomic",
  "task_prompt": "<给 agent 的具体执行 prompt，可直接当作 user message>",
  "estimated_cost_usd": <0.001-1.0 之间的浮点数>
}}

== Compound 输出 ==
{{
  "kind": "compound",
  "sub_goals": [
    {{"description": "...", "success_criteria": "...", "priority": 1-10}},
    ...
  ],
  "edges": [[i, j], ...]   /* i 依赖 j: 子 goal i 必须等子 goal j 完成 */
}}

判断准则:
- 一句话能让 agent 一次 run_turn 跑完 → atomic
- 涉及多个独立步骤 / 需要多次 LLM 调用 / 需要分阶段验证 → compound
- edges 表示依赖关系，空列表表示全部并行

绝对不要输出 JSON 之外的字符 (没有代码块标记，没有解释)。
"""


# ── Result types ─────────────────────────────────────────────────


PlanKind = Literal["atomic", "compound"]


@dataclass(frozen=True, slots=True)
class BoundGoal:
    """A goal that's been planned (decomposed). Tree node.

    * Atomic leaf: ``kind="atomic"``, ``task_prompt`` set, ``children``
      empty.
    * Compound: ``kind="compound"``, ``children`` populated, ``edges``
      describes child→child dependencies (indices into ``children``).
    """
    goal_id: str
    description: str
    success_criteria: str | None
    priority: int
    kind: PlanKind
    # Atomic fields
    task_prompt: str | None = None
    estimated_cost_usd: float | None = None
    # Compound fields
    children: tuple["BoundGoal", ...] = field(default_factory=tuple)
    edges: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    # Metadata
    depth: int = 0
    error: str | None = None  # set when LLM call failed; treated atomic

    def total_estimated_cost_usd(self) -> float:
        """Sum cost across the whole subtree."""
        if self.kind == "atomic":
            return float(self.estimated_cost_usd or 0.0)
        return sum(c.total_estimated_cost_usd() for c in self.children)

    def atomic_leaves(self) -> list["BoundGoal"]:
        """Flatten to the executable leaves (in tree-walk order)."""
        if self.kind == "atomic":
            return [self]
        out: list[BoundGoal] = []
        for c in self.children:
            out.extend(c.atomic_leaves())
        return out


# ── Planner ──────────────────────────────────────────────────────


class HTNPlanner:
    """LLM-driven HTN planner.

    Args:
        llm: any object exposing ``async complete(messages, tools=None)
            -> LLMResponse``.
        max_depth: hard cap on decomposition depth (default 3).
            Goals beyond this are treated atomic regardless of LLM.
        max_sub_goals: per-decompose cap on sub-goal count (default 6).
        max_total_cost_usd: budget gate. When the running estimated
            cost across decomposed leaves exceeds this, remaining
            sub-goals get downgraded to atomic (= one big run_turn,
            no further LLM-driven splits).
        timeout_s: hard wall-clock cap on each decompose call.
    """

    def __init__(
        self,
        *,
        llm: Any,
        max_depth: int = 3,
        max_sub_goals: int = 6,
        max_total_cost_usd: float = 1.0,
        timeout_s: float = 30.0,
    ) -> None:
        self._llm = llm
        self._max_depth = max(1, int(max_depth))
        self._max_sub_goals = max(1, int(max_sub_goals))
        self._max_total_cost_usd = float(max_total_cost_usd)
        self._timeout_s = max(2.0, float(timeout_s))

    # ── Public API ───────────────────────────────────────────────

    async def plan(self, goal: Any) -> BoundGoal:
        """Decompose a Goal recursively into a BoundGoal tree.

        ``goal`` is duck-typed: any object with ``id``, ``description``,
        ``success_criteria`` (optional), ``priority`` (optional). The
        upgraded ``cognition.state.Goal`` matches this shape exactly.
        """
        return await self._plan_recursive(
            goal_id=getattr(goal, "id", uuid.uuid4().hex),
            description=getattr(goal, "description", str(goal)),
            success_criteria=getattr(goal, "success_criteria", None),
            priority=int(getattr(goal, "priority", 5)),
            depth=0,
            running_cost_usd=0.0,
        )

    async def materialize(
        self,
        bound: BoundGoal,
        *,
        scheduler: Any,
        agent_id: str = "main",
    ) -> list[str]:
        """Bind the BoundGoal tree to the TaskScheduler.

        For each atomic leaf we ``submit`` a Task; dependency edges
        are computed by walking the tree (a leaf must wait for any
        leaf its ancestor depends on).

        Returns:
            List of ``task_id`` strings, in scheduling order. Empty
            list if the bound tree had zero atomic leaves (shouldn't
            happen with a healthy planner; defensive).
        """
        from xmclaw.cognition.task_scheduler import Task

        leaves = bound.atomic_leaves()
        if not leaves:
            return []

        # Compute leaf-level dependencies. The HTN tree's edges are
        # at COMPOUND level; we need them at LEAF level. Rule: leaf L
        # depends on leaf K iff the SUBTREE that contains L depends
        # on the SUBTREE that contains K (any leaf of that subtree).
        leaf_deps = self._leaf_dependency_map(bound)

        # Map BoundGoal → submitted Task id.
        leaf_to_task: dict[str, str] = {}
        ordered_ids: list[str] = []

        # Submit in topological order so when we set dependencies on
        # later tasks, earlier ones already have ids.
        topo = self._topo_sort(leaves, leaf_deps)
        for leaf in topo:
            task = Task(
                id=uuid.uuid4().hex,
                prompt=leaf.task_prompt or leaf.description,
                priority=leaf.priority,
                dependencies=[
                    leaf_to_task[dep_id]
                    for dep_id in leaf_deps.get(leaf.goal_id, [])
                    if dep_id in leaf_to_task
                ],
                agent_id=agent_id,
            )
            try:
                tid = await scheduler.submit(task)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "htn.materialize.submit_failed goal=%s err=%s",
                    leaf.goal_id, exc,
                )
                continue
            leaf_to_task[leaf.goal_id] = tid
            ordered_ids.append(tid)

        return ordered_ids

    # ── Internals ────────────────────────────────────────────────

    async def _plan_recursive(
        self,
        *,
        goal_id: str,
        description: str,
        success_criteria: str | None,
        priority: int,
        depth: int,
        running_cost_usd: float,
    ) -> BoundGoal:
        """Recurse until atomic / depth-cap / budget-cap."""

        # Depth cap: force atomic.
        if depth >= self._max_depth:
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=description,
                estimated_cost_usd=0.05,  # conservative default
                depth=depth,
            )

        # Budget cap: force atomic.
        if running_cost_usd >= self._max_total_cost_usd:
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=description,
                estimated_cost_usd=0.05,
                depth=depth,
                error=(
                    f"budget cap hit "
                    f"(running={running_cost_usd:.2f} "
                    f"cap={self._max_total_cost_usd:.2f})"
                ),
            )

        envelope = await self._ask_llm(
            description=description,
            success_criteria=success_criteria or "(by inspection)",
        )

        if envelope is None:
            # LLM failed → treat atomic so the caller can still
            # ship the goal as a single run_turn.
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=description,
                estimated_cost_usd=0.05,
                depth=depth,
                error="llm_failed_or_unparseable",
            )

        kind = envelope.get("kind", "atomic")
        if kind not in ("atomic", "compound"):
            kind = "atomic"

        if kind == "atomic":
            cost = float(envelope.get("estimated_cost_usd", 0.05))
            cost = max(0.0, min(cost, 1.0))
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=str(envelope.get("task_prompt", description)),
                estimated_cost_usd=cost,
                depth=depth,
            )

        # Compound — recurse on each sub_goal.
        raw_sub = envelope.get("sub_goals", []) or []
        if not isinstance(raw_sub, list) or len(raw_sub) == 0:
            # Compound with no children = nonsense; treat atomic.
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=description,
                estimated_cost_usd=0.05,
                depth=depth,
                error="compound_with_no_subgoals",
            )
        raw_sub = raw_sub[: self._max_sub_goals]

        children: list[BoundGoal] = []
        rolling_cost = running_cost_usd
        for sg in raw_sub:
            if not isinstance(sg, dict):
                continue
            sg_desc = str(sg.get("description", "")).strip()
            if not sg_desc:
                continue
            sg_success = sg.get("success_criteria")
            sg_pri = int(sg.get("priority", priority))
            child = await self._plan_recursive(
                goal_id=uuid.uuid4().hex,
                description=sg_desc,
                success_criteria=str(sg_success) if sg_success else None,
                priority=max(1, min(10, sg_pri)),
                depth=depth + 1,
                running_cost_usd=rolling_cost,
            )
            children.append(child)
            rolling_cost += child.total_estimated_cost_usd()

        if not children:
            return BoundGoal(
                goal_id=goal_id, description=description,
                success_criteria=success_criteria, priority=priority,
                kind="atomic",
                task_prompt=description,
                estimated_cost_usd=0.05,
                depth=depth,
                error="all_subgoals_invalid",
            )

        # Validate edges: ints in [0, len(children)), no self-edges,
        # acyclic. Drop bad ones rather than reject the whole plan.
        raw_edges = envelope.get("edges", []) or []
        edges_clean: list[tuple[int, int]] = []
        for e in raw_edges:
            if not (isinstance(e, list) and len(e) == 2):
                continue
            try:
                i, j = int(e[0]), int(e[1])
            except (ValueError, TypeError):
                continue
            if i == j or not (0 <= i < len(children)) or not (0 <= j < len(children)):
                continue
            edges_clean.append((i, j))
        edges_clean = self._strip_cycles(edges_clean, len(children))

        return BoundGoal(
            goal_id=goal_id, description=description,
            success_criteria=success_criteria, priority=priority,
            kind="compound",
            children=tuple(children),
            edges=tuple(edges_clean),
            depth=depth,
        )

    async def _ask_llm(
        self, *, description: str, success_criteria: str,
    ) -> dict[str, Any] | None:
        """Run one decompose LLM call. Returns parsed dict or None
        on any failure (timeout / network / unparseable JSON)."""
        import asyncio

        prompt = _DECOMPOSE_PROMPT.format(
            description=description,
            success_criteria=success_criteria,
        )
        try:
            from xmclaw.providers.llm.base import Message
            resp = await asyncio.wait_for(
                self._llm.complete([
                    Message(role="user", content=prompt),
                ]),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("htn.llm_timeout depth_unknown")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("htn.llm_failed err=%s", exc)
            return None

        content = (getattr(resp, "content", "") or "").strip()
        if content.startswith("```"):
            content = content.lstrip("`")
            if content.lower().startswith("json"):
                content = content[4:]
            content = content.strip("`").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("htn.bad_json preview=%r", content[:200])
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _strip_cycles(
        edges: list[tuple[int, int]], n: int,
    ) -> list[tuple[int, int]]:
        """Drop edges that would create a cycle (greedy first-pass)."""
        adj: dict[int, set[int]] = {i: set() for i in range(n)}
        kept: list[tuple[int, int]] = []
        for i, j in edges:
            # Adding edge i→j would create cycle iff j→...→i exists.
            if HTNPlanner._reachable(adj, j, i):
                continue
            adj[i].add(j)
            kept.append((i, j))
        return kept

    @staticmethod
    def _reachable(
        adj: dict[int, set[int]], src: int, dst: int,
    ) -> bool:
        if src == dst:
            return True
        visited = {src}
        stack = [src]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if nxt == dst:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        return False

    @staticmethod
    def _leaf_dependency_map(
        bound: BoundGoal,
    ) -> dict[str, list[str]]:
        """For each atomic leaf in ``bound``, list the leaf goal_ids
        it must wait for.

        Walks the tree once, propagating compound-level edges down
        to the leaves they ultimately gate.
        """
        deps: dict[str, list[str]] = {}

        def walk(node: BoundGoal) -> None:
            if node.kind == "atomic":
                deps.setdefault(node.goal_id, [])
                return
            # Each child first gets its own subtree mapped.
            for c in node.children:
                walk(c)
            # Edge (i, j) means child i depends on child j → every
            # leaf in subtree(i) depends on every leaf in subtree(j).
            for i, j in node.edges:
                src_leaves = node.children[i].atomic_leaves()
                dst_leaves = node.children[j].atomic_leaves()
                for sl in src_leaves:
                    bucket = deps.setdefault(sl.goal_id, [])
                    for dl in dst_leaves:
                        if dl.goal_id not in bucket:
                            bucket.append(dl.goal_id)

        walk(bound)
        return deps

    @staticmethod
    def _topo_sort(
        leaves: list[BoundGoal],
        leaf_deps: dict[str, list[str]],
    ) -> list[BoundGoal]:
        """Kahn's algorithm. Drops impossible cases (= we already
        cycle-stripped, but defensive)."""
        by_id = {leaf.goal_id: leaf for leaf in leaves}
        in_deg: dict[str, int] = {
            lid: len(leaf_deps.get(lid, [])) for lid in by_id
        }
        ready = [lid for lid, d in in_deg.items() if d == 0]
        out: list[BoundGoal] = []
        # Lookup: who points at me?
        rev: dict[str, list[str]] = {lid: [] for lid in by_id}
        for src, dsts in leaf_deps.items():
            for dst in dsts:
                if dst in rev:
                    rev[dst].append(src)
        while ready:
            cur = ready.pop(0)
            out.append(by_id[cur])
            for nxt in rev.get(cur, ()):
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0:
                    ready.append(nxt)
        # Append anything left over (cyclic; degenerate case).
        for lid in by_id:
            if by_id[lid] not in out:
                out.append(by_id[lid])
        return out


__all__ = [
    "BoundGoal",
    "HTNPlanner",
    "PlanKind",
]
