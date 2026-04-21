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


def test_check_type_matched_no_declared_type_passes_with_caveat() -> None:
    ok, ev = check_type_matched(_finished(result="hi"))
    assert ok is True
    assert any("no expected_type" in e for e in ev)


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


def test_side_effect_non_fs_uri_unchecked() -> None:
    # Phase 1 does not verify http://, redis://, etc. — they're marked unchecked.
    v, ev = check_side_effect_observable(
        _finished(expected_side_effects=["http://example.com/foo"])
    )
    assert v is True  # no missing fs paths
    assert any("unchecked" in e for e in ev)


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
async def test_grader_perfect_structural_gives_0_80() -> None:
    """All hard checks pass, no LLM opinion → 0.80 (LLM cap absent)."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="hi", error=None,
        expected_type="str", expected_side_effects=[],
    ))
    # Hard checks fill (1 - 0.20) = 0.80 slot; all pass → 0.80 * 1.0 = 0.80.
    assert v.ran and v.returned and v.type_matched
    assert v.side_effect_observable is None
    assert abs(v.score - 0.80) < 1e-6


@pytest.mark.asyncio
async def test_llm_opinion_capped_even_with_all_hard_failing() -> None:
    """Anti-req #4: LLM score cannot exceed 0.20 regardless of opinion.

    Scenario: the model emitted text that *looked* like a tool call but
    wasn't — so the bus records an ``anti_req_violation`` event, not a
    ``tool_invocation_finished``. Every ground-truth check fails. Even if
    the model gives itself 1.0 subjective score, the grader must cap the
    total at 0.20.
    """
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
    assert not v.type_matched
    # Score = 0 * 0.80 + 1.0 * 0.20 = 0.20 — the cap.
    assert abs(v.score - 0.20) < 1e-6


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
async def test_evidence_non_empty() -> None:
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="c-99", result="hi", expected_type="str", expected_side_effects=[],
    ))
    assert len(v.evidence) >= 3  # at least one entry per hard check
    assert any("c-99" in e for e in v.evidence)
