"""SelfExperimentLoop — Jarvis Phase 6.5.

Propose a hypothesis ("treatment X improves metric M by >= 10%"), run
the *same* Sprint 4 benchmark suite twice (baseline vs treatment) via
the existing :class:`xmclaw.eval.harness.Runner`, compute the delta and
a Welch's t-test p-value, then decide ``adopt`` / ``reject`` /
``extend`` / ``abort``.

Key reuse: the entire A/B numbers come from re-using the Sprint 4
harness — we hand the ``Runner`` two different ``agent_factory``
callables (one for baseline behaviour, one for the experimental
treatment) and diff the resulting ``SuiteResult.pass_rate`` (or
``mean_score``, depending on ``Experiment.metric``). **No new test
framework is created here.**

Iron Rule #2 (staging-gated promotion):
    ``adopt()`` does NOT call ``SkillRegistry.promote(...)`` directly.
    On a positive decision we *log a staged adoption event* — the
    actual registry mutation is deferred to a future caller (e.g. the
    EvolutionOrchestrator), which independently re-validates the
    evidence before flipping HEAD. This module is the experiment runner,
    not the deployer.

Persistence: experiments + results live in
``~/.xmclaw/v2/experiments.db`` (SQLite, two tables). The path is
overridable via the ``db_path`` constructor argument so tests can use
``tmp_path``.

Stdlib-only stats: the Welch's t-test is implemented in
:func:`SelfExperimentLoop._compute_p_value` using ``math`` and
``statistics`` — no scipy dependency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


logger = logging.getLogger(__name__)


ExperimentDecision = Literal["adopt", "reject", "extend", "abort"]


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Experiment:
    """A planned A/B comparison.

    ``intervention`` is opaque metadata describing what the treatment
    does (e.g. ``{"skill_id": "x", "from_v": 3, "to_v": 4}``); we never
    interpret it here — the caller's ``treatment_agent_factory`` is
    what actually realises the change.
    """

    id: str
    hypothesis: str
    intervention: dict[str, Any]
    metric: str
    baseline_metric_value: float
    holdout_set_size: int
    started_at: float
    suite_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hypothesis": self.hypothesis,
            "intervention": dict(self.intervention),
            "metric": self.metric,
            "baseline_metric_value": self.baseline_metric_value,
            "holdout_set_size": self.holdout_set_size,
            "started_at": self.started_at,
            "suite_id": self.suite_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Experiment":
        return cls(
            id=str(data["id"]),
            hypothesis=str(data["hypothesis"]),
            intervention=dict(data.get("intervention") or {}),
            metric=str(data["metric"]),
            baseline_metric_value=float(data["baseline_metric_value"]),
            holdout_set_size=int(data["holdout_set_size"]),
            started_at=float(data["started_at"]),
            suite_id=str(data["suite_id"]),
        )


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Outcome of running an :class:`Experiment` end-to-end."""

    experiment_id: str
    baseline_value: float
    treatment_value: float
    delta: float
    delta_p_value: float
    n_baseline: int
    n_treatment: int
    decision: ExperimentDecision
    decision_reason: str
    finished_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "baseline_value": self.baseline_value,
            "treatment_value": self.treatment_value,
            "delta": self.delta,
            "delta_p_value": self.delta_p_value,
            "n_baseline": self.n_baseline,
            "n_treatment": self.n_treatment,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "finished_at": self.finished_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentResult":
        return cls(
            experiment_id=str(data["experiment_id"]),
            baseline_value=float(data["baseline_value"]),
            treatment_value=float(data["treatment_value"]),
            delta=float(data["delta"]),
            delta_p_value=float(data["delta_p_value"]),
            n_baseline=int(data["n_baseline"]),
            n_treatment=int(data["n_treatment"]),
            decision=str(data["decision"]),  # type: ignore[arg-type]
            decision_reason=str(data["decision_reason"]),
            finished_at=float(data["finished_at"]),
            metadata=dict(data.get("metadata") or {}),
        )


# ── Persistence ────────────────────────────────────────────────────────


def _default_db_path() -> Path:
    # Patch A (2026-05-10): delegate to paths.py so XMC_DATA_DIR /
    # XMC_V2_EXPERIMENTS_DB_PATH overrides reroute the file.
    from xmclaw.utils.paths import default_experiments_db_path
    return default_experiments_db_path()


