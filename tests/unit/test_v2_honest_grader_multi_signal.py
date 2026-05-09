"""HonestGrader multi-signal rewrite — Sprint 3 Iron Rule #1 tests.

The audit identified the prior HonestGrader's #1 weakness as 70-80%
of every score coming from "tool didn't crash" and a gameable LLM
self-rating capped at 0.20. Sprint 3 splits the grader into TWO
independent signal layers and refuses to single-signal-promote.
These tests pin every important corner of the new contract:

* Single-signal-only verdict → ``promote_eligible=False`` (Iron Rule #1).
* Both signals applicable + both pass thresholds → ``promote_eligible=True``.
* Tightened ``ran`` rejects empty / whitespace / fake-success sentinels.
* ``type_matched`` is ``None`` (not True) when no ``expected_type`` declared.
* ``side_effect_observable`` covers fs + memory + bus emissions; unverified
  schemes no longer score True.
* ``UserFollowupSignal``: negative pattern → 0.0, sustained positive → 0.7,
  thumbs-up reaction → 1.0, no follow-up → ``None``.
* ``HoldoutTestSignal``: ``eval_test_id`` resolved → score; missing → ``None``.
* ``CrossJudgeSignal``: agreement → mean; disagreement (>delta) → 0.0
  (NEGATIVE signal, not a positive consensus).
* ``GraderVerdict.to_payload`` round-trips losslessly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, make_event
from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.core.grader import (
    CrossJudgeSignal,
    GraderVerdict,
    HoldoutTestSignal,
    HonestGrader,
    UserFollowupSignal,
)
from xmclaw.core.grader._signals import best_independent_score
from xmclaw.core.grader.checks import (
    check_ran,
    check_side_effect_observable,
    check_type_matched,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _finished(**payload: object) -> BehavioralEvent:
    return make_event(
        session_id="t",
        agent_id="t",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload=payload,
    )


def _user_msg(text: str, ts_offset: float = 1.0, session_id: str = "t") -> BehavioralEvent:
    """Construct a ``USER_MESSAGE`` event placed AFTER the prior event in
    a session. ``ts_offset`` is added to the current time so we can
    order multiple follow-ups deterministically without sleeping."""
    e = make_event(
        session_id=session_id,
        agent_id="user",
        type=EventType.USER_MESSAGE,
        payload={"text": text},
    )
    return BehavioralEvent(
        id=e.id,
        ts=e.ts + ts_offset,  # force after the parent event
        session_id=e.session_id,
        agent_id=e.agent_id,
        type=e.type,
        payload=e.payload,
    )


# ── tightened check_ran (Sprint 3) ───────────────────────────────────────


def test_check_ran_rejects_empty_string_result() -> None:
    """Pre-Sprint-3 this returned True — call_id existed and result was
    present. Sprint 3 tightening: empty string is "fake success"."""
    ok, ev = check_ran(_finished(call_id="x", result=""))
    assert ok is False
    assert any("non-trivial check failed" in e for e in ev)


def test_check_ran_rejects_whitespace_only_result() -> None:
    ok, ev = check_ran(_finished(call_id="x", result="   \n\t  "))
    assert ok is False
    assert any("empty / whitespace-only" in e for e in ev)


def test_check_ran_rejects_ok_sentinel() -> None:
    ok, ev = check_ran(_finished(call_id="x", result="ok"))
    assert ok is False
    assert any("fake-success sentinel" in e for e in ev)


def test_check_ran_rejects_done_sentinel() -> None:
    ok, _ = check_ran(_finished(call_id="x", result="done"))
    assert ok is False


def test_check_ran_rejects_true_sentinel_string() -> None:
    """The literal string 'true' is fake-success. The boolean True
    (covered separately) is structural data and stays True."""
    ok, _ = check_ran(_finished(call_id="x", result="true"))
    assert ok is False


def test_check_ran_rejects_empty_list() -> None:
    ok, _ = check_ran(_finished(call_id="x", result=[]))
    assert ok is False


def test_check_ran_rejects_empty_dict() -> None:
    ok, _ = check_ran(_finished(call_id="x", result={}))
    assert ok is False


def test_check_ran_accepts_real_string() -> None:
    ok, _ = check_ran(_finished(call_id="x", result="hello world"))
    assert ok is True


def test_check_ran_accepts_dict_with_payload() -> None:
    ok, _ = check_ran(_finished(call_id="x", result={"k": 1}))
    assert ok is True


def test_check_ran_rejects_errored_call() -> None:
    ok, ev = check_ran(_finished(call_id="x", result="hi", error="boom"))
    assert ok is False
    assert any("errored" in e for e in ev)


# ── tightened check_type_matched (Sprint 3) ──────────────────────────────


def test_type_matched_returns_none_when_no_expected_type() -> None:
    """Iron Rule #1 tightening: previously returned True with caveat;
    now returns None so the grader's combiner skips it."""
    ok, ev = check_type_matched(_finished(result="hi"))
    assert ok is None
    assert any("not applicable" in e for e in ev)


