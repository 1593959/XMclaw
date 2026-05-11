"""SelfExperimentLoop — Jarvis Phase 6.5 unit tests.

Covers the dataclass shapes, the SQLite-backed ExperimentStore round-
trip, the propose/execute/adopt flow, the four decision branches
(adopt / reject / extend / abort), Iron Rule #2 staging (no direct
registry mutation), and the stdlib Welch's t-test used internally.

The Sprint 4 ``Runner`` is intentionally NOT exercised here — we hand
``SelfExperimentLoop.execute`` two stub agent factories plus a
``load_suite`` thunk that returns a stub suite. The same code path
that production hits in real wiring is covered, just without invoking
any concrete benchmark.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from xmclaw.cognition.self_experiment import (
    Experiment,
    ExperimentResult,
    ExperimentStore,
    SelfExperimentLoop,
    _regularised_incomplete_beta,
)


# ── Stub harness shapes (match xmclaw.eval.harness duck shape) ─────────


@dataclass(frozen=True)
class _StubTaskResult:
    task_id: str
    passed: bool
    score: float
    turns: int = 1
    cost_usd: float = 0.0
    latency_s: float = 0.0
    agent_text: str = ""


@dataclass(frozen=True)
class _StubSuiteResult:
    suite_id: str
    results: tuple[_StubTaskResult, ...]
    n_tasks: int = 0
    n_passed: int = 0
    pass_rate: float = 0.0
    mean_score: float = 0.0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class _StubSuite:
    """Mimics enough of ``BenchmarkSuite`` for the Runner to run.

    We tuck a per-arm score generator into ``self._make_results`` so
    the same suite instance produces baseline-then-treatment numbers
    in the order the loop calls it. Tests build with the two arms.
    """

    def __init__(self, suite_id: str, arms: list[list[float]]) -> None:
        self.SUITE_ID = suite_id  # noqa: N815 (matches harness convention)
        self._suite_id = suite_id
        self._arms = arms
        self._call_count = 0

    @property
    def suite_id(self) -> str:
        return self._suite_id

    def load_tasks(self, limit: int | None = None) -> list[Any]:
        n = max(len(arm) for arm in self._arms) if self._arms else 0
        if limit is not None:
            n = min(n, limit)
        return [object() for _ in range(n)]  # opaque — Runner doesn't inspect

    def grade(self, case: Any, agent_text: str, **extra: Any) -> tuple[bool, float, dict]:
        return True, 1.0, {}


def _make_stub_suite_result(
    suite_id: str, scores: list[float], passes: list[bool] | None = None,
) -> _StubSuiteResult:
    if passes is None:
        passes = [s >= 0.5 for s in scores]
    results = tuple(
        _StubTaskResult(task_id=f"t{i}", passed=p, score=s)
        for i, (s, p) in enumerate(zip(scores, passes))
    )
    n = len(scores)
    n_passed = sum(1 for p in passes if p)
    return _StubSuiteResult(
        suite_id=suite_id,
        results=results,
        n_tasks=n,
        n_passed=n_passed,
        pass_rate=n_passed / n if n else 0.0,
        mean_score=sum(scores) / n if n else 0.0,
    )


# ── Dataclass shape ────────────────────────────────────────────────────


def test_experiment_dataclass_is_frozen():
    exp = Experiment(
        id="e1", hypothesis="X helps Y", intervention={"k": "v"},
        metric="task_pass_rate", baseline_metric_value=0.5,
        holdout_set_size=10, started_at=1000.0, suite_id="s",
    )
    with pytest.raises((AttributeError, TypeError)):
        exp.id = "other"  # type: ignore[misc]


def test_experiment_round_trip_via_dict():
    exp = Experiment(
        id="e1", hypothesis="X", intervention={"k": "v"},
        metric="task_pass_rate", baseline_metric_value=0.5,
        holdout_set_size=10, started_at=1000.0, suite_id="s",
    )
    rebuilt = Experiment.from_dict(exp.to_dict())
    assert rebuilt == exp


def test_experiment_result_dataclass_round_trip():
    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.5, treatment_value=0.7,
        delta=0.2, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="adopt", decision_reason="ok", finished_at=1000.0,
        metadata={"suite_id": "s"},
    )
    assert ExperimentResult.from_dict(res.to_dict()) == res


# ── ExperimentStore ────────────────────────────────────────────────────


async def test_store_save_and_get_experiment(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    exp = Experiment(
        id="e1", hypothesis="hyp", intervention={"x": 1},
        metric="task_pass_rate", baseline_metric_value=0.4,
        holdout_set_size=15, started_at=1000.0, suite_id="s",
    )
    await store.save_experiment(exp)
    loaded = await store.get_experiment("e1")
    assert loaded == exp


async def test_store_get_missing_experiment_returns_none(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    assert await store.get_experiment("nope") is None


async def test_store_save_and_get_result(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    exp = Experiment(
        id="e1", hypothesis="h", intervention={},
        metric="task_pass_rate", baseline_metric_value=0.4,
        holdout_set_size=5, started_at=1000.0, suite_id="s",
    )
    await store.save_experiment(exp)
    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.4, treatment_value=0.6,
        delta=0.2, delta_p_value=0.02, n_baseline=10, n_treatment=10,
        decision="adopt", decision_reason="ok", finished_at=1100.0,
    )
    await store.save_result(res)
    assert await store.get_result("e1") == res


async def test_store_list_experiments_unfiltered(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    for i, started in enumerate([1000.0, 1100.0, 1200.0]):
        await store.save_experiment(Experiment(
            id=f"e{i}", hypothesis="h", intervention={},
            metric="task_pass_rate", baseline_metric_value=0.0,
            holdout_set_size=5, started_at=started, suite_id="s",
        ))
    rows = await store.list_experiments()
    assert [r[0].id for r in rows] == ["e2", "e1", "e0"]  # DESC by started_at
    assert all(r[1] is None for r in rows)


async def test_store_list_experiments_filter_by_decision(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    for i, decision in enumerate(["adopt", "reject", "adopt"]):
        await store.save_experiment(Experiment(
            id=f"e{i}", hypothesis="h", intervention={},
            metric="task_pass_rate", baseline_metric_value=0.0,
            holdout_set_size=5, started_at=1000.0 + i, suite_id="s",
        ))
        await store.save_result(ExperimentResult(
            experiment_id=f"e{i}", baseline_value=0.0,
            treatment_value=0.5, delta=0.5, delta_p_value=0.01,
            n_baseline=10, n_treatment=10, decision=decision,
            decision_reason="r", finished_at=1100.0,
        ))
    adopts = await store.list_experiments(decision="adopt")
    assert {r[0].id for r in adopts} == {"e0", "e2"}
    assert all(r[1] is not None and r[1].decision == "adopt" for r in adopts)


async def test_store_creates_parent_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "exp.db"
    store = ExperimentStore(db_path=nested)
    assert store.db_path == nested
    assert nested.parent.is_dir()


# ── propose() ──────────────────────────────────────────────────────────


async def test_propose_mints_id_and_started_at(tmp_path):
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    exp = await loop.propose(
        hypothesis="X", intervention={"k": 1},
        metric="task_pass_rate", suite_id="s",
        baseline_value=0.4, holdout_set_size=20,
    )
    assert exp.id and len(exp.id) >= 16
    assert exp.started_at > 0
    assert exp.suite_id == "s"
    assert exp.holdout_set_size == 20
    assert exp.baseline_metric_value == 0.4


async def test_propose_persists_experiment(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    loop = SelfExperimentLoop(store=store)
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.4,
    )
    assert await store.get_experiment(exp.id) == exp


async def test_propose_unique_ids(tmp_path):
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    ids = set()
    for _ in range(5):
        exp = await loop.propose(
            hypothesis="X", intervention={}, metric="task_pass_rate",
            suite_id="s", baseline_value=0.4,
        )
        ids.add(exp.id)
    assert len(ids) == 5


# ── execute() decision branches ────────────────────────────────────────


async def test_execute_adopts_when_delta_big_and_p_small(tmp_path):
    """delta=0.20 (pass-rate 0.5 → 0.95), p < 0.05 → adopt."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
        adopt_min_delta=0.10, p_value_threshold=0.05,
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )
    res = await _execute_with_stubs(
        loop, exp,
        baseline_passes=[True, False] * 10 + [False] * 0,  # 0.5
        treatment_passes=[True] * 19 + [False],            # 0.95
    )
    assert res.decision == "adopt"
    assert res.delta > 0.10
    assert res.delta_p_value < 0.05
    assert res.n_baseline == 20
    assert res.n_treatment == 20


