"""HTN Planner — Jarvis Phase 6.3 goal-to-plan decomposition.

A simplified Hierarchical Task Network planner: an LLM decomposes a
``Goal`` into a tuple of atomic ``PlanStep`` actions, each preferring
``skill_invoke`` when an installed skill matches and falling back to
``llm_turn`` otherwise. The planner refuses to ship a cyclic plan
(``ValueError`` at ``plan()`` time, never silently recovered) and on
runtime failure repairs the plan **once** with optional reasoning-engine
grounding before accepting a second failure as terminal.

Why "simplified": real SHOP / SHOP2 HTN with method libraries +
preconditions is Phase 7. Today we get LLM-driven step generation +
registry matching + bounded retry, which is sufficient to drive the
``ActionDispatcher`` end-to-end and let later phases swap in deeper
search without changing this file's public surface.

This module is greenfield. Nothing in the daemon imports it yet —
wiring (factory + lifespan) lands in a follow-up commit. See
``docs/JARVIS_PHASE_6_DESIGN.md`` §3.4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)


ActionKind = Literal["llm_turn", "skill_invoke", "tool_call", "wait_for_percept"]
PlanStatus = Literal["draft", "executing", "completed", "failed", "repaired"]

_VALID_ACTION_KINDS: frozenset[str] = frozenset(
    ("llm_turn", "skill_invoke", "tool_call", "wait_for_percept")
)


@dataclass(frozen=True)
class PlanStep:
    """One atomic action inside a :class:`Plan`.

    ``depends_on`` references other ``PlanStep.id`` values inside the
    *same* plan; cross-plan dependencies are intentionally not modelled
    here (use a parent goal). ``retry_policy`` defaults to two retries
    with one second of backoff — the planner reads ``max_retries`` and
    ``backoff_s`` keys; additional keys are tolerated for forward
    compatibility.
    """

    id: str
    action_kind: ActionKind
    payload: dict[str, Any]
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    expected_outcome: str = ""
    retry_policy: dict[str, Any] = field(
        default_factory=lambda: {"max_retries": 2, "backoff_s": 1.0}
    )


@dataclass(frozen=True)
class Plan:
    """A goal decomposition: ordered, dependency-aware, status-tracked.

    ``confidence`` is the planner's self-estimate in [0, 1] and is
    capped at construction time by ``Planner.confidence_cap`` —
    honest disclosure: the LLM does not know how good its plan is, so
    we refuse to claim more than ``confidence_cap`` (default 0.6).
    """

    id: str
    goal_id: str
    steps: tuple[PlanStep, ...]
    status: PlanStatus = "draft"
    confidence: float = 0.5
    created_at: float = 0.0


@dataclass(frozen=True)
class PlanStepFailure:
    """Diagnostic surface for a step that exhausted its retry budget."""

    step_id: str
    reason: str
    step_output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    """Outcome of :meth:`Planner.execute`.

    ``step_results`` is in execution order (topological), one dict per
    completed step. On failure, ``error`` carries the human-readable
    reason and ``status`` is ``"failed"``; on repair-then-success,
    ``status`` is ``"repaired"``.
    """

    plan_id: str
    status: PlanStatus
    step_results: tuple[dict[str, Any], ...]
    error: str | None = None


class Planner:
    """Goal → Plan via LLM, with skill-registry matching + repair.

    Constructor parameters are duck-typed (``Any``) on purpose — this
    module is in ``xmclaw/cognition/`` and we DO NOT want a hard
    import on ``providers/`` or ``daemon/``. Tests inject fakes; real
    wiring happens in ``daemon/factory.py``.

    - ``llm`` MUST expose an awaitable ``complete(prompt: str, *,
      response_format: str = "json") -> str`` (or equivalent — we only
      call ``await llm.complete(prompt)``-style and parse JSON).
    - ``skill_registry`` (optional) MUST expose ``find(intent: str)
      -> Any | None`` returning a skill object with ``.name`` /
      ``.id`` when a step's intent matches an installed skill.
    - ``reasoning_engine`` (optional) is consulted by :meth:`repair`
      to ground the new plan in similar historical failures via
      whatever ``analogical(query)``-shaped method the engine offers;
      we silently degrade if the call raises or returns nothing.
    - ``confidence_cap`` clamps the LLM's claimed confidence so we
      can't over-promise (default 0.6).
    """

    def __init__(
        self,
        llm: Any,
        skill_registry: Any | None = None,
        reasoning_engine: Any | None = None,
        confidence_cap: float = 0.6,
    ) -> None:
        if not 0.0 < confidence_cap <= 1.0:
            raise ValueError("confidence_cap must be in (0, 1]")
        self._llm = llm
        self._skill_registry = skill_registry
        self._reasoning_engine = reasoning_engine
        self._confidence_cap = confidence_cap

    @property
    def confidence_cap(self) -> float:
        return self._confidence_cap

    # ----------------------------------------------------------- plan()

    async def plan(self, goal: Any) -> Plan:
        """Decompose ``goal`` into a Plan.

        Steps:
          1. Render a JSON-shaped prompt from the goal's public fields.
          2. Ask the LLM for a list of step descriptors.
          3. For each descriptor, prefer ``skill_invoke`` when
             ``skill_registry.find(intent)`` resolves; otherwise
             ``llm_turn``.
          4. Validate dependencies (every ``depends_on`` id MUST be a
             sibling step), then run cycle detection. A cyclic plan
             is **never** returned — we raise ``ValueError`` so the
             caller can surface or repair it explicitly.

        Malformed LLM output (non-JSON, wrong shape, empty list) → a
        ``Plan`` with ``status="failed"`` and zero steps. The caller
        decides whether to retry or escalate.
        """
        goal_id = _extract_goal_id(goal)
        goal_blob = _goal_to_prompt_blob(goal)
        prompt = _build_planning_prompt(goal_blob)

        raw_steps, raw_confidence = await self._call_llm_for_plan(prompt)
        if not raw_steps:
            return Plan(
                id=_new_id("plan"),
                goal_id=goal_id,
                steps=(),
                status="failed",
                confidence=0.0,
                created_at=time.time(),
            )

        steps: list[PlanStep] = []
        seen_ids: set[str] = set()
        for raw in raw_steps:
            step = self._materialize_step(raw, sibling_ids=seen_ids)
            if step is None:
                continue
            steps.append(step)
            seen_ids.add(step.id)

        if not steps:
            return Plan(
                id=_new_id("plan"),
                goal_id=goal_id,
                steps=(),
                status="failed",
                confidence=0.0,
                created_at=time.time(),
            )

        plan_obj = Plan(
            id=_new_id("plan"),
            goal_id=goal_id,
            steps=tuple(steps),
            status="draft",
            confidence=min(self._confidence_cap, max(0.0, raw_confidence)),
            created_at=time.time(),
        )

        # Cycle detection — a cyclic plan is an error, NOT a recovery
        # path. The planner refuses to ship it.
        if self._has_cycle(plan_obj):
            raise ValueError(
                f"Planner.plan: detected cyclic depends_on in plan "
                f"{plan_obj.id} for goal {goal_id}"
            )

        return plan_obj

    async def _call_llm_for_plan(
        self, prompt: str
    ) -> tuple[list[dict[str, Any]], float]:
        """Call the injected LLM and parse its JSON reply.

        Tolerates: ``complete`` returning a coroutine OR a value, and
        the JSON being wrapped in ```json fences. On any parse error
        we return ``([], 0.0)`` and let :meth:`plan` mark the plan
        failed.
        """
        try:
            raw = await _call_llm(self._llm, prompt)
        except Exception:
            logger.exception("Planner: LLM call raised; marking plan failed")
            return [], 0.0

        text = _strip_fences(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Planner: LLM returned non-JSON output; marking plan failed")
            return [], 0.0

        if not isinstance(data, dict):
            logger.warning(
                "Planner: LLM JSON not an object (got %s); marking plan failed",
                type(data).__name__,
            )
            return [], 0.0

        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            logger.warning("Planner: LLM JSON missing non-empty 'steps' list")
            return [], 0.0

        confidence_raw = data.get("confidence", 0.5)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.5

        return steps, confidence

    def _materialize_step(
        self,
        raw: dict[str, Any],
        sibling_ids: set[str],
    ) -> PlanStep | None:
        """Convert one LLM step descriptor into a :class:`PlanStep`.

        Skill-invoke preference: when ``skill_registry`` is wired and
        ``intent`` (or ``description``) resolves to a known skill, the
        step is upgraded to ``skill_invoke`` with the skill metadata
        captured in payload. Otherwise it falls back to ``llm_turn``
        (or whatever ``action_kind`` the LLM proposed, if recognised).

        Returns ``None`` (skipped) when the descriptor is too malformed
        to recover — that's preferable to fabricating a step that the
        dispatcher can't run.
        """
        if not isinstance(raw, dict):
            return None

        step_id = str(raw.get("id") or _new_id("step"))
        intent = raw.get("intent") or raw.get("description") or ""
        if not isinstance(intent, str):
            intent = str(intent)

        # depends_on hygiene — only accept refs to siblings already
        # materialised. Forward refs are silently dropped (the LLM
        # frequently invents them in practice).
        raw_deps = raw.get("depends_on") or []
        if not isinstance(raw_deps, (list, tuple)):
            raw_deps = []
        deps = tuple(
            str(d) for d in raw_deps if isinstance(d, str) and d in sibling_ids
        )

        retry_raw = raw.get("retry_policy")
        retry_policy: dict[str, Any]
        if isinstance(retry_raw, dict):
            retry_policy = {
                "max_retries": _coerce_int(retry_raw.get("max_retries", 2), 2),
                "backoff_s": _coerce_float(retry_raw.get("backoff_s", 1.0), 1.0),
            }
            # Forward-compat: keep extra keys.
            for k, v in retry_raw.items():
                if k not in retry_policy:
                    retry_policy[k] = v
        else:
            retry_policy = {"max_retries": 2, "backoff_s": 1.0}

        expected = raw.get("expected_outcome") or ""
        if not isinstance(expected, str):
            expected = str(expected)

        # Try skill match first.
        skill = None
        if self._skill_registry is not None and intent:
            try:
                skill = self._skill_registry.find(intent)
            except Exception:
                logger.exception(
                    "Planner: skill_registry.find raised; falling back to llm_turn"
                )
                skill = None

        if skill is not None:
            payload: dict[str, Any] = {
                "intent": intent,
                "skill_id": getattr(skill, "id", None) or getattr(skill, "name", None),
                "skill_name": getattr(skill, "name", None),
                "skill_args": raw.get("args") or raw.get("payload") or {},
            }
            return PlanStep(
                id=step_id,
                action_kind="skill_invoke",
                payload=payload,
                depends_on=deps,
                expected_outcome=expected,
                retry_policy=retry_policy,
            )

        # Honour an explicit non-default action_kind from the LLM if
        # it picked one we know about; default to llm_turn otherwise.
        explicit_kind = raw.get("action_kind")
        if isinstance(explicit_kind, str) and explicit_kind in _VALID_ACTION_KINDS:
            kind: ActionKind = explicit_kind  # type: ignore[assignment]
        else:
            kind = "llm_turn"

        payload = {
            "intent": intent,
            "prompt": raw.get("prompt") or intent,
            "args": raw.get("args") or raw.get("payload") or {},
        }
        return PlanStep(
            id=step_id,
            action_kind=kind,
            payload=payload,
            depends_on=deps,
            expected_outcome=expected,
            retry_policy=retry_policy,
        )

    # -------------------------------------------------------- execute()

    async def execute(self, plan: Plan, dispatcher: Any) -> PlanResult:
        """Run a plan in topological order with bounded retry + repair.

        ``dispatcher`` is duck-typed: it MUST expose
        ``await dispatcher.dispatch(step: PlanStep) -> dict``. The
        return dict is appended to ``step_results``. The dispatcher
        signals failure by **raising** (we don't introspect the dict
        for an "error" key — exceptions are the contract).

        Retry: each step gets ``retry_policy['max_retries']`` retries
        with ``backoff_s`` linear backoff. After exhaustion we call
        :meth:`repair` exactly **once** per ``execute`` call; the
        repaired plan replaces the in-flight one starting from the
        failed step. A second failure is terminal — we DO NOT loop.
        """
        if plan.status == "failed" or not plan.steps:
            return PlanResult(
                plan_id=plan.id,
                status="failed",
                step_results=(),
                error="empty or failed plan",
            )

        sorted_steps = self._topological_sort(plan)
        results: list[dict[str, Any]] = []
        repair_used = False
        current_plan = plan

        idx = 0
        while idx < len(sorted_steps):
            step = sorted_steps[idx]
            outcome = await self._run_step_with_retry(step, dispatcher)
            if outcome.ok:
                results.append(outcome.output)
                idx += 1
                continue

            # Step failed past its retry budget.
            failure = PlanStepFailure(
                step_id=step.id,
                reason=outcome.error or "unknown failure",
                step_output=outcome.output,
            )

            if repair_used:
                # No second repair — accept failure terminally.
                return PlanResult(
                    plan_id=current_plan.id,
                    status="failed",
                    step_results=tuple(results),
                    error=(
                        f"step {step.id} failed after repair: {failure.reason}"
                    ),
                )

            # First (and only) repair attempt.
            try:
                current_plan = await self.repair(current_plan, failure)
            except Exception as exc:
                logger.exception("Planner: repair raised; aborting execute")
                return PlanResult(
                    plan_id=current_plan.id,
                    status="failed",
                    step_results=tuple(results),
                    error=f"repair raised: {exc}",
                )
            repair_used = True

            if not current_plan.steps:
                return PlanResult(
                    plan_id=current_plan.id,
                    status="failed",
                    step_results=tuple(results),
                    error="repair produced empty plan",
                )

            # Restart sorted execution from the top of the repaired
            # plan. Already-completed results are preserved; we only
            # replay from where we got stuck.
            sorted_steps = self._topological_sort(current_plan)
            idx = 0  # re-execute the repaired plan from scratch
            results = []  # discard pre-repair partials — repair may

            # have reordered or rewritten earlier steps too.

        return PlanResult(
            plan_id=current_plan.id,
            status=("repaired" if repair_used else "completed"),
            step_results=tuple(results),
            error=None,
        )

    async def _run_step_with_retry(
        self, step: PlanStep, dispatcher: Any
    ) -> _StepOutcome:
        """Dispatch one step, honouring its retry_policy."""
        max_retries = _coerce_int(step.retry_policy.get("max_retries", 2), 2)
        backoff_s = _coerce_float(step.retry_policy.get("backoff_s", 1.0), 1.0)
        # Total attempts = 1 initial + max_retries retries.
        attempts = max(1, max_retries + 1)

        last_error: str | None = None
        last_output: dict[str, Any] = {}
        for attempt in range(attempts):
            try:
                output = await dispatcher.dispatch(step)
                if not isinstance(output, dict):
                    output = {"value": output}
                return _StepOutcome(ok=True, output=output, error=None)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_output = {"attempt": attempt, "error": last_error}
                logger.warning(
                    "Planner: step %s attempt %d/%d failed: %s",
                    step.id,
                    attempt + 1,
                    attempts,
                    last_error,
                )
                # Sleep only between attempts, not after the last.
                if attempt < attempts - 1 and backoff_s > 0:
                    await asyncio.sleep(backoff_s)

        return _StepOutcome(ok=False, output=last_output, error=last_error)

    # --------------------------------------------------------- repair()

    async def repair(
        self, failed_plan: Plan, failure: PlanStepFailure
    ) -> Plan:
        """Synthesise a fresh plan informed by a step failure.

        When ``reasoning_engine`` is wired we ask it for analogical
        precedents (``analogical`` is a Phase 6.2 ReasoningEngine
        method) and feed them as grounding to the LLM. Failures of
        the reasoning call are logged and suppressed — repair must
        still proceed without that grounding.

        The returned plan inherits the original ``goal_id`` and is
        flagged ``status="repaired"``; its confidence is the original's
        minus 0.1, floored at 0.0 (each repair lowers our claim by
        design).
        """
        grounding_blob = await self._fetch_repair_grounding(failed_plan, failure)

        prompt = _build_repair_prompt(
            failed_plan=failed_plan,
            failure=failure,
            grounding=grounding_blob,
        )

        raw_steps, raw_confidence = await self._call_llm_for_plan(prompt)
        if not raw_steps:
            # No new plan — return a status-marked empty so the caller
            # treats it as a failure, not as a no-op.
            return replace(
                failed_plan,
                steps=(),
                status="failed",
                confidence=0.0,
            )

        steps: list[PlanStep] = []
        seen: set[str] = set()
        for raw in raw_steps:
            step = self._materialize_step(raw, sibling_ids=seen)
            if step is None:
                continue
            steps.append(step)
            seen.add(step.id)

        if not steps:
            return replace(
                failed_plan,
                steps=(),
                status="failed",
                confidence=0.0,
            )

        # Repair lowers confidence by 0.1 (a small honest penalty)
        # and is still capped by confidence_cap.
        repaired_confidence = max(0.0, min(self._confidence_cap, raw_confidence - 0.1))

        repaired = Plan(
            id=_new_id("plan"),
            goal_id=failed_plan.goal_id,
            steps=tuple(steps),
            status="repaired",
            confidence=repaired_confidence,
            created_at=time.time(),
        )

        if self._has_cycle(repaired):
            raise ValueError(
                f"Planner.repair: produced cyclic depends_on for "
                f"goal {failed_plan.goal_id}"
            )

        return repaired

    async def _fetch_repair_grounding(
        self, failed_plan: Plan, failure: PlanStepFailure
    ) -> str:
        """Best-effort grounding from the optional reasoning engine.

        The engine API is duck-typed; we try ``analogical`` then
        ``reason(mode="analogical")`` and accept whatever string-
        renderable thing comes back. Any exception is swallowed —
        repair MUST still run when the reasoning engine is missing or
        broken.
        """
        if self._reasoning_engine is None:
            return ""

        query = (
            f"goal={failed_plan.goal_id}; failed_step={failure.step_id}; "
            f"reason={failure.reason}"
        )
        try:
            fn = getattr(self._reasoning_engine, "analogical", None)
            if callable(fn):
                result = await fn(query)
            else:
                fn = getattr(self._reasoning_engine, "reason", None)
                if not callable(fn):
                    return ""
                result = await fn(query, mode="analogical")
        except Exception:
            logger.exception(
                "Planner.repair: reasoning_engine call raised; "
                "continuing without grounding"
            )
            return ""

        if result is None:
            return ""
        try:
            return str(result)
        except Exception:
            return ""

    # --------------------------------------------------------- topology

    def _topological_sort(self, plan: Plan) -> list[PlanStep]:
        """Return steps in a dependency-respecting linear order.

        Kahn's algorithm. Ties broken by the step's index in the
        original tuple — stable, deterministic. Raises ``ValueError``
        if a cycle slips through (defensive — :meth:`plan` already
        rejects cyclic plans, but :meth:`execute` re-checks).
        """
        if not plan.steps:
            return []

        by_id: dict[str, PlanStep] = {s.id: s for s in plan.steps}
        order_index: dict[str, int] = {
            s.id: i for i, s in enumerate(plan.steps)
        }
        indegree: dict[str, int] = {sid: 0 for sid in by_id}
        children: dict[str, list[str]] = {sid: [] for sid in by_id}
        for s in plan.steps:
            for dep in s.depends_on:
                if dep not in by_id:
                    # Forward / dangling refs are dropped during
                    # materialise, but be defensive here too.
                    continue
                indegree[s.id] += 1
                children[dep].append(s.id)

        ready = sorted(
            [sid for sid, d in indegree.items() if d == 0],
            key=lambda sid: order_index[sid],
        )

        ordered: list[PlanStep] = []
        while ready:
            sid = ready.pop(0)
            ordered.append(by_id[sid])
            for child in children[sid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    # Insert maintaining stable original order.
                    target = order_index[child]
                    pos = 0
                    while pos < len(ready) and order_index[ready[pos]] < target:
                        pos += 1
                    ready.insert(pos, child)

        if len(ordered) != len(plan.steps):
            raise ValueError(
                f"Plan {plan.id} has a dependency cycle (sorted "
                f"{len(ordered)} of {len(plan.steps)} steps)"
            )
        return ordered

    def _has_cycle(self, plan: Plan) -> bool:
        """True if any depends_on chain forms a cycle.

        Iterative DFS with white / grey / black coloring — no
        recursion so we don't trip RecursionError on big plans.
        """
        if not plan.steps:
            return False
        by_id: dict[str, PlanStep] = {s.id: s for s in plan.steps}
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {sid: WHITE for sid in by_id}

        for start in by_id:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                node, child_idx = stack[-1]
                if child_idx == 0:
                    color[node] = GREY
                deps = by_id[node].depends_on
                if child_idx >= len(deps):
                    color[node] = BLACK
                    stack.pop()
                    continue
                # Advance the iterator on the parent frame.
                stack[-1] = (node, child_idx + 1)
                child = deps[child_idx]
                if child not in by_id:
                    continue
                if color[child] == GREY:
                    return True
                if color[child] == WHITE:
                    stack.append((child, 0))
        return False


# --------------------------------------------------------------- helpers


@dataclass(frozen=True)
class _StepOutcome:
    ok: bool
    output: dict[str, Any]
    error: str | None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _extract_goal_id(goal: Any) -> str:
    """Coerce a goal-like thing into its id string."""
    gid = getattr(goal, "id", None)
    if isinstance(gid, str) and gid:
        return gid
    if isinstance(goal, dict):
        v = goal.get("id")
        if isinstance(v, str) and v:
            return v
    return _new_id("goal")


def _goal_to_prompt_blob(goal: Any) -> dict[str, Any]:
    """Render the goal's public surface as JSON-friendly fields.

    Tolerates dataclasses, dicts, and arbitrary objects with the
    common ``name`` / ``description`` / ``priority`` attributes.
    """
    if isinstance(goal, dict):
        return {
            "id": goal.get("id"),
            "name": goal.get("name") or goal.get("description") or "",
            "description": goal.get("description") or "",
            "priority": goal.get("priority"),
            "completion_criteria": goal.get("completion_criteria") or {},
        }
    return {
        "id": getattr(goal, "id", None),
        "name": getattr(goal, "name", None) or getattr(goal, "description", "") or "",
        "description": getattr(goal, "description", "") or "",
        "priority": getattr(goal, "priority", None),
        "completion_criteria": getattr(goal, "completion_criteria", None) or {},
    }


def _build_planning_prompt(goal_blob: dict[str, Any]) -> str:
    """Construct the JSON-shaped prompt for :meth:`Planner.plan`."""
    return (
        "You are an HTN planner. Decompose this goal into atomic steps.\n\n"
        f"GOAL:\n{json.dumps(goal_blob, ensure_ascii=False, indent=2)}\n\n"
        "Reply with JSON ONLY (no prose, no fences) shaped as:\n"
        "{\n"
        '  "steps": [\n'
        '    {\n'
        '      "id": "step_1",\n'
        '      "intent": "<short verb phrase>",\n'
        '      "action_kind": "llm_turn|skill_invoke|tool_call|wait_for_percept",\n'
        '      "payload": {...},\n'
        '      "depends_on": ["step_0"],\n'
        '      "expected_outcome": "<what success looks like>",\n'
        '      "retry_policy": {"max_retries": 2, "backoff_s": 1.0}\n'
        '    }\n'
        '  ],\n'
        '  "confidence": 0.6\n'
        "}\n"
    )


def _build_repair_prompt(
    *,
    failed_plan: Plan,
    failure: PlanStepFailure,
    grounding: str,
) -> str:
    """Construct the JSON-shaped repair prompt."""
    failed_steps = [
        {
            "id": s.id,
            "intent": s.payload.get("intent", ""),
            "action_kind": s.action_kind,
            "expected_outcome": s.expected_outcome,
        }
        for s in failed_plan.steps
    ]
    blob = {
        "goal_id": failed_plan.goal_id,
        "original_steps": failed_steps,
        "failure": {
            "step_id": failure.step_id,
            "reason": failure.reason,
            "step_output": failure.step_output,
        },
        "analogical_grounding": grounding,
    }
    return (
        "An HTN plan failed. Produce a REPAIRED plan that avoids the "
        "failure mode. Reply with JSON ONLY in the same schema as plan().\n\n"
        f"FAILURE_CONTEXT:\n{json.dumps(blob, ensure_ascii=False, indent=2)}\n"
    )


async def _call_llm(llm: Any, prompt: str) -> str:
    """Invoke the LLM in the most permissive way the duck supports.

    Tries ``llm.complete(prompt, response_format='json')`` first; falls
    back to ``llm.complete(prompt)`` then ``llm(prompt)``. Awaits the
    return value if it is a coroutine.
    """
    fn = getattr(llm, "complete", None)
    if callable(fn):
        try:
            result = fn(prompt, response_format="json")
        except TypeError:
            result = fn(prompt)
    elif callable(llm):
        result = llm(prompt)
    else:
        raise TypeError("Planner: llm has neither .complete nor __call__")

    if asyncio.iscoroutine(result):
        result = await result

    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result)
    return str(result)


def _strip_fences(text: str) -> str:
    """Remove ```json … ``` fences a chatty LLM may have emitted."""
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (with optional language tag).
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        # Drop trailing fence.
        if s.endswith("```"):
            s = s[: -3].rstrip()
    return s.strip()


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "ActionKind",
    "Plan",
    "PlanResult",
    "PlanStatus",
    "PlanStep",
    "PlanStepFailure",
    "Planner",
]