def test_type_matched_true_when_declared_and_match() -> None:
    ok, _ = check_type_matched(_finished(result="hi", expected_type="str"))
    assert ok is True


def test_type_matched_false_when_declared_and_mismatch() -> None:
    ok, _ = check_type_matched(_finished(result=42, expected_type="str"))
    assert ok is False


# ── tightened check_side_effect_observable (Sprint 3) ────────────────────


def test_side_effect_memory_uri_observed() -> None:
    """Sprint 3 extension: memory:// URIs are verified via
    payload.memory_writes / memory_op."""
    ev = _finished(
        call_id="x",
        result="ok",  # would fail check_ran but doesn't matter here
        expected_side_effects=["memory://builtin/key-foo"],
        memory_writes=["key-foo"],
    )
    v, evidence = check_side_effect_observable(ev)
    assert v is True
    assert any("memory write observed" in e for e in evidence)


def test_side_effect_memory_uri_missing() -> None:
    ev = _finished(
        expected_side_effects=["memory://builtin/key-foo"],
    )
    v, _ = check_side_effect_observable(ev)
    assert v is False


def test_side_effect_bus_emission_observed() -> None:
    ev = _finished(
        expected_side_effects=["bus://memory_op"],
        bus_emissions=["memory_op", "cost_tick"],
    )
    v, evidence = check_side_effect_observable(ev)
    assert v is True
    assert any("bus emission observed" in e for e in evidence)


def test_side_effect_unverified_scheme_no_longer_passes_silently() -> None:
    """Pre-Sprint-3 returned True for http:// / redis:// (no verifier).
    Now: when EVERY declared side effect uses an unverified scheme, we
    return False so the grader can't take credit for what it can't see."""
    ev = _finished(expected_side_effects=["http://example.com/foo"])
    v, evidence = check_side_effect_observable(ev)
    assert v is False
    assert any("unverified scheme" in e for e in evidence)


def test_side_effect_returns_none_when_none_declared() -> None:
    v, _ = check_side_effect_observable(_finished())
    assert v is None


# ── HonestGrader.grade — Iron Rule #1 promote_eligible ───────────────────


@pytest.mark.asyncio
async def test_single_signal_only_blocks_promotion() -> None:
    """Iron Rule #1: when only Signal A is available, promote_eligible
    is always False — never single-signal-promote."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="real output",
        expected_type="str",
        expected_side_effects=[],
    ))
    assert v.deterministic_score >= 0.6
    assert v.independent_score is None
    assert v.independent_kind == "none"
    assert v.promote_eligible is False
    assert any("single-signal" in n.lower() for n in v.notes)


@pytest.mark.asyncio
async def test_both_signals_pass_promote_eligible_true() -> None:
    """Both Signal A and Signal B clear thresholds → promote_eligible=True."""
    g = HonestGrader()
    event = _finished(
        call_id="x", result="rich payload",
        expected_type="str",
        expected_side_effects=[],
    )
    # Build a follow-up that triggers UserFollowupSignal positively.
    history = [event, _user_msg("great, now do the next step please", ts_offset=1.0)]
    v = await g.grade(event, history=history)
    assert v.deterministic_score >= 0.6
    assert v.independent_score is not None
    assert v.independent_score >= 0.5
    assert v.promote_eligible is True


@pytest.mark.asyncio
async def test_signal_a_below_floor_blocks_even_with_signal_b() -> None:
    """Even if independent signal fires positively, deterministic floor
    must clear too. Iron Rule #1 is multiplicative, not additive."""
    g = HonestGrader()
    # ran=False (no result), no expected_type → deterministic ~ 0.
    event = _finished(call_id="x")  # missing result → ran=False
    history = [event, _user_msg("thumbs-up keep going", ts_offset=1.0)]
    v = await g.grade(event, history=history)
    assert v.deterministic_score < 0.6
    assert v.promote_eligible is False
    assert any("deterministic" in n.lower() for n in v.notes)


