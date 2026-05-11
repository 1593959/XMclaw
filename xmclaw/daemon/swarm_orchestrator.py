"""SwarmOrchestrator — distributed multi-agent task decomposition & aggregation.

Jarvisification Phase C-4/4.  Bridges the existing HTNPlanner, TaskScheduler,
and MultiAgentManager into a single ``dispatch()`` call that:

1. **Decomposes** a high-level goal into a DAG of atomic tasks.
2. **Balances** tasks across available agents (round-robin + capability hint).
3. **Schedules** tasks onto the TaskScheduler with correct dependencies.
4. **Aggregates** results as they complete (concat / vote / llm-synthesize).

The orchestrator is intentionally thin — it does not replace the scheduler's
execution engine or the planner's LLM-driven decomposition.  It is the
*conductor* that wires those pieces together so the primary agent can say
"dispatch this complex goal to the swarm" and receive a unified answer.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

AggregationStrategy = Literal["concat", "vote", "map_reduce"]


# ── Load balancer ─────────────────────────────────────────────────────────


class LoadBalancer:
    """Assign tasks to agents based on availability and simple capability hints."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def assign(self, tasks: list[Any]) -> dict[str, str]:
        """Return ``{task_id: agent_id}`` mapping.

        Strategy:
        * Enumerate every registered agent id (excluding the primary
          which is handled implicitly as ``"main"``).
        * Round-robin across them; if no workers exist everything lands
          on ``"main"``.
        * Simple keyword heuristics steer tasks toward agents whose id
          hints at a capability (``dev`` → code tasks, ``research`` →
          research tasks).  This is a placeholder for a future capability
          registry.
        """
        agents: list[str] = []
        try:
            agents = [
                aid for aid in self._manager.list_ids()
                if aid != "main"
            ]
        except Exception:  # noqa: BLE001
            pass

        if not agents:
            return {t.id: "main" for t in tasks}

        assignments: dict[str, str] = {}
        idx = 0
        for task in tasks:
            prompt = getattr(task, "prompt", "").lower()
            matched = self._match_capability(prompt, agents)
            if matched:
                assignments[task.id] = matched
            else:
                assignments[task.id] = agents[idx % len(agents)]
                idx += 1
        return assignments

    @staticmethod
    def _match_capability(prompt: str, agents: list[str]) -> str | None:
        """Simple keyword → agent-id heuristic."""
        hints = [
            ("code", "dev"),
            ("program", "dev"),
            ("refactor", "dev"),
            ("research", "research"),
            ("search", "research"),
            ("analyze", "research"),
            ("test", "qa"),
            ("bug", "qa"),
        ]
        for keyword, capability in hints:
            if keyword in prompt:
                for aid in agents:
                    if capability in aid.lower():
                        return aid
        return None


# ── Task aggregator ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AggregationResult:
    """Outcome of a swarm dispatch."""

    result: str
    task_ids: list[str] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)
    completed_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    elapsed_seconds: float = 0.0