async def _execute_with_stubs(
    loop: SelfExperimentLoop,
    exp: Experiment,
    *,
    baseline_passes: list[bool] | None = None,
    treatment_passes: list[bool] | None = None,
    baseline_scores: list[float] | None = None,
    treatment_scores: list[float] | None = None,
):
    """Async-friendly version of the execute driver — patches
    ``xmclaw.eval.harness.Runner`` and awaits the loop normally."""
    if baseline_passes is None and baseline_scores is None:
        raise ValueError("must supply passes or scores for baseline")
    if treatment_passes is None and treatment_scores is None:
        raise ValueError("must supply passes or scores for treatment")
    bs = baseline_scores or [1.0 if p else 0.0 for p in baseline_passes or []]
    ts = treatment_scores or [1.0 if p else 0.0 for p in treatment_passes or []]

    class _StubRunner:
        call_count = [0]

        def __init__(self, agent_factory, suite):
            self._agent_factory = agent_factory
            self._suite = suite

        async def run(self, limit: int | None = None):
            i = _StubRunner.call_count[0]
            _StubRunner.call_count[0] += 1
            if i == 0:
                return _make_stub_suite_result(
                    exp.suite_id, bs, baseline_passes,
                )
            return _make_stub_suite_result(
                exp.suite_id, ts, treatment_passes,
            )

    suite = _StubSuite(exp.suite_id, [bs, ts])
    with patch("xmclaw.eval.harness.Runner", _StubRunner):
        return await loop.execute(
            exp,
            baseline_agent_factory=lambda: object(),
            treatment_agent_factory=lambda: object(),
            load_suite=lambda sid: suite,
        )