@pytest.mark.asyncio
async def test_signal_b_below_floor_blocks_even_with_signal_a() -> None:
    """Negative user reaction (independent_score=0.0) blocks even when
    deterministic checks all pass."""
    g = HonestGrader()
    event = _finished(
        call_id="x", result="real output",
        expected_type="str",
        expected_side_effects=[],
    )
    history = [event, _user_msg("undo that, wrong direction", ts_offset=1.0)]
    v = await g.grade(event, history=history)
    assert v.deterministic_score >= 0.6
    assert v.independent_score == 0.0
    assert v.promote_eligible is False


# ── UserFollowupSignal ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_followup_negative_redo_pattern() -> None:
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    history = [parent, _user_msg("please redo that, it's wrong", ts_offset=1.0)]
    score, ev = await sig.probe(parent, history=history)
    assert score == 0.0
    assert "pattern" in ev


@pytest.mark.asyncio
async def test_user_followup_negative_chinese_pattern() -> None:
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    history = [parent, _user_msg("不对，重做", ts_offset=1.0)]
    score, ev = await sig.probe(parent, history=history)
    assert score == 0.0
    assert ev.get("language") == "zh"


@pytest.mark.asyncio
async def test_user_followup_thumbs_up_reaction() -> None:
    """Future chat-reaction frame: thumbs-up → 1.0."""
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    follow = _user_msg("thanks, perfect", ts_offset=1.0)
    follow_with_reaction = BehavioralEvent(
        id=follow.id, ts=follow.ts, session_id=follow.session_id,
        agent_id=follow.agent_id, type=follow.type,
        payload={**follow.payload, "reactions": {"thumbs_up": True}},
    )
    score, ev = await sig.probe(parent, history=[parent, follow_with_reaction])
    assert score == 1.0
    assert ev.get("reaction") == "thumbs_up"


@pytest.mark.asyncio
async def test_user_followup_sustained_engagement() -> None:
    """≥3 follow-ups, none negative → 0.7."""
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    history = [
        parent,
        _user_msg("ok cool", ts_offset=1.0),
        _user_msg("now what about X", ts_offset=2.0),
        _user_msg("and also Y", ts_offset=3.0),
    ]
    score, ev = await sig.probe(parent, history=history)
    assert score == 0.7
    assert ev.get("reason") == "sustained_engagement"


@pytest.mark.asyncio
async def test_user_followup_neutral_single_reply() -> None:
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    history = [parent, _user_msg("hmm let me think", ts_offset=1.0)]
    score, _ = await sig.probe(parent, history=history)
    assert score == 0.5


@pytest.mark.asyncio
async def test_user_followup_no_history_returns_none() -> None:
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    score, _ = await sig.probe(parent, history=None)
    assert score is None


@pytest.mark.asyncio
async def test_user_followup_no_followup_in_history_returns_none() -> None:
    """Conversation ended on the tool call — signal not applicable."""
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    score, _ = await sig.probe(parent, history=[parent])
    assert score is None


@pytest.mark.asyncio
async def test_user_followup_filters_other_session() -> None:
    """A follow-up from a different session must not be picked up."""
    sig = UserFollowupSignal()
    parent = _finished(call_id="x", result="hi")
    foreign = _user_msg(
        "redo this please", ts_offset=1.0, session_id="other-session",
    )
    score, _ = await sig.probe(parent, history=[parent, foreign])
    assert score is None


# ── HoldoutTestSignal ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_holdout_test_no_eval_id_returns_none() -> None:
    """No registered holdout → signal not applicable."""
    sig = HoldoutTestSignal()
    score, _ = await sig.probe(_finished(call_id="x", result="hi"))
    assert score is None