class ExperimentStore:
    """SQLite-backed log of experiments and their results.

    Schema:
      * ``experiments(id PRIMARY KEY, payload TEXT)`` — JSON blob of the
        :class:`Experiment` dataclass.
      * ``experiment_results(experiment_id PRIMARY KEY, payload TEXT)``
        — JSON blob of the :class:`ExperimentResult`.

    A JSON-blob schema is intentional: experiment shape will evolve and
    we do not want to migrate columns every Phase 6 sub-iteration.
    """

    def __init__(self, db_path: Any | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    started_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS experiment_results (
                    experiment_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    finished_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_results_decision "
                "ON experiment_results(decision)"
            )
            conn.commit()

    async def save_experiment(self, exp: Experiment) -> None:
        await asyncio.to_thread(self._save_experiment_sync, exp)

    def _save_experiment_sync(self, exp: Experiment) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiments(id, payload, started_at) "
                "VALUES(?, ?, ?)",
                (exp.id, json.dumps(exp.to_dict()), exp.started_at),
            )
            conn.commit()

    async def save_result(self, res: ExperimentResult) -> None:
        await asyncio.to_thread(self._save_result_sync, res)

    def _save_result_sync(self, res: ExperimentResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiment_results"
                "(experiment_id, decision, payload, finished_at) VALUES(?, ?, ?, ?)",
                (
                    res.experiment_id,
                    res.decision,
                    json.dumps(res.to_dict()),
                    res.finished_at,
                ),
            )
            conn.commit()

    async def get_experiment(self, eid: str) -> Experiment | None:
        return await asyncio.to_thread(self._get_experiment_sync, eid)

    def _get_experiment_sync(self, eid: str) -> Experiment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM experiments WHERE id = ?", (eid,)
            ).fetchone()
        if row is None:
            return None
        return Experiment.from_dict(json.loads(row["payload"]))

    async def get_result(self, eid: str) -> ExperimentResult | None:
        return await asyncio.to_thread(self._get_result_sync, eid)

    def _get_result_sync(self, eid: str) -> ExperimentResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM experiment_results WHERE experiment_id = ?",
                (eid,),
            ).fetchone()
        if row is None:
            return None
        return ExperimentResult.from_dict(json.loads(row["payload"]))

    async def list_experiments(
        self,
        decision: ExperimentDecision | None = None,
        limit: int = 50,
    ) -> list[tuple[Experiment, ExperimentResult | None]]:
        return await asyncio.to_thread(self._list_experiments_sync, decision, limit)

    def _list_experiments_sync(
        self,
        decision: ExperimentDecision | None,
        limit: int,
    ) -> list[tuple[Experiment, ExperimentResult | None]]:
        with self._connect() as conn:
            if decision is None:
                rows = conn.execute(
                    "SELECT e.payload AS exp_payload, r.payload AS res_payload "
                    "FROM experiments e "
                    "LEFT JOIN experiment_results r "
                    "ON r.experiment_id = e.id "
                    "ORDER BY e.started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT e.payload AS exp_payload, r.payload AS res_payload "
                    "FROM experiments e "
                    "INNER JOIN experiment_results r "
                    "ON r.experiment_id = e.id "
                    "WHERE r.decision = ? "
                    "ORDER BY e.started_at DESC LIMIT ?",
                    (decision, limit),
                ).fetchall()
        out: list[tuple[Experiment, ExperimentResult | None]] = []
        for row in rows:
            exp = Experiment.from_dict(json.loads(row["exp_payload"]))
            res_payload = row["res_payload"]
            res = (
                ExperimentResult.from_dict(json.loads(res_payload))
                if res_payload
                else None
            )
            out.append((exp, res))
        return out


# ── The loop ───────────────────────────────────────────────────────────


class _RunnerInfraError(Exception):
    """Raised by :meth:`SelfExperimentLoop.execute` when the harness or
    one of the factories blows up before a clean comparison is possible.
    Triggers a ``decision='abort'`` outcome — the experiment is *not*
    counted as a reject, because we never got a fair signal."""


