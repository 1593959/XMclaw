"""Sprint 3 #4 controller integration — Iron Rule #2 wired into
``EvolutionController.consider_promotion``.

Iron Rule #2 (`docs/EVOLUTION_HONEST_STATE.md`):

    "Staging → gate → explicit promote. The orchestrator never mutates
    SkillRegistry HEAD inline. Always: candidate dir → 4 gates →
    explicit promote() call (auto-policy or human)."

The 4-gate mechanics ship in `xmclaw/core/evolution/staging.py` (Sprint
3 #4 base commit). This file pins the integration point: the
controller MUST refuse promotion when the caller supplies a GateBundle
that didn't pass all gates.

Two-layer compatibility:

* When ``iron_rule_2`` is None (legacy callers / pre-staging tests),
  behaviour is unchanged — the controller never consults the staging
  bundle.
* When ``iron_rule_2`` is supplied AND ``passed_all`` is False, the
  controller returns NO_CHANGE with ``blocked_by_iron_rule_2`` populated.
* When ``iron_rule_2`` is supplied AND ``passed_all`` is True, the
  controller runs through to PROMOTE as if the bundle wasn't there.
"""
from __future__ import annotations

from xmclaw.core.evolution.controller import (
    CandidateEvaluation,
    EvolutionController,
    EvolutionDecision,
    PromotionThresholds,
)
from xmclaw.core.evolution.staging import (
    Candidate,
    GateBundle,
    GateResult,
    run_gates,
)


def _passing_evals() -> list[CandidateEvaluation]:
    """Two arms: HEAD v1 (low score) + candidate v2 (high score). The
    candidate clears all four legacy gates AND has plenty of plays."""
    return [
        CandidateEvaluation(
            candidate_id="head",
            version=1,
            plays=20,
            mean_score=0.40,
        ),
        CandidateEvaluation(
            candidate_id="cand_v2",
            version=2,
            plays=20,
            mean_score=0.80,
        ),
    ]


def _bundle(*, all_pass: bool, fail_gate: str = "structure") -> GateBundle:
    """Build a GateBundle. ``all_pass=True`` returns 4 passed results;
    False returns 3 passed + 1 failed at ``fail_gate``."""
    if all_pass:
        results = [
            GateResult(name=n, status="passed", evidence={}, reason=None)
            for n in ("size_limit", "growth_limit", "structure_validation",
                     "holdout_test")
        ]
    else:
        results = []
        for n in ("size_limit", "growth_limit", "structure_validation",
                  "holdout_test"):
            if n == fail_gate:
                results.append(GateResult(
                    name=n, status="failed", evidence={"reason": "synthetic"},
                    reason="synthetic test failure",
                ))
            else:
                results.append(GateResult(
                    name=n, status="passed", evidence={}, reason=None,
                ))
    return GateBundle(results=tuple(results))


# ── tests ─────────────────────────────────────────────────────────


def test_b_iron_rule_2_none_is_legacy_path() -> None:
    """When the caller doesn't pass a bundle, behaviour is unchanged."""
    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_1=None,
        iron_rule_2=None,
    )
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_version == 2
    assert report.blocked_by_iron_rule_2 is None


def test_b_iron_rule_2_all_pass_promotes() -> None:
    """All 4 staging gates pass → controller promotes (gates didn't
    block)."""
    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    bundle = _bundle(all_pass=True)
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_version == 2
    assert report.blocked_by_iron_rule_2 is None


def test_b_iron_rule_2_size_failure_blocks_promotion() -> None:
    """size_limit gate failed → controller refuses promotion even
    with high mean + plenty of plays."""
    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    bundle = _bundle(all_pass=False, fail_gate="size_limit")
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert report.blocked_by_iron_rule_2 is bundle
    assert "Iron Rule #2" in report.reason
    assert "size_limit" in report.reason


def test_b_iron_rule_2_structure_failure_blocks_promotion() -> None:
    """Structure validation failure → controller refuses."""
    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    bundle = _bundle(all_pass=False, fail_gate="structure_validation")
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert report.blocked_by_iron_rule_2 is bundle
    assert "structure_validation" in report.reason


def test_b_iron_rule_2_holdout_failure_blocks_promotion() -> None:
    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    bundle = _bundle(all_pass=False, fail_gate="holdout_test")
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "holdout_test" in report.reason


def test_b_iron_rule_2_only_consulted_after_legacy_gates() -> None:
    """When the legacy 4 gates would already have blocked (e.g.
    insufficient plays), Iron Rule #2 isn't reached — the report's
    ``blocked_by_iron_rule_2`` field stays None."""
    ctrl = EvolutionController(PromotionThresholds(min_plays=100))
    # Plays=20 < min_plays=100 → blocked at gate 1.
    bundle = _bundle(all_pass=False, fail_gate="size_limit")
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert report.blocked_by_iron_rule_2 is None  # legacy gate blocked first
    assert "plays" in report.reason


def test_b_iron_rule_2_real_run_gates_integration() -> None:
    """Wire through a REAL ``run_gates`` call (not a hand-built bundle)
    using a candidate whose source bloated 10x — gate_growth_limit must
    fail, controller must refuse promotion."""
    head_source = "def run(): return 1\n"
    huge_source = "# bloat\n" * 200 + "def run(): return 1\n"
    candidate = Candidate(
        id="cand-bloat",
        skill_id="my_skill",
        version=2,
        source_text=huge_source,
        metadata={},
        created_at=0.0,
    )
    bundle = run_gates(candidate, head_source=head_source)
    assert not bundle.passed_all  # growth limit caught it

    ctrl = EvolutionController(PromotionThresholds(min_plays=1))
    report = ctrl.consider_promotion(
        _passing_evals(),
        head_version=1,
        head_mean=0.40,
        iron_rule_2=bundle,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert report.blocked_by_iron_rule_2 is bundle
    assert "growth_limit" in report.reason