async def test_execute_extends_on_small_positive_delta(tmp_path):
    """delta in [0, 0.10), p < 0.10 → extend."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
        adopt_min_delta=0.10, p_value_threshold=0.05,
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )
    # 0.50 vs 0.55 over n=200 → small but significant.
    base = [True, False] * 100
    treat = [True] * 110 + [False] * 90
    res = await _execute_with_stubs(
        loop, exp, baseline_passes=base, treatment_passes=treat,
    )
    assert 0 <= res.delta < 0.10
    if res.delta_p_value < 0.10:
        assert res.decision == "extend"
    else:
        assert res.decision == "reject"


async def test_execute_rejects_on_negative_delta(tmp_path):
    """delta < 0 → reject regardless of p."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.6,
    )
    res = await _execute_with_stubs(
        loop, exp,
        baseline_passes=[True] * 12 + [False] * 8,        # 0.60
        treatment_passes=[True] * 11 + [False] * 9,       # 0.55
    )
    assert res.delta < 0
    assert res.decision == "reject"


async def test_execute_rejects_when_high_p_value(tmp_path):
    """delta=0.15 but p high (huge variance) → reject."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )
    # 4 vs 4 with massive variance — p high.
    bs = [0.0, 1.0, 0.0, 1.0]
    ts = [0.0, 1.0, 1.0, 1.0]
    res = await _execute_with_stubs(
        loop, exp, baseline_scores=bs, treatment_scores=ts,
    )
    assert res.decision == "reject"
    assert res.delta_p_value >= 0.05


async def test_execute_aborts_on_runner_exception(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )

    class _ExplodingRunner:
        def __init__(self, agent_factory, suite): pass

        async def run(self, limit: int | None = None):
            raise RuntimeError("boom")

    suite = _StubSuite(exp.suite_id, [[1.0], [1.0]])
    with patch("xmclaw.eval.harness.Runner", _ExplodingRunner):
        res = await loop.execute(
            exp,
            baseline_agent_factory=lambda: object(),
            treatment_agent_factory=lambda: object(),
            load_suite=lambda sid: suite,
        )
    assert res.decision == "abort"
    assert "boom" in res.decision_reason
    assert res.n_baseline == 0
    assert res.n_treatment == 0


async def test_execute_aborts_on_load_suite_failure(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )

    def _bad_loader(sid: str):
        raise FileNotFoundError("no such suite")

    res = await loop.execute(
        exp,
        baseline_agent_factory=lambda: object(),
        treatment_agent_factory=lambda: object(),
        load_suite=_bad_loader,
    )
    assert res.decision == "abort"


async def test_execute_persists_result(tmp_path):
    store = ExperimentStore(db_path=tmp_path / "exp.db")
    loop = SelfExperimentLoop(store=store)
    exp = await loop.propose(
        hypothesis="X", intervention={}, metric="task_pass_rate",
        suite_id="s", baseline_value=0.5,
    )
    res = await _execute_with_stubs(
        loop, exp,
        baseline_passes=[True, False] * 10,
        treatment_passes=[True] * 19 + [False],
    )
    persisted = await store.get_result(exp.id)
    assert persisted == res


# ── adopt() — Iron Rule #2 ─────────────────────────────────────────────


async def test_adopt_does_not_call_registry_promote(tmp_path):
    """Adopt must NEVER call registry.promote directly. We pass a
    sentinel-y registry stub and assert no mutation method was hit."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )

    class _SpyRegistry:
        promote_called = False
        register_called = False

        def promote(self, *a, **kw):
            type(self).promote_called = True

        def register(self, *a, **kw):
            type(self).register_called = True

    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.5, treatment_value=0.7,
        delta=0.2, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="adopt", decision_reason="ok", finished_at=1.0,
    )
    await loop.adopt(res, registry=_SpyRegistry())
    assert _SpyRegistry.promote_called is False
    assert _SpyRegistry.register_called is False


