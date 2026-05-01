"""B-117/118/119 — pin the three logic improvements found in the
skill-system audit:

  * B-117: PromotionThresholds reads from config (workspace ctor)
  * B-118: _ArmAggregate has EWMA + sliding-window-style scoring
  * B-119: EvolutionController returns ROLLBACK when HEAD regresses

These are decision-engine pins — they assert the math, not the
end-to-end pipeline plumbing. Pipeline integration is covered by the
existing tests/integration/test_v2_autonomous_evolution.py.
"""
from __future__ import annotations

import pytest

from xmclaw.core.evolution.controller import (
    CandidateEvaluation,
    EvolutionController,
    EvolutionDecision,
    PromotionThresholds,
)
from xmclaw.daemon.evolution_agent import _ArmAggregate


# ── B-118: ArmAggregate EWMA ───────────────────────────────────────────


def test_arm_update_advances_both_mean_and_ewma() -> None:
    arm = _ArmAggregate(skill_id="x", version=1)
    for r in [0.5, 0.5, 0.5, 0.5]:
        arm.update(r)
    assert arm.plays == 4
    assert arm.mean == pytest.approx(0.5)
    assert arm.ewma_mean == pytest.approx(0.5)


def test_arm_ewma_drops_when_recent_scores_drop() -> None:
    """Lifetime mean stays high but ewma sees the regression."""
    arm = _ArmAggregate(skill_id="x", version=1, ewma_alpha=0.3)
    # 20 plays at 1.0 then 5 plays at 0.0 — lifetime mean ≈ 0.8;
    # ewma should drop substantially because alpha=0.3 emphasises
    # the last few.
    for _ in range(20):
        arm.update(1.0)
    for _ in range(5):
        arm.update(0.0)
    assert arm.mean == pytest.approx(20 / 25)
    # EWMA should be visibly below 0.5 by now.
    assert arm.ewma_mean < 0.5


def test_arm_first_play_seeds_ewma() -> None:
    arm = _ArmAggregate(skill_id="x", version=1, ewma_alpha=0.1)
    arm.update(0.7)
    assert arm.ewma_mean == pytest.approx(0.7)


# ── B-119: ROLLBACK decision ───────────────────────────────────────────


def test_controller_returns_rollback_when_head_regressed() -> None:
    """HEAD v2 has dropped; v1 has stronger recent mean and enough plays."""
    t = PromotionThresholds(
        min_plays=10, min_mean=0.5,
        min_gap_over_head=0.05, min_gap_over_second=0.03,
    )
    c = EvolutionController(t)
    evaluations = [
        # v1 — old HEAD, still scoring well.
        CandidateEvaluation(candidate_id="x", version=1, plays=20, mean_score=0.78),
        # v2 — current HEAD, regressed.
        CandidateEvaluation(candidate_id="x", version=2, plays=15, mean_score=0.55),
    ]
    report = c.consider_promotion(
        evaluations, head_version=2, head_mean=0.55,
    )
    assert report.decision == EvolutionDecision.ROLLBACK
    assert report.winner_version == 1
    # Evidence must mention both arm states for audit clarity.
    assert any("target_mean=" in e for e in report.evidence)
    assert any("head_mean=" in e for e in report.evidence)


def test_controller_no_rollback_when_earlier_has_too_few_plays() -> None:
    """v1 looks better than HEAD but only has 3 plays — small-sample
    fluke, refuse to rollback."""
    t = PromotionThresholds(min_plays=10)
    c = EvolutionController(t)
    evaluations = [
        CandidateEvaluation(candidate_id="x", version=1, plays=3, mean_score=0.95),
        CandidateEvaluation(candidate_id="x", version=2, plays=20, mean_score=0.55),
    ]
    report = c.consider_promotion(
        evaluations, head_version=2, head_mean=0.55,
    )
    assert report.decision != EvolutionDecision.ROLLBACK


def test_controller_no_rollback_when_gap_too_small() -> None:
    """Earlier version edges out HEAD by 0.02 — below the 0.05 gate.
    Stay put."""
    t = PromotionThresholds(min_gap_over_head=0.05)
    c = EvolutionController(t)
    evaluations = [
        CandidateEvaluation(candidate_id="x", version=1, plays=20, mean_score=0.62),
        CandidateEvaluation(candidate_id="x", version=2, plays=20, mean_score=0.60),
    ]
    report = c.consider_promotion(
        evaluations, head_version=2, head_mean=0.60,
    )
    assert report.decision != EvolutionDecision.ROLLBACK


def test_promote_still_works_when_no_regression() -> None:
    """B-119 must not break the promote path. v3 dominates, head_mean
    is fine; expect PROMOTE not ROLLBACK."""
    t = PromotionThresholds(min_plays=10)
    c = EvolutionController(t)
    evaluations = [
        CandidateEvaluation(candidate_id="x", version=1, plays=15, mean_score=0.55),
        CandidateEvaluation(candidate_id="x", version=2, plays=15, mean_score=0.62),  # HEAD
        CandidateEvaluation(candidate_id="x", version=3, plays=15, mean_score=0.85),
    ]
    report = c.consider_promotion(
        evaluations, head_version=2, head_mean=0.62,
    )
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_version == 3
