"""HonestGrader unit tests.

Anti-req #4: LLM opinion never exceeds 0.20 of total score, regardless of
what the LLM says about itself. Several tests here pin that invariant
directly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, make_event
from xmclaw.core.grader import HonestGrader
from xmclaw.core.grader.checks import (
    check_ran,
    check_returned,
    check_side_effect_observable,
    check_type_matched,
)


def _finished(**payload):  # noqa: ANN003
    return make_event(
        session_id="t", agent_id="t",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload=payload,
    )


# ── check_ran ─────────────────────────────────────────────────────────────

def test_check_ran_finished_event_with_call_id() -> None:
    ran, ev = check_ran(_finished(call_id="c-1", result=42))
    assert ran is True
    assert any("c-1" in e for e in ev)


def test_check_ran_missing_call_id() -> None:
    ran, _ = check_ran(_finished(result=42))
    assert ran is False


def test_check_ran_anti_req_violation_event() -> None:
    ev = make_event(
        session_id="t", agent_id="t", type=EventType.ANTI_REQ_VIOLATION,
        payload={"message": "looked like a tool call but wasn't"},
    )
    ran, _ = check_ran(ev)
    assert ran is False


# ── check_returned ────────────────────────────────────────────────────────

def test_check_returned_happy_path() -> None:
    ok, _ = check_returned(_finished(call_id="x", result="hello"))
    assert ok is True


def test_check_returned_error_set() -> None:
    ok, _ = check_returned(_finished(call_id="x", result="hello", error="boom"))
    assert ok is False


def test_check_returned_none_result() -> None:
    ok, _ = check_returned(_finished(call_id="x", result=None))
    assert ok is False


def test_check_returned_missing_key() -> None:
    ok, _ = check_returned(_finished(call_id="x"))
    assert ok is False


# ── check_type_matched ────────────────────────────────────────────────────

def test_check_type_matched_str() -> None:
    ok, _ = check_type_matched(_finished(result="hi", expected_type="str"))
    assert ok is True


def test_check_type_matched_mismatch() -> None:
    ok, _ = check_type_matched(_finished(result=42, expected_type="str"))
    assert ok is False


def test_check_type_matched_no_declared_type_returns_none_sprint3() -> None:
    """Sprint 3 Iron Rule #1 tightening: pre-Sprint-3 this returned True
    with a caveat. Now it returns None ("not applicable") so the grader's
    weighting layer skips the check entirely instead of awarding free
    points for a missing declaration."""
    ok, ev = check_type_matched(_finished(result="hi"))
    assert ok is None
    assert any("no expected_type declared" in e for e in ev)


def test_check_type_matched_dict() -> None:
    ok, _ = check_type_matched(_finished(result={"k": 1}, expected_type="dict"))
    assert ok is True


def test_check_type_matched_unknown_declared_type() -> None:
    ok, _ = check_type_matched(_finished(result="hi", expected_type="bogon"))
    assert ok is False


# ── check_side_effect_observable ──────────────────────────────────────────

def test_side_effect_none_when_not_applicable() -> None:
    v, ev = check_side_effect_observable(_finished(expected_side_effects=[]))
    assert v is None
    assert any("no side effects declared" in e for e in ev)


def test_side_effect_file_exists() -> None:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        path = f.name
    try:
        v, _ = check_side_effect_observable(
            _finished(expected_side_effects=[path])
        )
        assert v is True
    finally:
        Path(path).unlink(missing_ok=True)


def test_side_effect_file_missing() -> None:
    v, _ = check_side_effect_observable(
        _finished(expected_side_effects=["/definitely/does/not/exist/abc"])
    )
    assert v is False


def test_side_effect_non_fs_uri_unverified_sprint3() -> None:
    """Sprint 3 Iron Rule #1: pre-Sprint-3 silently returned True for
    http:// / redis:// schemes (no verifier yet). That was a free-points
    loophole. Now: if EVERY declared side effect is in an unverified
    scheme, return False so the grader can't take credit for what it
    can't observe. Schemes ``memory://`` / ``bus://`` ship with
    verifiers; everything else is "declared but unobservable"."""
    v, ev = check_side_effect_observable(
        _finished(expected_side_effects=["http://example.com/foo"])
    )
    assert v is False
    assert any("unverified scheme" in e for e in ev)


# ── HonestGrader.grade (anti-req #4 invariants) ───────────────────────────

@pytest.mark.asyncio
async def test_grader_score_bounds() -> None:
    g = HonestGrader()
    verdict = await g.grade(_finished(
        call_id="x", result="hi", error=None,
        expected_type="str", expected_side_effects=[],
    ))
    assert 0.0 <= verdict.score <= 1.0


@pytest.mark.asyncio
async def test_grader_perfect_structural_signal_a_only_sprint3() -> None:
    """Sprint 3 multi-signal: deterministic (Signal A) all-pass with no
    independent signal applicable → final = deterministic_score = 1.0
    (Signal A's own range), but ``promote_eligible`` is False because
    no Signal B fired. Iron Rule #1: never single-signal promote."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="hi", error=None,
        expected_type="str", expected_side_effects=[],
    ))
    assert v.ran and v.returned and v.type_matched
    assert v.side_effect_observable is None
    # Without an independent signal, final == deterministic_score, and
    # all applicable Signal A checks pass → 1.0 in the new range.
    assert abs(v.deterministic_score - 1.0) < 1e-6
    assert abs(v.final_score - 1.0) < 1e-6
    assert v.independent_score is None
    assert v.independent_kind == "none"
    assert v.promote_eligible is False  # Iron Rule #1