class SelfExperimentLoop:
    """Reuses Sprint 4 harness — proposes, runs, decides.

    This module deliberately knows nothing about ``Runner`` /
    ``BenchmarkSuite`` shapes at type-check time. We accept ``Any`` for
    the suite + factories so ``xmclaw/cognition`` does not have to
    reach into ``xmclaw/eval`` (which itself sits *above* cognition in
    the layer cake — eval imports core/providers; cognition currently
    imports nothing from eval). Duck typing keeps the dependency graph
    pointed downward.

    Decision policy (single source of truth, used by :meth:`execute`):
      * delta >= ``adopt_min_delta`` AND p < ``p_value_threshold``
        → ``adopt``  ("treatment is materially and significantly better")
      * 0 <= delta < ``adopt_min_delta`` AND p < 0.10
        → ``extend`` ("trending positive, more data needed")
      * delta < 0 OR p >= ``p_value_threshold`` (and not extend)
        → ``reject``
      * infrastructure error (factory or runner raises)
        → ``abort``
    """

    EXTEND_P_VALUE_THRESHOLD = 0.10

    def __init__(
        self,
        store: ExperimentStore | None = None,
        adopt_min_delta: float = 0.10,
        p_value_threshold: float = 0.05,
    ) -> None:
        self._store = store if store is not None else ExperimentStore()
        if adopt_min_delta <= 0:
            raise ValueError("adopt_min_delta must be > 0")
        if not 0 < p_value_threshold < 1:
            raise ValueError("p_value_threshold must be in (0, 1)")
        self._adopt_min_delta = adopt_min_delta
        self._p_value_threshold = p_value_threshold
        # Caller can register a sink to be notified on staged adoptions.
        # See Iron Rule #2 — we never call registry.promote ourselves.
        self._staging_sinks: list[Callable[[ExperimentResult], Any]] = []

    @property
    def store(self) -> ExperimentStore:
        return self._store

    @property
    def adopt_min_delta(self) -> float:
        return self._adopt_min_delta

    @property
    def p_value_threshold(self) -> float:
        return self._p_value_threshold

    def add_staging_sink(self, sink: Callable[[ExperimentResult], Any]) -> None:
        """Register a callback that fires (sync or async) on adopt."""
        self._staging_sinks.append(sink)

    async def propose(
        self,
        hypothesis: str,
        intervention: dict[str, Any],
        metric: str,
        suite_id: str,
        baseline_value: float,
        holdout_set_size: int = 30,
    ) -> Experiment:
        """Mint an :class:`Experiment` (id + started_at) and persist it.

        Persisting at *propose time* — before any run — means an aborted
        execute() still leaves a row, so the audit log doesn't "lose"
        crashed experiments.
        """
        exp = Experiment(
            id=uuid.uuid4().hex,
            hypothesis=hypothesis,
            intervention=dict(intervention),
            metric=metric,
            baseline_metric_value=float(baseline_value),
            holdout_set_size=int(holdout_set_size),
            started_at=time.time(),
            suite_id=suite_id,
        )
        await self._store.save_experiment(exp)
        return exp

    async def execute(
        self,
        exp: Experiment,
        baseline_agent_factory: Callable[[], Any],
        treatment_agent_factory: Callable[[], Any],
        load_suite: Callable[[str], Any],
    ) -> ExperimentResult:
        """Run the suite under both factories, decide, persist.

        ``load_suite`` is a thunk: ``suite_id -> BenchmarkSuite``. Tests
        pass a closure that returns a mock; production wiring will pass
        ``lambda sid: SUITE_REGISTRY[sid](...)``. Keeping it injected
        avoids importing ``xmclaw.eval`` here.
        """
        try:
            baseline_result = await self._run_one_arm(
                load_suite, exp.suite_id, baseline_agent_factory, exp.holdout_set_size
            )
            treatment_result = await self._run_one_arm(
                load_suite, exp.suite_id, treatment_agent_factory, exp.holdout_set_size
            )
        except _RunnerInfraError as exc:
            return await self._record_abort(exp, str(exc))
        except Exception as exc:  # noqa: BLE001
            return await self._record_abort(exp, f"unexpected: {exc!r}")

        baseline_scores = _scores_for_metric(baseline_result, exp.metric)
        treatment_scores = _scores_for_metric(treatment_result, exp.metric)

        if not baseline_scores or not treatment_scores:
            return await self._record_abort(
                exp, "empty score series (suite returned no tasks)"
            )

        baseline_value = statistics.fmean(baseline_scores)
        treatment_value = statistics.fmean(treatment_scores)
        delta = treatment_value - baseline_value
        p_value = self._compute_p_value(baseline_scores, treatment_scores)

        decision, reason = self._decide(delta, p_value)
        result = ExperimentResult(
            experiment_id=exp.id,
            baseline_value=baseline_value,
            treatment_value=treatment_value,
            delta=delta,
            delta_p_value=p_value,
            n_baseline=len(baseline_scores),
            n_treatment=len(treatment_scores),
            decision=decision,
            decision_reason=reason,
            finished_at=time.time(),
            metadata={"metric": exp.metric, "suite_id": exp.suite_id},
        )
        await self._store.save_result(result)
        if decision == "adopt":
            await self.adopt(result)
        return result

    async def _run_one_arm(
        self,
        load_suite: Callable[[str], Any],
        suite_id: str,
        agent_factory: Callable[[], Any],
        limit: int,
    ) -> Any:
        """Drive the Sprint 4 ``Runner`` once. Wraps every infra error
        in :class:`_RunnerInfraError` so the caller can map them to
        ``decision='abort'`` without ambiguity."""
        try:
            suite = load_suite(suite_id)
        except Exception as exc:  # noqa: BLE001
            raise _RunnerInfraError(
                f"load_suite({suite_id!r}) raised: {exc!r}"
            ) from exc
        try:
            # Lazy import — the test suite hands its own mock and never
            # exercises this branch; real callers do.
            from xmclaw.eval.harness import Runner as _Runner

            runner = _Runner(agent_factory, suite)
        except Exception as exc:  # noqa: BLE001
            raise _RunnerInfraError(f"Runner construction failed: {exc!r}") from exc

        try:
            return await runner.run(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _RunnerInfraError(f"runner.run raised: {exc!r}") from exc

    def _decide(
        self, delta: float, p_value: float,
    ) -> tuple[ExperimentDecision, str]:
        if delta >= self._adopt_min_delta and p_value < self._p_value_threshold:
            return (
                "adopt",
                f"delta={delta:.3f} >= {self._adopt_min_delta:.3f} "
                f"and p={p_value:.3f} < {self._p_value_threshold:.3f}",
            )
        if delta < 0:
            return (
                "reject",
                f"delta={delta:.3f} < 0 (treatment regressed)",
            )
        if p_value >= self._p_value_threshold and delta >= self._adopt_min_delta:
            # Big delta but not significant — treat as reject (could be noise).
            return (
                "reject",
                f"delta={delta:.3f} but p={p_value:.3f} >= "
                f"{self._p_value_threshold:.3f} (not significant)",
            )
        if 0 <= delta < self._adopt_min_delta and p_value < self.EXTEND_P_VALUE_THRESHOLD:
            return (
                "extend",
                f"delta={delta:.3f} in [0, {self._adopt_min_delta:.3f}) "
                f"and p={p_value:.3f} < {self.EXTEND_P_VALUE_THRESHOLD:.3f} "
                f"(promising — needs more data)",
            )
        return (
            "reject",
            f"delta={delta:.3f}, p={p_value:.3f} (insufficient evidence)",
        )

    async def _record_abort(
        self, exp: Experiment, reason: str,
    ) -> ExperimentResult:
        result = ExperimentResult(
            experiment_id=exp.id,
            baseline_value=exp.baseline_metric_value,
            treatment_value=exp.baseline_metric_value,
            delta=0.0,
            delta_p_value=1.0,
            n_baseline=0,
            n_treatment=0,
            decision="abort",
            decision_reason=f"infrastructure error: {reason}",
            finished_at=time.time(),
            metadata={"metric": exp.metric, "suite_id": exp.suite_id},
        )
        await self._store.save_result(result)
        return result

    @staticmethod
    def _compute_p_value(
        baseline_scores: list[float], treatment_scores: list[float],
    ) -> float:
        """Welch's t-test (two-tailed) between two independent samples
        with possibly unequal variances. Returns the p-value.

        Implementation: t = (m1 - m2) / sqrt(s1^2/n1 + s2^2/n2),
        df = Welch–Satterthwaite, p = 2 * (1 - CDF_t(|t|, df)). The
        Student t CDF is computed via the regularised incomplete beta
        function (math.lgamma + power-series Lentz continued fraction);
        this is pure stdlib and accurate to ~6 decimals for our regime
        (n in [10, 200], |t| in [0, 10]).

        Edge cases:
          * n_baseline < 2 or n_treatment < 2 → 1.0 (insufficient data).
          * Both variances zero → 0.0 if means differ, else 1.0
            (a deterministic identical sample is a "perfect" signal).
        """
        n1, n2 = len(baseline_scores), len(treatment_scores)
        if n1 < 2 or n2 < 2:
            return 1.0

        m1 = statistics.fmean(baseline_scores)
        m2 = statistics.fmean(treatment_scores)
        v1 = statistics.variance(baseline_scores)
        v2 = statistics.variance(treatment_scores)

        if v1 == 0.0 and v2 == 0.0:
            return 1.0 if m1 == m2 else 0.0

        se = math.sqrt(v1 / n1 + v2 / n2)
        if se == 0.0:
            return 1.0 if m1 == m2 else 0.0

        t = (m2 - m1) / se
        # Welch–Satterthwaite degrees of freedom:
        num = (v1 / n1 + v2 / n2) ** 2
        denom = (
            (v1 / n1) ** 2 / (n1 - 1)
            + (v2 / n2) ** 2 / (n2 - 1)
        )
        if denom == 0.0:
            return 1.0
        df = num / denom

        # Two-tailed p = 2 * P(T > |t|) for T ~ t(df).
        # Using the identity P(T > x) = 0.5 * I_{df/(df+x^2)}(df/2, 1/2)
        # where I is the regularised incomplete beta.
        x_sq = t * t
        if math.isnan(x_sq) or math.isinf(x_sq):
            return 0.0
        z = df / (df + x_sq)
        p = _regularised_incomplete_beta(z, df / 2.0, 0.5)
        # Clamp defensively against floating-point fuzz pushing us
        # outside [0, 1] in degenerate cases.
        if p < 0.0:
            p = 0.0
        elif p > 1.0:
            p = 1.0
        return p

    async def adopt(
        self, result: ExperimentResult, registry: Any | None = None,
    ) -> None:
        """Stage the change. **Iron Rule #2: never call registry.promote
        directly from here.**

        We log a structured event, fire registered staging sinks, and
        return. The actual flip-of-HEAD is the caller's responsibility
        — typically ``EvolutionOrchestrator``, which independently
        re-validates evidence before mutating the registry.

        ``registry`` is accepted for forward-compatibility (callers may
        pass it so a sink can route there) but **this method does not
        invoke any of its mutation methods**.
        """
        if result.decision != "adopt":
            logger.debug(
                "adopt() called for non-adopt result %s (decision=%s); skipping",
                result.experiment_id, result.decision,
            )
            return

        logger.info(
            "experiment %s staged for adoption: delta=%.3f p=%.3f reason=%s",
            result.experiment_id,
            result.delta,
            result.delta_p_value,
            result.decision_reason,
        )

        for sink in self._staging_sinks:
            try:
                outcome = sink(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:  # noqa: BLE001
                logger.exception(
                    "staging sink raised for experiment %s", result.experiment_id,
                )


# ── helpers ────────────────────────────────────────────────────────────


def _scores_for_metric(suite_result: Any, metric: str) -> list[float]:
    """Map ``Experiment.metric`` to the per-task score series we'll
    t-test on. Knows about a small whitelist; unknown metrics fall back
    to per-task ``score`` (the harness's primary number).

    Accepts duck-typed ``suite_result`` (only requires ``.results``
    iterable of objects with ``.score``, ``.passed``, ``.turns``,
    ``.cost_usd``, ``.latency_s``).
    """
    results = getattr(suite_result, "results", None) or ()
    if metric == "task_pass_rate":
        return [1.0 if getattr(r, "passed", False) else 0.0 for r in results]
    if metric == "mean_score":
        return [float(getattr(r, "score", 0.0)) for r in results]
    if metric == "mean_turns":
        return [float(getattr(r, "turns", 0)) for r in results]
    if metric == "cost_usd":
        return [float(getattr(r, "cost_usd", 0.0)) for r in results]
    if metric == "latency_s":
        return [float(getattr(r, "latency_s", 0.0)) for r in results]
    # Default: per-task score.
    return [float(getattr(r, "score", 0.0)) for r in results]


# ── stdlib stats ───────────────────────────────────────────────────────


def _regularised_incomplete_beta(x: float, a: float, b: float) -> float:
    """I_x(a, b) — regularised incomplete beta function.

    Uses the standard Lentz continued-fraction expansion (Numerical
    Recipes §6.4) wrapped by the symmetry I_x(a,b) = 1 - I_{1-x}(b,a)
    to keep the series in its convergence regime. Accurate to ~1e-7
    for the range we hit during a t-test.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Use the continuant only when x < (a+1)/(a+b+2); else flip.
    if x < (a + 1.0) / (a + b + 2.0):
        bt = math.exp(
            math.lgamma(a + b)
            - math.lgamma(a)
            - math.lgamma(b)
            + a * math.log(x)
            + b * math.log(1.0 - x)
        )
        return bt * _beta_continued_fraction(x, a, b) / a
    bt = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    return 1.0 - bt * _beta_continued_fraction(1.0 - x, b, a) / b


def _beta_continued_fraction(x: float, a: float, b: float) -> float:
    """Modified Lentz CF for the incomplete beta — translated from
    Numerical Recipes' ``betacf``."""
    max_iter = 200
    eps = 3.0e-9
    fpmin = 1.0e-30

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # Even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        # Odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    # Fell out of the loop — return what we have. Within our regime,
    # this is unlikely; either way, the caller clamps to [0, 1].
    return h


__all__ = [
    "Experiment",
    "ExperimentDecision",
    "ExperimentResult",
    "ExperimentStore",
    "SelfExperimentLoop",
]