@pytest.mark.asyncio
async def test_holdout_test_explicit_pass() -> None:
    """When the eval_test_id is set AND payload carries an explicit
    holdout_test_passed=True (test override), the signal scores 1.0."""
    sig = HoldoutTestSignal()
    score, ev = await sig.probe(_finished(
        call_id="x", result="hi",
        eval_test_id="check_file_exists",
        holdout_test_passed=True,
    ))
    assert score == 1.0
    assert ev["passed"] is True


@pytest.mark.asyncio
async def test_holdout_test_explicit_fail() -> None:
    sig = HoldoutTestSignal()
    score, _ = await sig.probe(_finished(
        call_id="x", result="hi",
        eval_test_id="check_file_exists",
        holdout_test_passed=False,
    ))
    assert score == 0.0


@pytest.mark.asyncio
async def test_holdout_test_id_set_no_executor_returns_none() -> None:
    """When eval_test_id is registered but no override is supplied
    AND the executor isn't wired (today's reality), return None
    so we never score-promote on missing infrastructure."""
    sig = HoldoutTestSignal()
    score, ev = await sig.probe(_finished(
        call_id="x", result="hi",
        eval_test_id="check_file_exists",
    ))
    assert score is None
    assert ev.get("status") == "stub_no_executor_yet"


# ── CrossJudgeSignal ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_judge_disagreement_is_negative_signal() -> None:
    """Disagreement between two judges (Δ > 0.15) → 0.0, NOT mean.
    This is the explicit ICLR finding the design was built on:
    multi-judge debate ceiling = best single agent; disagreement is
    a NEGATIVE signal, not a positive consensus."""
    sig = CrossJudgeSignal(delta=0.15)
    score, ev = await sig.probe(_finished(
        call_id="x", result="hi",
        cross_judge_a=0.9, cross_judge_b=0.3,  # diff = 0.6 > 0.15
    ))
    assert score == 0.0
    assert ev["verdict"] == "disagreement_penalty"


@pytest.mark.asyncio
async def test_cross_judge_agreement_returns_mean() -> None:
    sig = CrossJudgeSignal(delta=0.15)
    score, ev = await sig.probe(_finished(
        call_id="x", result="hi",
        cross_judge_a=0.8, cross_judge_b=0.7,  # diff = 0.1 < 0.15
    ))
    assert abs(score - 0.75) < 1e-6
    assert ev["verdict"] == "agreement"


@pytest.mark.asyncio
async def test_cross_judge_one_missing_returns_none() -> None:
    sig = CrossJudgeSignal()
    score, _ = await sig.probe(_finished(
        call_id="x", result="hi", cross_judge_a=0.8,
    ))
    assert score is None


@pytest.mark.asyncio
async def test_cross_judge_clamps_out_of_range_inputs() -> None:
    """Robustness: a judge that returned 1.4 must not break the diff
    computation. Inputs are clamped to [0, 1] before comparison."""
    sig = CrossJudgeSignal(delta=0.15)
    score, _ = await sig.probe(_finished(
        cross_judge_a=1.4, cross_judge_b=0.95,  # both clamp to ~1.0 → agreement
    ))
    assert score is not None
    assert 0.95 <= score <= 1.0


# ── best_independent_score helper ────────────────────────────────────────


@pytest.mark.asyncio
async def test_best_independent_score_picks_first_applicable() -> None:
    """The first signal that fires wins — others are not probed.
    Order matters: UserFollowup is the only fully-implemented signal,
    so it goes first by default."""
    parent = _finished(call_id="x", result="hi")
    history = [parent, _user_msg("undo this", ts_offset=1.0)]
    sigs = [UserFollowupSignal(), HoldoutTestSignal(), CrossJudgeSignal()]
    score, kind, _ = await best_independent_score(sigs, parent, history=history)
    assert score == 0.0
    assert kind == "user_followup"


@pytest.mark.asyncio
async def test_best_independent_score_falls_through_when_first_inapplicable() -> None:
    """When the first signal returns None, we try the next."""
    parent = _finished(
        call_id="x", result="hi",
        eval_test_id="check_x", holdout_test_passed=True,
    )
    sigs = [UserFollowupSignal(), HoldoutTestSignal(), CrossJudgeSignal()]
    # No history → UserFollowup returns None → falls through to Holdout.
    score, kind, _ = await best_independent_score(sigs, parent, history=None)
    assert score == 1.0
    assert kind == "holdout_test"


