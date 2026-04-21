"""EvolutionController — unit tests for the promotion decision engine.

The controller is pure: no I/O, no registry access, no scheduler
mutation. These tests pin its four gate conditions and the evidence
it produces when a promotion fires.
"""
from __future__ import annotations

import pytest

from xmclaw.core.evolution import (
    EvolutionController,
    EvolutionDecision,
    PromotionThresholds,
)
from xmclaw.core.evolution.controller import CandidateEvaluation


def _e(
    candidate_id: str, *, version: int, plays: int, mean: float,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate_id=candidate_id, version=version,
        plays=plays, mean_score=mean,
    )


# ── trivial paths ────────────────────────────────────────────────────────


def test_no_candidates_no_change() -> None:
    ctrl = EvolutionController()
    report = ctrl.consider_promotion([], head_version=1)
    assert report.decision == EvolutionDecision.NO_CHANGE


def test_head_is_best_no_change() -> None:
    ctrl = EvolutionController()
    # Tight thresholds so anything else would pass if not for the HEAD check.
    evals = [
        _e("head_arm",  version=1, plays=20, mean=0.92),
        _e("candidate", version=2, plays=20, mean=0.70),
    ]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.92)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "HEAD" in report.reason


# ── gate 1: plays ────────────────────────────────────────────────────────


def test_refuses_when_plays_below_threshold() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.0, min_gap_over_head=0.0,
        min_gap_over_second=0.0,
    ))
    evals = [_e("cand", version=2, plays=5, mean=0.99)]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.2)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "plays" in report.reason


# ── gate 2: mean floor ───────────────────────────────────────────────────


def test_refuses_when_mean_below_floor() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=5, min_mean=0.7, min_gap_over_head=0.0,
        min_gap_over_second=0.0,
    ))
    evals = [_e("cand", version=2, plays=20, mean=0.55)]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.3)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "below floor" in report.reason


# ── gate 3: gap over head ────────────────────────────────────────────────


def test_refuses_when_gap_over_head_too_small() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=5, min_mean=0.0, min_gap_over_head=0.10,
        min_gap_over_second=0.0,
    ))
    evals = [_e("cand", version=2, plays=20, mean=0.72)]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.70)
    # gap = 0.02 < 0.10 → refuse
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "gap" in report.reason.lower()


def test_uses_session_mean_when_head_mean_not_provided() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=5, min_mean=0.0, min_gap_over_head=0.10,
        min_gap_over_second=0.0,
    ))
    evals = [
        _e("a", version=2, plays=20, mean=0.80),
        _e("b", version=2, plays=20, mean=0.20),
    ]
    # session mean = 0.50; best 0.80 - 0.50 = 0.30 > 0.10 → promote
    report = ctrl.consider_promotion(evals, head_version=1)
    assert report.decision == EvolutionDecision.PROMOTE


# ── gate 4: separation from runner-up ────────────────────────────────────


def test_refuses_when_runner_up_is_too_close() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=5, min_mean=0.0, min_gap_over_head=0.0,
        min_gap_over_second=0.10,
    ))
    evals = [
        _e("a", version=2, plays=20, mean=0.82),
        _e("b", version=3, plays=20, mean=0.80),  # within 0.02 of best
    ]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.30)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "runner-up" in report.reason or "separation" in report.reason


# ── happy path ───────────────────────────────────────────────────────────


def test_promotes_best_candidate_when_all_gates_pass() -> None:
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [
        _e("winner", version=2, plays=15, mean=0.85),
        _e("loser",  version=3, plays=10, mean=0.65),
    ]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.50)
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_candidate_id == "winner"
    assert report.winner_version == 2


def test_evidence_contains_all_relevant_numbers() -> None:
    ctrl = EvolutionController()  # use defaults
    evals = [
        _e("winner", version=2, plays=20, mean=0.80),
        _e("second", version=3, plays=15, mean=0.60),
    ]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.40)
    assert report.decision == EvolutionDecision.PROMOTE
    ev = " ".join(report.evidence)
    # Key facts must all appear in evidence — registry.promote log
    # carries this verbatim.
    assert "winner" in ev
    assert "plays=20" in ev
    assert "mean=0.800" in ev
    assert "baseline=0.400" in ev
    assert "gap_over_head=0.400" in ev
    assert "gap_over_second=0.200" in ev


def test_single_candidate_no_runner_up_check_still_promotes() -> None:
    """With only one candidate, the gap-over-second gate can't apply —
    the promotion should still fire if other gates pass."""
    ctrl = EvolutionController()
    evals = [_e("only", version=2, plays=15, mean=0.85)]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.30)
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_candidate_id == "only"


# ── pure-function invariants ─────────────────────────────────────────────


def test_controller_has_no_side_effect_state() -> None:
    """Anti-req #12 defense-in-depth: the controller is a pure decision
    engine. Calling consider_promotion() does NOT mutate anything — the
    same inputs return the same decision on repeat."""
    ctrl = EvolutionController()
    evals = [_e("w", version=2, plays=15, mean=0.85)]
    r1 = ctrl.consider_promotion(evals, head_version=1, head_mean=0.3)
    r2 = ctrl.consider_promotion(evals, head_version=1, head_mean=0.3)
    assert r1 == r2
