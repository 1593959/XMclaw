"""Unit tests for Phase 0 — mutation engine + dataset + constraints."""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.evolution import (
    ConstraintReport,
    EvalDataset,
    EvalExample,
    MutationResult,
    SkillMutator,
    validate_candidate,
    xmclaw_fitness,
)


# ── Dataset / split ───────────────────────────────────────────────────


def _ex(score: float = 0.7, **k) -> EvalExample:
    return EvalExample(
        task_input=k.get("inp", "in"),
        expected_behavior=k.get("out", "out"),
        baseline_score=score,
    )


def test_split_50_25_25_proportions_for_realistic_size():
    items = [_ex(0.5) for _ in range(20)]
    ds = EvalDataset.split(items)
    assert len(ds.train) == 10
    assert len(ds.val) == 5
    assert len(ds.holdout) == 5


def test_split_handles_tiny_dataset_without_crashing():
    items = [_ex(0.5) for _ in range(3)]
    ds = EvalDataset.split(items)
    assert ds.train  # at least 1 train
    # With 3 items and 50/25/25, train=1, val=1 (max(1, 0)), holdout=1
    assert len(ds.train) + len(ds.val) + len(ds.holdout) == 3


def test_split_empty_returns_empty_dataset():
    ds = EvalDataset.split([])
    assert ds.is_empty
    assert not ds.has_holdout


def test_split_is_deterministic_with_seed():
    items = [_ex(i / 10.0) for i in range(20)]
    a = EvalDataset.split(items, seed=42)
    b = EvalDataset.split(items, seed=42)
    assert [e.baseline_score for e in a.train] == [
        e.baseline_score for e in b.train
    ]


# ── Constraints ───────────────────────────────────────────────────────


def test_constraints_pass_minor_edit():
    baseline = "# Skill\n\nThis is a small skill body that does X.\n\nUse it for X." * 3
    candidate = baseline + "\n\nAlso, prefer Y when Z."
    r = validate_candidate(baseline, candidate)
    assert r.ok, r.failures


def test_constraints_reject_too_small():
    baseline = "x" * 1000
    r = validate_candidate(baseline, "tiny")
    assert not r.ok
    assert any("size_below_min" in f for f in r.failures)


def test_constraints_reject_excess_growth():
    baseline = "x" * 1000
    candidate = "x" * 5000  # 5× = exceeds default 3× cap
    r = validate_candidate(baseline, candidate)
    assert not r.ok
    assert any("growth_excess" in f for f in r.failures)


def test_constraints_reject_total_rewrite():
    # Both pass size checks but the candidate shares no n-grams with
    # baseline — a degenerate "rewrite".
    baseline = (
        "Build an answer using the user's exact preferences. "
        "Always prefer concise prose. Cite sources when claims are factual." * 3
    )
    candidate = (
        "Yodel softly while inverting the bowl. " * 50
    )
    r = validate_candidate(baseline, candidate, min_retain_ratio=0.3)
    assert not r.ok
    assert any("retain_below_min" in f for f in r.failures)


def test_constraints_required_section_check():
    baseline = "# Skill\n\nBody." * 30
    candidate = "# Skill\n\nNew body." * 30
    r = validate_candidate(
        baseline, candidate, required_sections=[r"^## Examples"], min_retain_ratio=0.0,
    )
    assert not r.ok
    assert any("missing_section" in f for f in r.failures)


# ── Fitness function ──────────────────────────────────────────────────


def test_fitness_zero_for_empty_prediction():
    ex = _ex(out="The expected text answer.")
    assert xmclaw_fitness(ex, "") == 0.0


def test_fitness_full_for_close_prediction():
    ex = _ex(out="The expected outcome should mention apples and oranges.")
    score = xmclaw_fitness(ex, "Apples and oranges are the expected outcome.")
    # Hard checks: ran=1, returned=1, type_matched ≈ high. Soft: length ratio.
    assert score > 0.7


def test_fitness_caps_at_one():
    ex = _ex(out="the cat sat on the mat")
    score = xmclaw_fitness(ex, "the cat sat on the mat")
    assert 0.0 <= score <= 1.0


# ── SkillMutator ──────────────────────────────────────────────────────


def test_mutator_reports_dspy_unavailable_gracefully():
    m = SkillMutator()
    if m.is_available:
        pytest.skip("DSPy installed; cannot test the unavailable path")
    ds = EvalDataset.split([_ex() for _ in range(8)])
    res = asyncio.run(m.mutate(skill_id="demo", baseline_text="x" * 500, dataset=ds))
    assert isinstance(res, MutationResult)
    assert res.ok is False
    assert res.reason == "dspy_not_installed"


def test_mutator_handles_empty_dataset():
    m = SkillMutator()
    res = asyncio.run(
        m.mutate(skill_id="demo", baseline_text="x" * 500, dataset=EvalDataset.split([]))
    )
    # When DSPy is missing the early-return is `dspy_not_installed`; when
    # DSPy is present but dataset is empty, we return `empty_dataset`.
    assert res.ok is False
    assert res.reason in {"dspy_not_installed", "empty_dataset"}