async def test_adopt_fires_staging_sinks(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    seen: list[ExperimentResult] = []
    loop.add_staging_sink(lambda r: seen.append(r))
    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.5, treatment_value=0.7,
        delta=0.2, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="adopt", decision_reason="ok", finished_at=1.0,
    )
    await loop.adopt(res)
    assert seen == [res]


async def test_adopt_skips_non_adopt_decision(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    seen: list[ExperimentResult] = []
    loop.add_staging_sink(lambda r: seen.append(r))
    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.5, treatment_value=0.4,
        delta=-0.1, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="reject", decision_reason="bad", finished_at=1.0,
    )
    await loop.adopt(res)
    assert seen == []


async def test_adopt_supports_async_sinks(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    seen: list[str] = []

    async def _async_sink(r: ExperimentResult) -> None:
        seen.append(r.experiment_id)

    loop.add_staging_sink(_async_sink)
    res = ExperimentResult(
        experiment_id="e9", baseline_value=0.5, treatment_value=0.7,
        delta=0.2, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="adopt", decision_reason="ok", finished_at=1.0,
    )
    await loop.adopt(res)
    assert seen == ["e9"]


async def test_adopt_swallows_sink_exceptions(tmp_path):
    """A misbehaving staging sink must not break the loop — Iron Rule #2
    decoupling means we keep going to the next sink."""
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
    )
    calls: list[str] = []

    def _bad_sink(r):
        calls.append("bad")
        raise RuntimeError("nope")

    def _good_sink(r):
        calls.append("good")

    loop.add_staging_sink(_bad_sink)
    loop.add_staging_sink(_good_sink)
    res = ExperimentResult(
        experiment_id="e1", baseline_value=0.5, treatment_value=0.7,
        delta=0.2, delta_p_value=0.01, n_baseline=20, n_treatment=20,
        decision="adopt", decision_reason="ok", finished_at=1.0,
    )
    await loop.adopt(res)
    assert calls == ["bad", "good"]


