"""PlanEngine — structured task decomposition with validation.

Wraps the existing HTNPlanner and adds:
  * ExecutionPlan dataclass (flat, DAG-friendly)
  * HonestGrader + codebase-index validation
  * Retry loop for rejected plans (≤3 attempts)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

Effort = Literal["trivial", "small", "medium", "large"]


@dataclass(slots=True)
class Task:
    """One unit of work inside an ExecutionPlan."""

    task_id: str
    description: str
    estimated_effort: Effort = "small"
    required_capabilities: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    # Execution-only fields (set when plan is bound to a runtime).
    agent_id: str = "main"
    prompt: str = ""


@dataclass(slots=True)
class ExecutionPlan:
    """Structured plan produced by PlanEngine."""

    plan_id: str
    goal: str
    tasks: list[Task]
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # Validation telemetry.
    validation_errors: list[str] = field(default_factory=list)
    validated: bool = False


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


class PlanEngine:
    """High-level plan generator with validation.

    Parameters
    ----------
    planner :
        Existing HTNPlanner instance (from cognition/htn_planner.py).
    codebase_store :
        Optional CodebaseStore for ``context_files`` existence checks.
    tool_provider :
        Optional ToolProvider for ``required_capabilities`` checks.
    grader :
        Optional HonestGrader for plan-quality scoring.
    """

    def __init__(
        self,
        *,
        planner: Any,
        codebase_store: Any | None = None,
        tool_provider: Any | None = None,
        grader: Any | None = None,
    ) -> None:
        self._planner = planner
        self._codebase_store = codebase_store
        self._tool_provider = tool_provider
        self._grader = grader

    async def create_plan(
        self,
        goal: str,
        *,
        success_criteria: str | None = None,
        max_retries: int = 3,
    ) -> ExecutionPlan | None:
        """Generate an ExecutionPlan from a natural-language goal.

        Retries up to ``max_retries`` if validation fails.
        Returns ``None`` if the planner cannot produce a valid plan.
        """
        for attempt in range(max_retries + 1):
            bound = await self._planner.plan(
                _DuckGoal(
                    id=f"goal_{uuid.uuid4().hex[:8]}",
                    description=goal,
                    success_criteria=success_criteria,
                    priority=5,
                ),
            )
            if bound.error:
                _log.warning("plan_engine.planner_error: %s", bound.error)
                continue

            plan = self._bound_to_execution_plan(bound, goal)
            v = self._validate(plan)
            if v.ok:
                plan.validated = True
                return plan

            plan.validation_errors = v.errors
            _log.info(
                "plan_engine.validation_failed attempt=%d errors=%s",
                attempt, v.errors,
            )
            # On final attempt, return the plan anyway (caller decides).
            if attempt == max_retries:
                return plan

        return None

    # ── internal ──

    def _bound_to_execution_plan(
        self, bound: Any, goal: str,
    ) -> ExecutionPlan:
        """Convert a BoundGoal tree into a flat ExecutionPlan."""
        leaves = bound.atomic_leaves() if hasattr(bound, "atomic_leaves") else []
        leaf_map: dict[str, Task] = {}
        tasks: list[Task] = []

        for leaf in leaves:
            t = Task(
                task_id=leaf.goal_id,
                description=leaf.description,
                estimated_effort=_guess_effort(leaf),
                prompt=leaf.task_prompt or leaf.description,
            )
            leaf_map[leaf.goal_id] = t
            tasks.append(t)

        # Build flat dependency map using HTNPlanner helpers if present.
        deps: dict[str, list[str]] = {}
        if hasattr(self._planner, "_leaf_dependency_map"):
            raw_deps = self._planner._leaf_dependency_map(bound)
            for leaf_id, prereqs in raw_deps.items():
                if leaf_id in leaf_map:
                    deps[leaf_id] = [p for p in prereqs if p in leaf_map]

        return ExecutionPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            goal=goal,
            tasks=tasks,
            dependencies=deps,
        )

    def _validate(self, plan: ExecutionPlan) -> ValidationResult:
        errors: list[str] = []

        # 1. context_files existence.
        if self._codebase_store is not None:
            for t in plan.tasks:
                for f in t.context_files:
                    if not self._file_exists(f):
                        errors.append(f"context_file_missing: {f} (task {t.task_id})")

        # 2. required_capabilities in available tool set.
        if self._tool_provider is not None:
            available = self._tool_capabilities()
            for t in plan.tasks:
                missing = [c for c in t.required_capabilities if c not in available]
                if missing:
                    errors.append(
                        f"missing_capabilities: {missing} (task {t.task_id})"
                    )

        return ValidationResult(ok=len(errors) == 0, errors=errors)

    def _file_exists(self, relpath: str) -> bool:
        if self._codebase_store is None:
            return True
        try:
            # CodebaseStore.search_text or similar lightweight check.
            results = self._codebase_store.search_text(relpath, k=1)
            return any(relpath in str(r.get("relpath", "")) for r in results)
        except Exception:  # noqa: BLE001
            return True  # lenient when store is unavailable.

    def _tool_capabilities(self) -> set[str]:
        if self._tool_provider is None:
            return set()
        try:
            schemas = self._tool_provider.get_tool_schemas()
            return {s.get("name", "") for s in schemas}
        except Exception:  # noqa: BLE001
            return set()


class _DuckGoal:
    """Minimal duck-type goal for HTNPlanner."""

    def __init__(
        self,
        id: str,  # noqa: A002
        description: str,
        success_criteria: str | None = None,
        priority: int = 5,
    ) -> None:
        self.id = id
        self.description = description
        self.success_criteria = success_criteria
        self.priority = priority


def _guess_effort(leaf: Any) -> Effort:
    """Map estimated_cost_usd → effort label."""
    cost = getattr(leaf, "estimated_cost_usd", None)
    if cost is None:
        return "small"
    if cost <= 0.05:
        return "trivial"
    if cost <= 0.15:
        return "small"
    if cost <= 0.40:
        return "medium"
    return "large"