@pytest.mark.asyncio
async def test_llm_self_rating_no_longer_inflates_score_sprint3() -> None:
    """Sprint 3 Iron Rule #1: pre-Sprint-3, LLM self-rating could push the
    score to 0.20 even when every hard check failed. Now: LLM self-rating
    is REMOVED from the new combined score path entirely (its only path
    in is via :class:`CrossJudgeSignal`, which treats disagreement as
    NEGATIVE, never as a positive lift). With every hard check failing
    AND no independent signal applicable, final_score = 0.0."""
    g = HonestGrader()
    violation = make_event(
        session_id="t", agent_id="t", type=EventType.ANTI_REQ_VIOLATION,
        payload={
            "message": "text that described a tool call, not a real call",
            "llm_judge_score": 1.0,
            "llm_judge_opinion": "I totally executed that tool!",
        },
    )
    v = await g.grade(violation)
    assert not v.ran
    assert not v.returned
    # No expected_type declared → type_matched is None ("not applicable"),
    # type_matched/side_effect both excluded from the deterministic
    # weight pool. With ran=False AND returned=False, deterministic = 0.
    assert v.deterministic_score == 0.0
    assert v.final_score == 0.0
    assert v.promote_eligible is False


@pytest.mark.asyncio
async def test_llm_opinion_out_of_range_is_clamped() -> None:
    g = HonestGrader()
    v_high = await g.grade(_finished(
        call_id="x", result="hi", expected_type="str", expected_side_effects=[],
        llm_judge_score=42.0,  # deliberately out of range
    ))
    v_low = await g.grade(_finished(
        call_id="x", result="hi", expected_type="str", expected_side_effects=[],
        llm_judge_score=-5.0,
    ))
    # Both should produce valid scores in [0, 1].
    assert 0.0 <= v_high.score <= 1.0
    assert 0.0 <= v_low.score <= 1.0


@pytest.mark.asyncio
async def test_evidence_non_empty_sprint3() -> None:
    """Sprint 3: ``verdict.evidence`` is now a flat string list summarising
    the multi-signal verdict (deterministic / independent / final +
    eligibility). The structured per-check evidence lives in
    ``deterministic_evidence`` and ``independent_evidence``. This test
    pins both surfaces are populated."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="c-99", result="hi", expected_type="str", expected_side_effects=[],
    ))
    assert len(v.evidence) >= 3  # det / ind / final summary lines
    # Per-check structured evidence carries the call_id.
    ran_ev = v.deterministic_evidence["ran"]["evidence"]
    assert any("c-99" in e for e in ran_ev)