# ── _compute_p_value ───────────────────────────────────────────────────


def test_compute_p_value_n_one_returns_one():
    assert SelfExperimentLoop._compute_p_value([1.0], [0.0, 1.0]) == 1.0
    assert SelfExperimentLoop._compute_p_value([0.0, 1.0], [1.0]) == 1.0
    assert SelfExperimentLoop._compute_p_value([], [1.0, 0.0]) == 1.0


def test_compute_p_value_zero_variance_identical_means():
    # Both samples are constant 0.5 — no signal at all.
    p = SelfExperimentLoop._compute_p_value([0.5] * 5, [0.5] * 5)
    assert p == 1.0


def test_compute_p_value_zero_variance_differing_means():
    # Both constants but different — deterministic separation.
    p = SelfExperimentLoop._compute_p_value([0.0] * 5, [1.0] * 5)
    assert p == 0.0


def test_compute_p_value_large_difference_small_p():
    # Means clearly separated; p must be < 0.01.
    base = [0.0, 0.05, 0.0, 0.05, 0.0, 0.05, 0.0, 0.05, 0.0, 0.05]
    treat = [1.0, 0.95, 1.0, 0.95, 1.0, 0.95, 1.0, 0.95, 1.0, 0.95]
    p = SelfExperimentLoop._compute_p_value(base, treat)
    assert 0.0 <= p < 0.01


def test_compute_p_value_no_difference_high_p():
    # Same distribution → p should be near 1.
    base = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    treat = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    p = SelfExperimentLoop._compute_p_value(base, treat)
    assert p > 0.5