class TaskAggregator:
    """Poll completed tasks and merge their results."""

    async def wait_and_aggregate(
        self,
        task_ids: list[str],
        scheduler: Any,
        *,
        strategy: AggregationStrategy = "concat",
        timeout_s: float = 300.0,
        llm: Any | None = None,
        goal_description: str = "",
    ) -> AggregationResult:
        """Block until every task in ``task_ids`` reaches a terminal state,
        then apply ``strategy`` to merge outputs.

        Args:
            task_ids: IDs previously submitted to the scheduler.
            scheduler: TaskScheduler-like object.
            strategy: ``concat`` (default), ``vote``, or ``map_reduce``.
            timeout_s: Hard wall-clock cap for polling.
            llm: Optional LLM for ``map_reduce`` synthesis.
            goal_description: Passed to the LLM synthesizer when using
                ``map_reduce``.
        """
        pending = set(task_ids)
        t0 = time.time()
        raw_results: dict[str, str] = {}
        failed: list[str] = []
        timed_out: list[str] = []

        while pending and (time.time() - t0) < timeout_s:
            for tid in list(pending):
                task = await scheduler.get_task(tid)
                if task is None:
                    timed_out.append(tid)
                    pending.remove(tid)
                    continue
                if task.status == "completed":
                    raw_results[tid] = task.result or ""
                    pending.remove(tid)
                elif task.status in ("failed", "escalated"):
                    raw_results[tid] = f"[task {tid} failed: {task.error or 'unknown'}]"
                    failed.append(tid)
                    pending.remove(tid)
            if pending:
                await asyncio.sleep(0.5)

        for tid in pending:
            raw_results[tid] = f"[task {tid} timed out]"
            timed_out.append(tid)

        merged = self._merge(
            [raw_results[tid] for tid in task_ids if tid in raw_results],
            strategy=strategy,
            llm=llm,
            goal_description=goal_description,
        )

        return AggregationResult(
            result=merged,
            task_ids=task_ids,
            completed_count=len(task_ids) - len(failed) - len(timed_out),
            failed_count=len(failed),
            timed_out_count=len(timed_out),
            elapsed_seconds=round(time.time() - t0, 1),
        )

    @staticmethod
    def _merge(
        results: list[str],
        *,
        strategy: AggregationStrategy,
        llm: Any | None = None,
        goal_description: str = "",
    ) -> str:
        if not results:
            return "(no results)"

        if strategy == "concat":
            return "\n\n---\n\n".join(results)

        if strategy == "vote":
            yes = sum(1 for r in results if "yes" in r.lower())
            no = sum(1 for r in results if "no" in r.lower())
            abstain = len(results) - yes - no
            return (
                f"Vote result: yes={yes}, no={no}, abstain={abstain}\n"
                f"Total votes: {len(results)}"
            )

        # map_reduce — attempt LLM synthesis, fallback to concat
        if llm is not None and goal_description:
            try:
                return _synthesize_with_llm(llm, results, goal_description)
            except Exception as exc:  # noqa: BLE001
                logger.warning("swarm.synthesize_failed err=%s", exc)

        return "\n\n---\n\n".join(results)


# ── LLM synthesis helper ──────────────────────────────────────────────────


_SYNTHESIS_PROMPT = """\
You are a synthesis engine.  Below are partial results from sub-agents
that worked in parallel on different pieces of a larger goal.

GOAL:
{goal}

PARTIAL RESULTS:
{results}

Please produce a single coherent, unified answer that integrates the
partial results.  Remove redundancies, resolve contradictions (if any),
and present the final answer in a clean, structured form.
"""


def _synthesize_with_llm(
    llm: Any, results: list[str], goal_description: str,
) -> str:
    """Best-effort LLM-based synthesis.  Synchronous call — the aggregator
    already runs inside an async poll loop, so we use ``asyncio.run``
    only when we know we're in a coroutine context.  In practice this is
    awaited by ``wait_and_aggregate``, so ``llm.complete`` is awaited
    directly."""
    # This function is intentionally synchronous-looking because the
    # caller awaits it inside an async method.  We construct the prompt
    # and return a coroutine object that the caller awaits.
    from xmclaw.providers.llm.base import Message

    numbered = "\n\n".join(
        f"[{i + 1}]\n{r}" for i, r in enumerate(results)
    )
    prompt = _SYNTHESIS_PROMPT.format(
        goal=goal_description,
        results=numbered,
    )
    # Return a coroutine so the caller can await it.
    return llm.complete([Message(role="user", content=prompt)])


# ── SwarmOrchestrator ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwarmDispatchRequest:
    """Typed request for :meth:`SwarmOrchestrator.dispatch`."""

    description: str
    success_criteria: str | None = None
    priority: int = 5
    strategy: AggregationStrategy = "map_reduce"
    max_wait_s: float = 300.0


@dataclass(frozen=True)
class SwarmDispatchResult:
    """Typed result from :meth:`SwarmOrchestrator.dispatch`."""

    ok: bool
    result: str
    task_ids: list[str]
    assignments: dict[str, str]
    completed: int
    failed: int
    timed_out: int
    elapsed_seconds: float
    error: str | None = None


