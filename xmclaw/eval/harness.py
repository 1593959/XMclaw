"""Benchmark harness — dataclasses + ABC + Runner.

The harness abstracts three concerns so an A/B test on a suite is a
3-step ritual: (1) pick a ``BenchmarkSuite`` by id, (2) hand the
``Runner`` a factory that builds a fresh agent for each task, (3) read
the ``SuiteResult`` (pass_rate, mean_score, total cost/latency, plus
per-task ``TaskResult``s).

The ABC keeps suites self-describing: each suite owns its task list and
its grading function. New benchmarks plug in by subclassing
``BenchmarkSuite``; the Runner does not need changes.

Per-task isolation is the load-bearing invariant: the Runner builds a
fresh agent (via ``agent_factory()``) for every task so memory or hop
state from one task cannot bleed into the next, AND so a single task
that raises does not poison the rest of the suite — exceptions are
caught and surface as ``TaskResult(passed=False, error=...)``.

The Runner deliberately does NOT touch ``xmclaw.daemon`` — it accepts
a plain callable that returns "something with an ``arun`` / ``run_turn``
method" so tests can pass mocks and real CLI usage can pass an
``AgentLoop`` built via ``xmclaw.daemon.factory.build_agent_from_config``
(the *caller* — not this module — does that import).
"""
from __future__ import annotations

import abc
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


# ── Result / case dataclasses ──────────────────────────────────────────