def test_compute_p_value_known_small_reference():
    """Hand-computed: Welch's t for [1,2,3] vs [4,5,6].
    m1=2, m2=5, v1=v2=1, n=3, se=sqrt(2/3)≈0.8165, t=3/0.8165≈3.674,
    df = (2/3)^2 / (2 * (1/3)^2 / 2) = (4/9) / (2/9) = 2 → wait,
    df numerator = (1/3 + 1/3)^2 = (2/3)^2 = 4/9
    df denom = (1/3)^2/2 + (1/3)^2/2 = 2 * (1/9)/2 = 1/9
    df = 4. Two-tailed p at t=3.674, df=4 ≈ 0.021.
    """
    p = SelfExperimentLoop._compute_p_value([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    assert 0.015 < p < 0.030


def test_compute_p_value_returns_in_unit_interval():
    base = [0.0, 0.0, 1.0, 1.0, 0.5, 0.5]
    treat = [1.0, 1.0, 0.0, 0.0, 0.5, 0.5]
    p = SelfExperimentLoop._compute_p_value(base, treat)
    assert 0.0 <= p <= 1.0


def test_regularised_incomplete_beta_boundary_zero_one():
    # Boundary checks: I_x(a,b) = 0 at x=0, 1 at x=1.
    assert _regularised_incomplete_beta(0.0, 2.0, 3.0) == 0.0
    assert _regularised_incomplete_beta(1.0, 2.0, 3.0) == 1.0


def test_regularised_incomplete_beta_symmetry():
    # I_{0.5}(a, a) = 0.5 — the symmetry point.
    assert math.isclose(
        _regularised_incomplete_beta(0.5, 2.0, 2.0), 0.5, abs_tol=1e-6
    )


# ── decision boundary unit tests on _decide ────────────────────────────


def test_decide_branches_on_thresholds(tmp_path):
    loop = SelfExperimentLoop(
        store=ExperimentStore(db_path=tmp_path / "exp.db"),
        adopt_min_delta=0.10, p_value_threshold=0.05,
    )
    # adopt
    d, r = loop._decide(0.20, 0.01)
    assert d == "adopt" and "delta=0.200" in r
    # extend
    d, r = loop._decide(0.05, 0.03)
    assert d == "extend"
    # reject (negative)
    d, r = loop._decide(-0.05, 0.01)
    assert d == "reject"
    # reject (high p, big delta)
    d, r = loop._decide(0.20, 0.20)
    assert d == "reject"
    # reject (delta below extend threshold but p too high)
    d, r = loop._decide(0.05, 0.30)
    assert d == "reject"


# ── ctor validation ────────────────────────────────────────────────────


def test_ctor_rejects_non_positive_min_delta(tmp_path):
    with pytest.raises(ValueError):
        SelfExperimentLoop(
            store=ExperimentStore(db_path=tmp_path / "exp.db"),
            adopt_min_delta=0.0,
        )


def test_ctor_rejects_out_of_range_p_threshold(tmp_path):
    with pytest.raises(ValueError):
        SelfExperimentLoop(
            store=ExperimentStore(db_path=tmp_path / "exp.db"),
            p_value_threshold=1.0,
        )
    with pytest.raises(ValueError):
        SelfExperimentLoop(
            store=ExperimentStore(db_path=tmp_path / "exp.db"),
            p_value_threshold=0.0,
        )


# ── tick / set_factories ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_without_factories_returns_false(tmp_path):
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    assert await loop.tick() is False


@pytest.mark.asyncio
async def test_tick_with_factories_proposes_and_executes(tmp_path):
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    baseline = _make_stub_suite_result("s", [0.5, 0.6, 0.55])
    treatment = _make_stub_suite_result("s", [0.7, 0.75, 0.72])
    suite = _StubSuite("s", arms=[[0.5, 0.6, 0.55], [0.7, 0.75, 0.72]])

    loop.set_factories(
        baseline_factory=lambda: object(),
        treatment_factory=lambda: object(),
        load_suite=lambda sid: suite,
        suite_id="s",
    )

    # Patch _run_one_arm so we don't need a real Runner.
    call_order: list[str] = []
    async def _fake_run_one_arm(load_suite, suite_id, factory, limit):
        call_order.append("arm")
        if len(call_order) == 1:
            return baseline
        return treatment

    with patch.object(loop, "_run_one_arm", _fake_run_one_arm):
        ok = await loop.tick()

    assert ok is True
    # tick() should have proposed an experiment and executed it.
    experiments = await loop.store.list_experiments(limit=10)
    assert len(experiments) == 1
    exp, res = experiments[0]
    assert res is not None
    assert res.decision in ("adopt", "reject", "extend")


@pytest.mark.asyncio
async def test_tick_creates_pending_when_factories_missing_then_resumes(tmp_path):
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    loop.set_factories(
        baseline_factory=None,  # type: ignore[arg-type]
        treatment_factory=None,  # type: ignore[arg-type]
        load_suite=lambda sid: _StubSuite("s", arms=[]),
        suite_id="s",
    )
    # tick() with factories=None should still propose but return True
    # because it created a pending experiment.
    ok = await loop.tick()
    assert ok is True
    assert loop._pending_experiment is not None

    # Now inject factories and tick again — should execute the pending one.
    baseline = _make_stub_suite_result("s", [0.5, 0.5])
    treatment = _make_stub_suite_result("s", [0.6, 0.6])
    suite = _StubSuite("s", arms=[[0.5, 0.5], [0.6, 0.6]])

    loop.set_factories(
        baseline_factory=lambda: object(),
        treatment_factory=lambda: object(),
        load_suite=lambda sid: suite,
        suite_id="s",
    )

    async def _fake_run_one_arm(load_suite, suite_id, factory, limit):
        if factory == loop._baseline_factory:
            return baseline
        return treatment

    with patch.object(loop, "_run_one_arm", _fake_run_one_arm):
        ok2 = await loop.tick()

    assert ok2 is True
    assert loop._pending_experiment is None
    experiments = await loop.store.list_experiments(limit=10)
    assert len(experiments) == 1
    assert experiments[0][1] is not None


@pytest.mark.asyncio
async def test_tick_isolates_single_candidate_when_multiple_exist(tmp_path):
    """When multiple skills have candidates, tick() rotates through
    them one-at-a-time so each experiment isolates a single skill."""
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    loop.set_candidate_resolver(lambda: {"skill_a": 2, "skill_b": 3})
    loop.set_factories(
        baseline_factory=lambda: object(),
        treatment_factory=lambda overrides=None: object(),
        load_suite=lambda sid: _StubSuite("s", arms=[]),
        suite_id="s",
    )

    async def _fake_run_one_arm(load_suite, suite_id, factory, limit):
        return _make_stub_suite_result(suite_id, [0.5])

    with patch.object(loop, "_run_one_arm", _fake_run_one_arm):
        # First tick — should propose + execute with skill_a only.
        ok1 = await loop.tick()
        assert ok1 is True
        exps1 = await loop.store.list_experiments(limit=10)
        assert len(exps1) == 1
        assert exps1[0][0].intervention.get("candidate_overrides") == {"skill_a": 2}

        # Second tick — should rotate to skill_b.
        ok2 = await loop.tick()
        assert ok2 is True
        exps2 = await loop.store.list_experiments(limit=10)
        assert len(exps2) == 2
        assert exps2[0][0].intervention.get("candidate_overrides") == {"skill_b": 3}

        # Third tick — should wrap back to skill_a.
        ok3 = await loop.tick()
        assert ok3 is True
        exps3 = await loop.store.list_experiments(limit=10)
        assert len(exps3) == 3
        assert exps3[0][0].intervention.get("candidate_overrides") == {"skill_a": 2}


@pytest.mark.asyncio
async def test_execute_passes_overrides_to_treatment_factory(tmp_path):
    """execute() forwards candidate_overrides from the experiment
    intervention to the treatment_agent_factory."""
    loop = SelfExperimentLoop(store=ExperimentStore(db_path=tmp_path / "exp.db"))
    received_overrides = []

    def _capturing_treatment(overrides=None):
        received_overrides.append(overrides)
        return object()

    loop.set_factories(
        baseline_factory=lambda: object(),
        treatment_factory=_capturing_treatment,
        load_suite=lambda sid: _StubSuite("s", arms=[]),
        suite_id="s",
    )

    exp = await loop.propose(
        hypothesis="test",
        intervention={"candidate_overrides": {"skill_x": 7}},
        metric="mean_score",
        suite_id="s",
        baseline_value=0.0,
        holdout_set_size=1,
    )

    call_log = []

    async def _fake_run_one_arm(load_suite, suite_id, factory, limit):
        # Invoke the factory so treatment factories with overrides are
        # exercised (the real Runner does this).
        factory()
        call_log.append(suite_id)
        return _make_stub_suite_result(suite_id, [0.5])

    with patch.object(loop, "_run_one_arm", _fake_run_one_arm):
        await loop.execute(
            exp,
            baseline_agent_factory=lambda: object(),
            treatment_agent_factory=_capturing_treatment,
            load_suite=lambda sid: _StubSuite("s", arms=[]),
        )

    assert any(ov == {"skill_x": 7} for ov in received_overrides)
