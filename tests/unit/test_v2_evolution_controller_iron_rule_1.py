"""EvolutionController × Iron Rule #1 gate tests.

Sprint 3 Iron Rule #1: any promotion needs ≥2 INDEPENDENT signals;
never single LLM-judge. The controller now consumes a
:class:`IronRule1Gate` summary alongside the legacy plays / mean /
gap thresholds. When ``promote_eligible=False``, the controller
refuses promotion EVEN IF every legacy gate clears, returning
NO_CHANGE with a structured ``blocked_by_iron_rule_1`` field that
the orchestrator translates into a ``SKILL_PROMOTION_BLOCKED`` bus
event for ``xmclaw evolve review``.

These tests pin the gate's invariants:
  * promote_eligible=False blocks even when EWMA + plays + gaps all
    clear.
  * promote_eligible=True with all gates clear → PROMOTE as before.
  * Legacy gate failures (plays, mean, gap) still take precedence over
    Iron Rule #1 — the existing reasons are kept intact.
  * Backward compat: callers that don't pass ``iron_rule_1`` get the
    pre-Sprint-3 behaviour exactly (no behaviour change for legacy
    users yet).
  * SKILL_PROMOTION_BLOCKED event type is registered on the bus enum
    so subscribers can listen for it.
  * The block_reason string is one of the stable strings the
    ``xmclaw evolve review`` UI groups on.
  * registry.promote() still requires ``evidence=`` (anti-req #12 —
    structurally enforced at the door, unchanged).
"""
from __future__ import annotations

import pytest

from xmclaw.core.bus.events import EventType
from xmclaw.core.evolution import (
    EvolutionController,
    EvolutionDecision,
    IronRule1Gate,
    PromotionThresholds,
)
from xmclaw.core.evolution.controller import (
    CandidateEvaluation,
    EvolutionReport,
)
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


class _NoopSkill(Skill):
    def __init__(self, skill_id: str, version: int) -> None:
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"v": self.version}, side_effects=[])


def _register_demo(reg: SkillRegistry, skill_id: str, version: int) -> None:
    reg.register(
        _NoopSkill(skill_id, version),
        SkillManifest(id=skill_id, version=version, created_by="test"),
    )


def _eval(
    candidate_id: str, *, version: int, plays: int, mean: float,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate_id=candidate_id, version=version,
        plays=plays, mean_score=mean,
    )


# ── Iron Rule #1 blocking ────────────────────────────────────────────────


def test_promote_eligible_false_blocks_promotion_despite_legacy_gates() -> None:
    """The fifth gate. plays + mean + gap_over_head + gap_over_second
    all clear, but iron_rule_1.promote_eligible=False → NO_CHANGE."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [
        _eval("winner", version=2, plays=20, mean=0.85),
        _eval("loser", version=3, plays=15, mean=0.65),
    ]
    blocked = IronRule1Gate(
        promote_eligible=False,
        deterministic_score=0.85,
        independent_score=None,
        block_reason="single_signal_only",
    )
    report = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.50,
        iron_rule_1=blocked,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert report.blocked_by_iron_rule_1 is not None
    assert report.blocked_by_iron_rule_1.block_reason == "single_signal_only"
    assert "Iron Rule #1" in report.reason


def test_promote_eligible_true_lets_promotion_through() -> None:
    """When the multi-signal gate is True AND legacy gates clear → PROMOTE."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [
        _eval("winner", version=2, plays=20, mean=0.85),
        _eval("loser", version=3, plays=15, mean=0.65),
    ]
    eligible = IronRule1Gate(
        promote_eligible=True,
        deterministic_score=0.90,
        independent_score=0.70,
        block_reason="",
    )
    report = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.50,
        iron_rule_1=eligible,
    )
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_candidate_id == "winner"
    assert report.blocked_by_iron_rule_1 is None


def test_legacy_callers_without_iron_rule_1_arg_unaffected() -> None:
    """Pre-Sprint-3 callers that don't pass iron_rule_1 still see the
    legacy promotion path exactly. This is a backward-compat invariant
    so the rollout doesn't break the bench harness or the existing
    EvolutionAgent path mid-flight."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [_eval("winner", version=2, plays=20, mean=0.85)]
    report = ctrl.consider_promotion(evals, head_version=1, head_mean=0.50)
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.blocked_by_iron_rule_1 is None


def test_legacy_gate_failure_takes_precedence_over_iron_rule_1() -> None:
    """If plays / mean / gap fail, those reasons stay — Iron Rule #1
    isn't the proximate cause and shouldn't shadow the real signal.
    The blocked_by_iron_rule_1 field stays None in that case."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=20, min_mean=0.0, min_gap_over_head=0.0,
        min_gap_over_second=0.0,
    ))
    evals = [_eval("winner", version=2, plays=5, mean=0.99)]
    eligible = IronRule1Gate(
        promote_eligible=True,
        deterministic_score=0.99, independent_score=0.99,
    )
    report = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30,
        iron_rule_1=eligible,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "plays" in report.reason
    assert report.blocked_by_iron_rule_1 is None