@pytest.mark.asyncio
async def test_best_independent_score_all_inapplicable_returns_none() -> None:
    parent = _finished(call_id="x", result="hi")
    sigs = [UserFollowupSignal(), HoldoutTestSignal(), CrossJudgeSignal()]
    score, kind, _ = await best_independent_score(sigs, parent, history=None)
    assert score is None
    assert kind == "none"


# ── GraderVerdict serialization round-trip ──────────────────────────────


@pytest.mark.asyncio
async def test_verdict_to_payload_round_trip() -> None:
    """Sprint 3: bus event payloads must round-trip through
    ``to_payload`` / ``from_payload`` losslessly so the audit log
    replay sees the same verdict the runtime saw."""
    g = HonestGrader()
    event = _finished(
        call_id="x", result="real output",
        expected_type="str", expected_side_effects=[],
    )
    history = [event, _user_msg("great work, please continue", ts_offset=1.0)]
    v = await g.grade(event, history=history)
    payload = v.to_payload()
    restored = GraderVerdict.from_payload(payload)

    assert restored.event_id == v.event_id
    assert restored.deterministic_score == v.deterministic_score
    assert restored.independent_score == v.independent_score
    assert restored.independent_kind == v.independent_kind
    assert restored.final_score == v.final_score
    assert restored.promote_eligible == v.promote_eligible
    assert restored.notes == v.notes
    assert restored.ran == v.ran


@pytest.mark.asyncio
async def test_verdict_to_payload_carries_required_fields() -> None:
    g = HonestGrader()
    event = _finished(
        call_id="x", result="hi",
        expected_type="str", expected_side_effects=[],
    )
    v = await g.grade(event)
    payload = v.to_payload()
    # Every field the bus consumers expect must be present.
    required = {
        "event_id", "deterministic_score", "deterministic_evidence",
        "ran", "returned", "type_matched", "side_effect_observable",
        "independent_score", "independent_kind", "independent_evidence",
        "final_score", "score", "promote_eligible", "notes",
        "evidence",
    }
    assert required.issubset(payload.keys())


# ── final_score range invariants ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_final_score_bounded_zero_to_one() -> None:
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="hi", expected_type="str", expected_side_effects=[],
    ))
    assert 0.0 <= v.final_score <= 1.0


@pytest.mark.asyncio
async def test_legacy_score_property_alias() -> None:
    """Back-compat: ``verdict.score`` returns ``final_score``. UCB1
    aggregator and other consumers still address ``score``."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="hi", expected_type="str", expected_side_effects=[],
    ))
    assert v.score == v.final_score


# ── side-effect: real fs path observed ──────────────────────────────────


@pytest.mark.asyncio
async def test_grader_with_real_fs_side_effect_full_signal_a() -> None:
    """End-to-end Signal A: every check returns True with declared
    expected_type AND a real fs path. deterministic_score should be 1.0."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        path = f.name
    try:
        g = HonestGrader()
        v = await g.grade(_finished(
            call_id="x", result={"k": 1}, error=None,
            expected_type="dict",
            expected_side_effects=[path],
        ))
        assert v.ran is True
        assert v.returned is True
        assert v.type_matched is True
        assert v.side_effect_observable is True
        assert abs(v.deterministic_score - 1.0) < 1e-6
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_grader_partial_signal_a_normalizes_weights() -> None:
    """When some checks are N/A (None), weights re-normalize across
    the applicable ones — never award free points for absent declarations."""
    g = HonestGrader()
    v = await g.grade(_finished(
        call_id="x", result="hi",
        # No expected_type, no expected_side_effects → both None.
    ))
    # Only ran + returned applicable. Both pass → 1.0.
    assert v.deterministic_score == 1.0
    assert v.type_matched is None
    assert v.side_effect_observable is None


# ── HonestGrader.grade with custom signal list ───────────────────────────


@pytest.mark.asyncio
async def test_custom_signal_list_used_in_order() -> None:
    """Caller-supplied signals replace the default trio."""
    g = HonestGrader(signals=[CrossJudgeSignal()])
    assert len(g.signals) == 1
    assert isinstance(g.signals[0], CrossJudgeSignal)


@pytest.mark.asyncio
async def test_default_signals_include_three() -> None:
    g = HonestGrader()
    kinds = {s.name for s in g.signals}
    assert kinds == {"user_followup", "holdout_test", "cross_judge"}