@dataclass(frozen=True)
class TaskCase:
    """One benchmark task: prompt + ground-truth signals.

    ``expected_signals`` is suite-specific — the LongMemEval mini suite
    uses ``{"answer": "<ground truth string>"}``; SWE-bench-style suites
    might use ``{"unified_diff": "..."}``. The Runner does not interpret
    this field; the suite's ``grade()`` does.
    """

    task_id: str
    prompt: str
    expected_signals: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    """Per-task outcome after grading."""

    task_id: str
    agent_text: str
    passed: bool
    score: float
    turns: int
    cost_usd: float
    latency_s: float
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SuiteResult:
    """Aggregated result of running a whole suite."""

    suite_id: str
    n_tasks: int
    n_passed: int
    pass_rate: float
    mean_score: float
    total_cost_usd: float
    total_latency_s: float
    results: tuple[TaskResult, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view — used by the CLI's ``--out`` writer."""
        return {
            "suite_id": self.suite_id,
            "n_tasks": self.n_tasks,
            "n_passed": self.n_passed,
            "pass_rate": self.pass_rate,
            "mean_score": self.mean_score,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_s": self.total_latency_s,
            "results": [
                {
                    "task_id": r.task_id,
                    "agent_text": r.agent_text,
                    "passed": r.passed,
                    "score": r.score,
                    "turns": r.turns,
                    "cost_usd": r.cost_usd,
                    "latency_s": r.latency_s,
                    "error": r.error,
                    "metadata": r.metadata,
                }
                for r in self.results
            ],
            "metadata": self.metadata,
        }


# ── Agent protocol ─────────────────────────────────────────────────────


@runtime_checkable
class _AgentLike(Protocol):
    """Anything Runner can drive. Real ``AgentLoop`` matches; mocks may
    too. We accept either an async ``arun`` (preferred new-style) or
    the existing ``run_turn(session_id, user_message)`` shape, and
    auto-adapt. This keeps the harness decoupled from agent_loop.py
    refactors."""

    # Marker only — the Runner does an ``hasattr`` dance, not isinstance.


# ── Suite ABC ──────────────────────────────────────────────────────────


class BenchmarkSuite(abc.ABC):
    """ABC for a benchmark.

    A suite is responsible for two things:

    * Producing its task list (``load_tasks``). Implementations may pull
      from an HF dataset, parse a local JSON, or — for the mini suites
      shipped here — return hand-coded fixtures.
    * Grading a single agent response (``grade``). The grader returns a
      ``(passed, score_0_to_1, metadata)`` triple; the Runner aggregates.

    A suite is otherwise stateless — the same instance can be re-used
    across A/B runs.
    """

    @property
    @abc.abstractmethod
    def suite_id(self) -> str:
        """Stable id used in CLI / SuiteResult / registry lookup."""

    @abc.abstractmethod
    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        """Return up to ``limit`` ``TaskCase``s, or all if ``limit`` is None."""

    @abc.abstractmethod
    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Grade a single response. Returns ``(passed, score, metadata)``.

        ``score`` is a float in ``[0.0, 1.0]``; ``passed`` is a binary
        verdict the suite picks (the threshold is suite-specific).
        ``extra`` is a kwargs bag for future grader inputs (cost, hops,
        token usage) without breaking subclasses today.
        """


# ── Runner ─────────────────────────────────────────────────────────────


AgentFactory = Callable[[], Any]
"""``() -> agent_like``. Called once per task so tasks see fresh state."""


class Runner:
    """Drive a ``BenchmarkSuite`` against an agent.

    The Runner takes a *factory* (not an agent instance) because we
    want per-task isolation: each task gets its own agent context,
    free of any state accreted by previous tasks. If the user has a
    long-lived agent and wants per-task isolation handled elsewhere,
    they can wrap it in a no-op factory: ``lambda: shared_agent``.
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        suite: BenchmarkSuite,
        *,
        per_task_timeout_s: float = 600.0,
    ) -> None:
        if not callable(agent_factory):
            raise TypeError(
                f"agent_factory must be callable, got {type(agent_factory).__name__}"
            )
        self._agent_factory = agent_factory
        self._suite = suite
        # 2026-06-16 robustness: a per-task wall-clock cap. Without it ONE
        # hung agentic task (a stalled LLM hop against a slow/dead endpoint)
        # wedges the WHOLE suite indefinitely — observed: a 6-task run stuck
        # for a full day on task 6. On timeout the task is failed and the
        # suite moves on, so a baseline always finishes in bounded time.
        self._per_task_timeout_s = max(1.0, float(per_task_timeout_s))

    async def run(self, limit: int | None = None) -> SuiteResult:
        """Run every task in the suite (up to ``limit``) and return a
        ``SuiteResult``.

        Catches per-task exceptions. A failure in task N does not
        prevent task N+1 from running — the suite continues, and the
        failed task surfaces as ``TaskResult(passed=False, error=...)``.
        """
        cases = self._suite.load_tasks(limit=limit)
        results: list[TaskResult] = []
        for case in cases:
            results.append(await self._run_one(case))

        n_passed = sum(1 for r in results if r.passed)
        n_tasks = len(results)
        pass_rate = (n_passed / n_tasks) if n_tasks else 0.0
        mean_score = (
            sum(r.score for r in results) / n_tasks if n_tasks else 0.0
        )
        total_cost = sum(r.cost_usd for r in results)
        total_latency = sum(r.latency_s for r in results)
        return SuiteResult(
            suite_id=self._suite.suite_id,
            n_tasks=n_tasks,
            n_passed=n_passed,
            pass_rate=pass_rate,
            mean_score=mean_score,
            total_cost_usd=total_cost,
            total_latency_s=total_latency,
            results=tuple(results),
        )

    async def _run_one(self, case: TaskCase) -> TaskResult:
        start = time.monotonic()
        try:
            agent = self._agent_factory()
        except Exception as exc:  # noqa: BLE001
            latency = time.monotonic() - start
            return TaskResult(
                task_id=case.task_id,
                agent_text="",
                passed=False,
                score=0.0,
                turns=0,
                cost_usd=0.0,
                latency_s=latency,
                error=f"agent_factory raised: {exc!r}",
            )

        try:
            agent_text, turns, cost = await asyncio.wait_for(
                self._invoke_agent(agent, case),
                timeout=self._per_task_timeout_s,
            )
        except asyncio.TimeoutError:
            latency = time.monotonic() - start
            return TaskResult(
                task_id=case.task_id,
                agent_text="",
                passed=False,
                score=0.0,
                turns=0,
                cost_usd=0.0,
                latency_s=latency,
                error=(
                    f"task exceeded {self._per_task_timeout_s:.0f}s wall-clock "
                    f"and was aborted (likely a stalled LLM hop) — suite continues"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            latency = time.monotonic() - start
            return TaskResult(
                task_id=case.task_id,
                agent_text="",
                passed=False,
                score=0.0,
                turns=0,
                cost_usd=0.0,
                latency_s=latency,
                error=f"agent raised: {exc!r}",
            )

        latency = time.monotonic() - start
        try:
            passed, score, grade_meta = self._suite.grade(case, agent_text)
        except Exception as exc:  # noqa: BLE001
            return TaskResult(
                task_id=case.task_id,
                agent_text=agent_text,
                passed=False,
                score=0.0,
                turns=turns,
                cost_usd=cost,
                latency_s=latency,
                error=f"grader raised: {exc!r}",
            )

        # Clamp score defensively — a buggy grader returning 1.4 must not
        # make mean_score > 1.0 silently.
        if score < 0.0:
            score = 0.0
        elif score > 1.0:
            score = 1.0

        return TaskResult(
            task_id=case.task_id,
            agent_text=agent_text,
            passed=bool(passed),
            score=float(score),
            turns=turns,
            cost_usd=cost,
            latency_s=latency,
            metadata=dict(grade_meta) if grade_meta else {},
        )

    async def _invoke_agent(
        self, agent: Any, case: TaskCase,
    ) -> tuple[str, int, float]:
        """Call into the agent and pull out (text, turns, cost_usd).

        Three calling conventions are supported, in order:

        1. ``arun(prompt: str) -> str | dict`` — preferred for tests.
           If a dict is returned, we look for ``text`` / ``turns`` /
           ``cost_usd`` keys.
        2. ``run_turn(session_id, prompt) -> AgentTurnResult-like``
           with ``.text`` and ``.hops``. Used by the real
           ``xmclaw.daemon.agent_loop.AgentLoop`` from the CLI.
        3. ``__call__(prompt) -> str`` — last-resort sync fallback so a
           ``lambda p: "..."`` works as a stub agent in unit tests.
        """
        # Path 1: arun
        arun = getattr(agent, "arun", None)
        if arun is not None:
            res = await self._await_maybe(arun(case.prompt))
            return _normalise_agent_output(res)

        # Path 2: run_turn (AgentLoop)
        run_turn = getattr(agent, "run_turn", None)
        if run_turn is not None:
            session_id = f"eval-{case.task_id}-{uuid.uuid4().hex[:8]}"
            res = await self._await_maybe(run_turn(session_id, case.prompt))
            text = getattr(res, "text", "") or ""
            turns = int(getattr(res, "hops", 0) or 0)
            cost = float(getattr(res, "cost_usd", 0.0) or 0.0)
            return text, turns, cost

        # Path 3: callable stub
        if callable(agent):
            res = await self._await_maybe(agent(case.prompt))
            return _normalise_agent_output(res)

        raise TypeError(
            f"agent_factory produced {type(agent).__name__} which has "
            "no arun() / run_turn() / __call__ — Runner cannot drive it"
        )

    @staticmethod
    async def _await_maybe(maybe_awaitable: Any) -> Any:
        """Accept both async and sync agent results without forking the
        call sites above."""
        if asyncio.iscoroutine(maybe_awaitable) or isinstance(
            maybe_awaitable, Awaitable
        ):
            return await maybe_awaitable
        return maybe_awaitable


def _normalise_agent_output(res: Any) -> tuple[str, int, float]:
    """Coerce stub agent return values to ``(text, turns, cost_usd)``.

    Supports ``str`` (text only) and ``dict`` (richer telemetry). The
    real AgentLoop path doesn't go through here — see ``Runner._invoke_agent``
    path 2 — so this only deals with mock shapes.
    """
    if isinstance(res, str):
        return res, 1, 0.0
    if isinstance(res, dict):
        text = str(res.get("text", "") or "")
        turns = int(res.get("turns", 1) or 1)
        cost = float(res.get("cost_usd", 0.0) or 0.0)
        return text, turns, cost
    # Unknown shape — best-effort stringify.
    return str(res), 1, 0.0


__all__ = [
    "AgentFactory",
    "BenchmarkSuite",
    "Runner",
    "SuiteResult",
    "TaskCase",
    "TaskResult",
]