def test_block_reason_propagates_into_report() -> None:
    """The structured block_reason is what ``xmclaw evolve review``
    groups blocked promotions by — must round-trip into the report."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [_eval("winner", version=2, plays=20, mean=0.85)]
    cases = [
        "single_signal_only",
        "deterministic_floor",
        "independent_floor",
    ]
    for reason in cases:
        gate = IronRule1Gate(
            promote_eligible=False,
            deterministic_score=0.5,
            independent_score=0.3,
            block_reason=reason,
        )
        report = ctrl.consider_promotion(
            evals, head_version=1, head_mean=0.30,
            iron_rule_1=gate,
        )
        assert report.decision == EvolutionDecision.NO_CHANGE
        assert report.blocked_by_iron_rule_1 is not None
        assert report.blocked_by_iron_rule_1.block_reason == reason


def test_default_block_reason_when_unset() -> None:
    """If a caller forgets to set ``block_reason`` but says
    ``promote_eligible=False``, the controller defaults to
    ``"single_signal_only"`` — never silently 'unknown'."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [_eval("winner", version=2, plays=20, mean=0.85)]
    gate = IronRule1Gate(promote_eligible=False)
    report = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30,
        iron_rule_1=gate,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert "single_signal_only" in report.reason


def test_skill_promotion_blocked_event_type_registered() -> None:
    """The bus event type the orchestrator publishes on Iron Rule #1
    blocks must exist on the enum. Subscribers (``xmclaw evolve
    review``, audit log) listen by enum membership."""
    assert hasattr(EventType, "SKILL_PROMOTION_BLOCKED")
    assert EventType.SKILL_PROMOTION_BLOCKED.value == "skill_promotion_blocked"


# ── anti-req #12 unchanged: registry.promote still demands evidence ──


def test_registry_promote_still_requires_evidence_anti_req_12() -> None:
    """Anti-req #12: ``registry.promote(evidence=[])`` must fail.
    Sprint 3's new gate is ON TOP of this — the registry door check
    is unchanged. Kept here to pin that the new control surface didn't
    accidentally weaken the structural enforcement."""
    reg = SkillRegistry()
    _register_demo(reg, "t.demo", 1)
    _register_demo(reg, "t.demo", 2)
    with pytest.raises(ValueError) as exc:
        reg.promote(skill_id="t.demo", to_version=2, evidence=[])
    assert "anti-req #12" in str(exc.value)


def test_registry_promote_with_evidence_succeeds() -> None:
    """Sanity: the structural enforcement isn't paranoid — a real call
    with evidence still works."""
    reg = SkillRegistry()
    _register_demo(reg, "t.demo", 1)
    _register_demo(reg, "t.demo", 2)
    record = reg.promote(
        skill_id="t.demo", to_version=2,
        evidence=["multi-signal grader: det=0.85 ind=0.70 promote_eligible=True"],
    )
    assert record.to_version == 2


# ── EvolutionReport carries enough to emit SKILL_PROMOTION_BLOCKED ──


def test_blocked_report_carries_score_breakdown_for_event_payload() -> None:
    """The orchestrator builds a SKILL_PROMOTION_BLOCKED payload from
    the report. It needs both signal scores so the UI can show 'det
    cleared but independent missing' or vice versa."""
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=10, min_mean=0.6, min_gap_over_head=0.05,
        min_gap_over_second=0.03,
    ))
    evals = [_eval("winner", version=2, plays=20, mean=0.85)]
    gate = IronRule1Gate(
        promote_eligible=False,
        deterministic_score=0.85,
        independent_score=None,
        block_reason="single_signal_only",
    )
    report: EvolutionReport = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30,
        iron_rule_1=gate,
    )
    assert report.blocked_by_iron_rule_1 is not None
    assert report.blocked_by_iron_rule_1.deterministic_score == 0.85
    assert report.blocked_by_iron_rule_1.independent_score is None
    # Evidence list still populated — same audit trail as the legacy
    # path so the UI doesn't have to special-case blocked reports.
    assert any("plays=20" in e for e in report.evidence)
    assert any("mean=0.850" in e for e in report.evidence)


def test_blocked_report_evidence_includes_legacy_threshold_summary() -> None:
    """Even though promotion was blocked, the legacy threshold evidence
    is preserved so the audit log reads like ' would have promoted but
    for Iron Rule #1'. Easier for reviewers to investigate."""
    ctrl = EvolutionController()  # default thresholds
    evals = [_eval("w", version=2, plays=20, mean=0.85)]
    gate = IronRule1Gate(
        promote_eligible=False, block_reason="independent_floor",
        deterministic_score=0.85, independent_score=0.30,
    )
    report = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30,
        iron_rule_1=gate,
    )
    assert report.decision == EvolutionDecision.NO_CHANGE
    ev_joined = " ".join(report.evidence)
    assert "candidate=w" in ev_joined
    assert "plays=20" in ev_joined
    assert "gap_over_head=" in ev_joined


# ── purity invariant ────────────────────────────────────────────────────


def test_iron_rule_1_input_is_pure_no_state_mutation() -> None:
    """Anti-req #12 defense-in-depth — same call twice = same answer."""
    ctrl = EvolutionController()
    evals = [_eval("w", version=2, plays=20, mean=0.85)]
    gate = IronRule1Gate(
        promote_eligible=False, block_reason="single_signal_only",
    )
    r1 = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30, iron_rule_1=gate,
    )
    r2 = ctrl.consider_promotion(
        evals, head_version=1, head_mean=0.30, iron_rule_1=gate,
    )
    assert r1 == r2