class SwarmOrchestrator:
    """High-level swarm conductor.

    Args:
        planner: HTNPlanner instance (or duck-typed with ``plan`` / ``materialize``).
        scheduler: TaskScheduler instance.
        manager: MultiAgentManager-like registry.
        llm: Optional LLM for ``map_reduce`` synthesis.
    """

    def __init__(
        self,
        planner: Any,
        scheduler: Any,
        manager: Any,
        llm: Any | None = None,
    ) -> None:
        self._planner = planner
        self._scheduler = scheduler
        self._manager = manager
        self._llm = llm
        self._balancer = LoadBalancer(manager)
        self._aggregator = TaskAggregator()

    # ── public API ───────────────────────────────────────────────────────

    async def dispatch(
        self,
        request: SwarmDispatchRequest,
    ) -> SwarmDispatchResult:
        """End-to-end swarm dispatch.

        1. Decompose the goal via HTNPlanner.
        2. Assign each leaf task to an agent via LoadBalancer.
        3. Submit tasks to the TaskScheduler in topological order.
        4. Poll until completion and aggregate results.
        """
        t0 = time.time()
        try:
            bound = await self._planner.plan(
                _SimpleGoal(
                    id=uuid.uuid4().hex,
                    description=request.description,
                    success_criteria=request.success_criteria,
                    priority=request.priority,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm.plan_failed err=%s", exc)
            return SwarmDispatchResult(
                ok=False, result="", task_ids=[], assignments={},
                completed=0, failed=0, timed_out=0,
                elapsed_seconds=round(time.time() - t0, 1),
                error=f"planning failed: {exc}",
            )

        leaves = bound.atomic_leaves()
        if not leaves:
            return SwarmDispatchResult(
                ok=True, result="(goal produced no actionable tasks)",
                task_ids=[], assignments={},
                completed=0, failed=0, timed_out=0,
                elapsed_seconds=round(time.time() - t0, 1),
            )

        # Dependency map at leaf level (static methods on HTNPlanner).
        leaf_deps = self._planner._leaf_dependency_map(bound)
        topo = self._planner._topo_sort(leaves, leaf_deps)

        # Build dummy tasks so the balancer can see prompts.
        from xmclaw.cognition.task_scheduler import Task

        dummy_tasks = [
            Task(
                id=leaf.goal_id,
                prompt=leaf.task_prompt or leaf.description,
                priority=leaf.priority,
            )
            for leaf in topo
        ]
        assignments = self._balancer.assign(dummy_tasks)

        # Submit in topological order.
        leaf_to_task: dict[str, str] = {}
        task_ids: list[str] = []
        for leaf in topo:
            deps = leaf_deps.get(leaf.goal_id, [])
            resolved_deps = [
                leaf_to_task[d] for d in deps if d in leaf_to_task
            ]
            task = Task(
                id=uuid.uuid4().hex,
                prompt=leaf.task_prompt or leaf.description,
                priority=leaf.priority,
                dependencies=resolved_deps,
                agent_id=assignments.get(leaf.goal_id, "main"),
            )
            try:
                tid = await self._scheduler.submit(task)
            except Exception as exc:  # noqa: BLE001
                logger.warning("swarm.submit_failed leaf=%s err=%s", leaf.goal_id, exc)
                continue
            leaf_to_task[leaf.goal_id] = tid
            task_ids.append(tid)

        if not task_ids:
            return SwarmDispatchResult(
                ok=False, result="", task_ids=[], assignments={},
                completed=0, failed=0, timed_out=0,
                elapsed_seconds=round(time.time() - t0, 1),
                error="all task submissions failed",
            )

        # Aggregate.
        agg = await self._aggregator.wait_and_aggregate(
            task_ids,
            self._scheduler,
            strategy=request.strategy,
            timeout_s=request.max_wait_s,
            llm=self._llm,
            goal_description=request.description,
        )

        # Build reverse mapping for the result (task_id → agent_id).
        task_to_agent: dict[str, str] = {
            leaf_to_task[leaf.goal_id]: assignments.get(leaf.goal_id, "main")
            for leaf in topo
            if leaf.goal_id in leaf_to_task
        }

        return SwarmDispatchResult(
            ok=True,
            result=agg.result,
            task_ids=task_ids,
            assignments=task_to_agent,
            completed=agg.completed_count,
            failed=agg.failed_count,
            timed_out=agg.timed_out_count,
            elapsed_seconds=round(agg.elapsed_seconds + (time.time() - t0), 1),
        )


# ── Internal helper ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _SimpleGoal:
    """Minimal goal duck-type accepted by HTNPlanner.plan."""

    id: str
    description: str
    success_criteria: str | None = None
    priority: int = 5


__all__ = [
    "AggregationResult",
    "AggregationStrategy",
    "LoadBalancer",
    "SwarmDispatchRequest",
    "SwarmDispatchResult",
    "SwarmOrchestrator",
    "TaskAggregator",
]
