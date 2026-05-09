"""Sprint 3 Iron Rule #2 — staging gates + promotion policy unit tests.

These tests pin the contract of the new staging mechanics shipped in
``xmclaw/core/evolution/staging.py`` and
``xmclaw/core/evolution/promotion_policy.py``. Controller wiring is
deferred to a follow-up ticket; what's verified here is the pure
mechanics — every gate runs deterministically, the bundle aggregates
correctly, and each policy maps the bundle to the right decision.

References:
  * Iron Rule #2 in ``docs/EVOLUTION_HONEST_STATE.md``: "Staging →
    gate → explicit promote. The orchestrator never mutates
    `SkillRegistry` HEAD inline."
"""
from __future__ import annotations

import pytest

from xmclaw.core.evolution.promotion_policy import (
    Decision,
    PromotionPolicy,
    decide,
    is_high_risk,
)
from xmclaw.core.evolution.staging import (
    Candidate,
    GateBundle,
    GateResult,
    gate_growth_limit,
    gate_holdout_test,
    gate_size_limit,
    gate_structure_validation,
    run_gates,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _candidate(text: str, *, cid: str = "c1", skill_id: str = "s1", version: int = 1) -> Candidate:
    return Candidate(
        id=cid,
        skill_id=skill_id,
        version=version,
        source_text=text,
        metadata={"source": "test"},
        created_at=0.0,
    )


_VALID_PYTHON_SKILL = '''"""A small skill body used as a baseline test fixture."""

def run(ctx):
    return ctx.greet("world")
'''

_VALID_MD_SKILL = """---
name: example_skill
version: 1
---

# Example skill

Some description.
"""

_SHAPELESS_PROSE = "this is just plain prose with no python and no headers and no frontmatter"


# ---------------------------------------------------------------------------
# Per-gate tests — passing + failing for each of the 4 gates.
# ---------------------------------------------------------------------------


class TestGateSizeLimit:
    def test_passes_when_under_max(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_size_limit(c, max_kb=100)
        assert result.name == "size_limit"
        assert result.status == "passed"
        assert result.evidence["max_kb"] == 100
        assert result.evidence["size_bytes"] > 0
        assert result.reason is None

    def test_fails_when_over_max(self) -> None:
        big = "x" * (200 * 1024)  # 200 KB
        c = _candidate(big)
        result = gate_size_limit(c, max_kb=100)
        assert result.status == "failed"
        assert result.evidence["size_kb"] > 100
        assert "100 KB" in (result.reason or "")


class TestGateGrowthLimit:
    def test_passes_when_first_version(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_growth_limit(c, head_source=None, max_ratio=2.0)
        assert result.status == "passed"
        assert result.evidence["reason"] == "first_version"

    def test_fails_when_balloons_past_ratio(self) -> None:
        head = "small body"
        cand = "small body " + ("payload " * 1000)
        c = _candidate(cand)
        result = gate_growth_limit(c, head_source=head, max_ratio=2.0)
        assert result.status == "failed"
        assert result.evidence["ratio"] > 2.0
        assert "HEAD" in (result.reason or "")

    def test_passes_when_within_ratio(self) -> None:
        head = "abcdef" * 100
        c = _candidate(head + "extra")
        result = gate_growth_limit(c, head_source=head, max_ratio=2.0)
        assert result.status == "passed"
        assert result.evidence["ratio"] <= 2.0

    def test_passes_when_head_is_empty(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_growth_limit(c, head_source="", max_ratio=2.0)
        assert result.status == "passed"
        assert result.evidence["reason"] == "empty_head"


class TestGateStructureValidation:
    def test_passes_for_valid_python(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_structure_validation(c)
        assert result.status == "passed"
        assert result.evidence["has_python_structure"] is True

    def test_passes_for_markdown_with_frontmatter(self) -> None:
        c = _candidate(_VALID_MD_SKILL)
        result = gate_structure_validation(c)
        assert result.status == "passed"
        assert result.evidence["has_doc_structure"] is True

    def test_fails_for_shapeless_prose(self) -> None:
        c = _candidate(_SHAPELESS_PROSE)
        result = gate_structure_validation(c)
        assert result.status == "failed"
        assert result.evidence["has_python_structure"] is False
        assert result.evidence["has_doc_structure"] is False

    def test_fails_for_empty_body(self) -> None:
        c = _candidate("   \n  ")
        result = gate_structure_validation(c)
        assert result.status == "failed"
        assert result.evidence["empty"] is True

    def test_records_python_syntax_error_finding(self) -> None:
        c = _candidate("def broken(:\n    pass\n")
        result = gate_structure_validation(c)
        # Python hint matched but parse failed; with no markdown either, fails.
        assert result.status == "failed"
        findings = result.evidence["findings"]
        assert any("python_parse_error" in f for f in findings)


class TestGateHoldoutTest:
    def test_skipped_when_test_id_missing(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_holdout_test(c, test_id=None)
        assert result.status == "skipped"
        assert result.evidence["test_id"] is None

    def test_passed_when_test_id_present(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        result = gate_holdout_test(c, test_id="holdout_42")
        assert result.status == "passed"
        assert result.evidence["test_id"] == "holdout_42"


# ---------------------------------------------------------------------------
# GateBundle aggregation.
# ---------------------------------------------------------------------------


class TestGateBundleAggregation:
    def test_passed_all_when_all_pass(self) -> None:
        bundle = GateBundle(results=tuple(
            GateResult(name=n, status="passed") for n in ("a", "b", "c")
        ))
        assert bundle.passed_all is True
        assert bundle.failed_any is False
        assert bundle.skipped_any is False

    def test_passed_all_tolerates_skips(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="a", status="passed"),
            GateResult(name="b", status="skipped"),
        ))
        assert bundle.passed_all is True
        assert bundle.failed_any is False
        assert bundle.skipped_any is True

    def test_passed_all_false_when_any_failure(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="a", status="passed"),
            GateResult(name="b", status="failed"),
            GateResult(name="c", status="passed"),
        ))
        assert bundle.passed_all is False
        assert bundle.failed_any is True

    def test_by_name_lookup(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="size_limit", status="passed"),
            GateResult(name="growth_limit", status="failed"),
        ))
        size = bundle.by_name("size_limit")
        growth = bundle.by_name("growth_limit")
        assert size is not None and size.status == "passed"
        assert growth is not None and growth.status == "failed"
        assert bundle.by_name("nope") is None


# ---------------------------------------------------------------------------
# `run_gates` aggregator behaviour.
# ---------------------------------------------------------------------------


class TestRunGates:
    def test_holdout_policy_require_rejects_skip(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        bundle = run_gates(c, head_source=None, test_id=None, holdout_policy="require")
        holdout = bundle.by_name("holdout_test")
        assert holdout is not None
        assert holdout.status == "failed"
        assert "require" in (holdout.reason or "")
        assert bundle.failed_any is True

    def test_holdout_policy_skip_if_absent_tolerates_missing_test(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        bundle = run_gates(c, head_source=None, test_id=None, holdout_policy="skip_if_absent")
        holdout = bundle.by_name("holdout_test")
        assert holdout is not None
        assert holdout.status == "skipped"
        assert bundle.failed_any is False
        assert bundle.skipped_any is True

    def test_holdout_policy_skip_always(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        bundle = run_gates(c, head_source=None, test_id="real_test", holdout_policy="skip_always")
        holdout = bundle.by_name("holdout_test")
        assert holdout is not None
        assert holdout.status == "skipped"
        assert holdout.evidence["policy"] == "skip_always"

    def test_holdout_passes_with_test_id(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        bundle = run_gates(c, head_source=None, test_id="holdout_99", holdout_policy="skip_if_absent")
        holdout = bundle.by_name("holdout_test")
        assert holdout is not None
        assert holdout.status == "passed"
        assert bundle.passed_all is True
        assert bundle.skipped_any is False

    def test_run_gates_is_deterministic(self) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        b1 = run_gates(c, head_source="prior", test_id="h", holdout_policy="skip_if_absent")
        b2 = run_gates(c, head_source="prior", test_id="h", holdout_policy="skip_if_absent")
        assert tuple(r.name for r in b1.results) == tuple(r.name for r in b2.results)
        assert tuple(r.status for r in b1.results) == tuple(r.status for r in b2.results)
        # Order is the canonical size → growth → structure → holdout sequence.
        assert tuple(r.name for r in b1.results) == (
            "size_limit",
            "growth_limit",
            "structure_validation",
            "holdout_test",
        )

    def test_candidate_id_stability(self) -> None:
        # Candidate is frozen; the same id round-trips through gate evidence.
        c = _candidate(_VALID_PYTHON_SKILL, cid="stable-id-7")
        bundle = run_gates(c, head_source=None, test_id=None)
        # The id field never appears in gate evidence (gates don't read it),
        # but the dataclass equality lets us pin its stability across runs.
        assert c.id == "stable-id-7"
        assert c == _candidate(_VALID_PYTHON_SKILL, cid="stable-id-7")
        # And the bundle evidence is independent of the id.
        bundle2 = run_gates(_candidate(_VALID_PYTHON_SKILL, cid="other-id"))
        assert tuple(r.status for r in bundle.results) == tuple(r.status for r in bundle2.results)


# ---------------------------------------------------------------------------
# `is_high_risk` heuristic.
# ---------------------------------------------------------------------------


class TestIsHighRisk:
    def test_skipped_holdout_is_high_risk(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="size_limit", status="passed"),
            GateResult(name="growth_limit", status="passed"),
            GateResult(name="structure_validation", status="passed", evidence={"findings": []}),
            GateResult(name="holdout_test", status="skipped"),
        ))
        assert is_high_risk(bundle) is True

    def test_structure_findings_are_high_risk(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="size_limit", status="passed"),
            GateResult(name="growth_limit", status="passed"),
            GateResult(
                name="structure_validation",
                status="passed",
                evidence={"findings": ["python_no_top_level_def_or_import"]},
            ),
            GateResult(name="holdout_test", status="passed"),
        ))
        assert is_high_risk(bundle) is True

    def test_clean_pass_is_low_risk(self) -> None:
        bundle = GateBundle(results=(
            GateResult(name="size_limit", status="passed"),
            GateResult(name="growth_limit", status="passed"),
            GateResult(name="structure_validation", status="passed", evidence={"findings": []}),
            GateResult(name="holdout_test", status="passed"),
        ))
        assert is_high_risk(bundle) is False


# ---------------------------------------------------------------------------
# `decide` — every policy × every bundle shape.
# ---------------------------------------------------------------------------


def _all_pass_bundle() -> GateBundle:
    return GateBundle(results=(
        GateResult(name="size_limit", status="passed"),
        GateResult(name="growth_limit", status="passed"),
        GateResult(name="structure_validation", status="passed", evidence={"findings": []}),
        GateResult(name="holdout_test", status="passed"),
    ))


def _holdout_skipped_bundle() -> GateBundle:
    return GateBundle(results=(
        GateResult(name="size_limit", status="passed"),
        GateResult(name="growth_limit", status="passed"),
        GateResult(name="structure_validation", status="passed", evidence={"findings": []}),
        GateResult(name="holdout_test", status="skipped"),
    ))


def _failed_bundle() -> GateBundle:
    return GateBundle(results=(
        GateResult(name="size_limit", status="failed", reason="too big"),
        GateResult(name="growth_limit", status="passed"),
        GateResult(name="structure_validation", status="passed", evidence={"findings": []}),
        GateResult(name="holdout_test", status="passed"),
    ))


class TestDecide:
    def test_failure_rejects_under_every_policy(self) -> None:
        bundle = _failed_bundle()
        for policy in PromotionPolicy:
            decision = decide(bundle, policy)
            assert isinstance(decision, Decision)
            assert decision.action == "reject", f"{policy} did not reject a failed bundle"
            assert "size_limit" in decision.reason

    def test_auto_promotes_on_clean_pass(self) -> None:
        decision = decide(_all_pass_bundle(), PromotionPolicy.AUTO_ON_PASS_ALL)
        assert decision.action == "promote"
        assert "AUTO_ON_PASS_ALL" in decision.reason

    def test_auto_holds_when_skipped(self) -> None:
        decision = decide(_holdout_skipped_bundle(), PromotionPolicy.AUTO_ON_PASS_ALL)
        assert decision.action == "hold_for_human"
        assert "skipped" in decision.reason
        assert "holdout_test" in decision.reason

    def test_human_required_always_holds_clean_pass(self) -> None:
        decision = decide(_all_pass_bundle(), PromotionPolicy.HUMAN_REQUIRED_ALWAYS)
        assert decision.action == "hold_for_human"
        assert "HUMAN_REQUIRED_ALWAYS" in decision.reason

    def test_human_required_for_high_risk_holds_skipped(self) -> None:
        decision = decide(
            _holdout_skipped_bundle(), PromotionPolicy.HUMAN_REQUIRED_FOR_HIGH_RISK
        )
        assert decision.action == "hold_for_human"
        assert "high-risk" in decision.reason

    def test_human_required_for_high_risk_promotes_clean_pass(self) -> None:
        decision = decide(
            _all_pass_bundle(), PromotionPolicy.HUMAN_REQUIRED_FOR_HIGH_RISK
        )
        assert decision.action == "promote"
        assert "low-risk" in decision.reason


# ---------------------------------------------------------------------------
# End-to-end sanity: run_gates → decide.
# ---------------------------------------------------------------------------


class TestRunGatesThenDecide:
    @pytest.mark.parametrize(
        "policy,expected_action",
        [
            (PromotionPolicy.AUTO_ON_PASS_ALL, "promote"),
            (PromotionPolicy.HUMAN_REQUIRED_ALWAYS, "hold_for_human"),
            (PromotionPolicy.HUMAN_REQUIRED_FOR_HIGH_RISK, "promote"),
        ],
    )
    def test_clean_candidate_with_holdout(
        self, policy: PromotionPolicy, expected_action: str
    ) -> None:
        c = _candidate(_VALID_PYTHON_SKILL)
        bundle = run_gates(c, head_source=None, test_id="holdout_ok", holdout_policy="skip_if_absent")
        assert bundle.passed_all is True
        decision = decide(bundle, policy)
        assert decision.action == expected_action

    def test_shapeless_prose_rejects(self) -> None:
        c = _candidate(_SHAPELESS_PROSE)
        bundle = run_gates(c)
        decision = decide(bundle, PromotionPolicy.AUTO_ON_PASS_ALL)
        assert decision.action == "reject"
