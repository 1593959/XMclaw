"""End-to-end tool-execution loop — anti-req #1 closed in a real scenario.

Exercises the full path:

    Scheduler picks a tool-call candidate
      → BuiltinTools.invoke (real file-system side effects)
        → ToolResult (ok + side_effects)
          → tool_invocation_finished event (payload contains the
            real side_effects list from the result)
            → HonestGrader.grade
              → check_side_effect_observable verifies files exist
                → grader_verdict

The "real tool ran" path scores high because every hard check passes.
The "hallucinated tool call" counter-case (anti_req_violation event,
no tool actually ran) scores at most the 0.20 LLM-opinion cap — this
is anti-req #1 end-to-end, not just at the translator layer.

Phase 2.5 proves anti-req #1 WITH side-effects. Earlier phases proved
it at the translator layer (a malformed ``tool_use`` block produces
``None``); this test proves it at the scheduler→grader layer too.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, make_event
from xmclaw.core.grader import HonestGrader
from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


# ── real tool loop: grader verifies the file exists ───────────────────────


@pytest.mark.asyncio
async def test_real_tool_loop_scores_full_side_effect() -> None:
    """file_write → grader sees real side_effects → side_effect_observable=True."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        grader = HonestGrader()

        target = Path(tmp) / "greeting.txt"
        call = ToolCall(
            name="file_write",
            args={"path": str(target), "content": "hello v2"},
            provenance="anthropic",
        )

        result = await tools.invoke(call)
        assert result.ok
        assert target.exists()

        # The orchestrator would normally emit this event after invoking
        # the tool. Here we construct it directly to exercise the grader.
        finished = make_event(
            session_id="s", agent_id="a",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": result.call_id,
                "result": result.content,
                "error": result.error,
                "latency_ms": result.latency_ms,
                "expected_type": "dict",
                # The REAL side effects from the ToolResult — not a
                # hardcoded list, not a hint from the tool spec.
                "expected_side_effects": list(result.side_effects),
            },
        )
        verdict = await grader.grade(finished)
        assert verdict.ran is True
        assert verdict.returned is True
        assert verdict.type_matched is True
        assert verdict.side_effect_observable is True
        # Sprint-3 multi-signal redesign (verdict.py 52d0f33): deterministic
        # score is now the weighted pass-fraction of APPLICABLE checks
        # (ran .40 / returned .20 / type .20 / side_effect .20). All four
        # pass → 1.0. With no independent signal (history=None) the combined
        # final_score == deterministic_score. (Old 0.80/0.20 hard-vs-LLM
        # split is retired — see _legacy_score.)
        assert verdict.score == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_read_after_write_round_trip() -> None:
    """A two-tool chain: write then read; grader verifies each hop."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        grader = HonestGrader()

        path = Path(tmp) / "rt.txt"
        write_call = ToolCall(
            name="file_write",
            args={"path": str(path), "content": "round-trip"},
            provenance="anthropic",
        )
        w_result = await tools.invoke(write_call)
        assert w_result.ok and path.exists()

        read_call = ToolCall(
            name="file_read",
            args={"path": str(path)},
            provenance="anthropic",
        )
        r_result = await tools.invoke(read_call)
        assert r_result.ok
        assert r_result.content == "round-trip"

        # Grader on the read: pure read, side_effect = None (not applicable).
        read_event = make_event(
            session_id="s", agent_id="a",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": r_result.call_id,
                "result": r_result.content,
                "error": None,
                "expected_type": "str",
                "expected_side_effects": list(r_result.side_effects),  # ()
            },
        )
        r_verdict = await grader.grade(read_event)
        assert r_verdict.side_effect_observable is None  # pure read
        # side_effect is N/A for a pure read → dropped from numerator AND
        # denominator (no free points). Remaining ran+returned+type all
        # pass → re-normalized to 1.0. (Sprint-3 contract.)
        assert r_verdict.score == pytest.approx(1.0, abs=1e-6)


# ── anti-req #1 counter-case: hallucinated tool call scores badly ─────────


@pytest.mark.asyncio
async def test_hallucinated_tool_call_scores_zero() -> None:
    """Model said "I ran the tool" but no tool_invocation_finished happened.

    The bus records an anti_req_violation instead. Sprint-3 multi-signal
    redesign removed the LLM self-rating from the combined-score path
    entirely (self-flattery can no longer buy a 0.20 floor — it only
    enters via CrossJudgeSignal, which treats disagreement as NEGATIVE).
    So a pure hallucination now scores 0.0, not 0.20 — strictly stronger
    anti-req #1 enforcement: the runtime never rewards tool-call claims
    that didn't actually execute.
    """
    grader = HonestGrader()
    violation = make_event(
        session_id="s", agent_id="a",
        type=EventType.ANTI_REQ_VIOLATION,
        payload={
            "message": "model emitted text describing a tool call, no call fired",
            "llm_judge_opinion": "I definitely wrote the file!",
            "llm_judge_score": 1.0,  # maximum self-flattery — must NOT help
        },
    )
    verdict = await grader.grade(violation)
    assert verdict.ran is False
    assert verdict.returned is False
    assert verdict.type_matched is False
    # All hard checks fail; LLM self-flattery no longer floors the score.
    assert verdict.score == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_real_call_outscores_hallucination_by_wide_margin() -> None:
    """The separation between REAL-TOOL and TEXT-CLAIMED-TOOL is the
    numeric embodiment of anti-req #1. Under the Sprint-3 contract a real
    write scores 1.0 and a pure hallucination scores 0.0 — perfect
    separation. We assert real ≥ 3× fake, treating fake==0 (the current
    behavior) as the strongest possible separation rather than dividing
    by zero."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        grader = HonestGrader()

        # REAL
        call = ToolCall(
            name="file_write",
            args={"path": str(Path(tmp) / "r.txt"), "content": "real"},
            provenance="anthropic",
        )
        result = await tools.invoke(call)
        real_event = make_event(
            session_id="s", agent_id="a",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": result.call_id,
                "result": result.content,
                "error": None,
                "expected_type": "dict",
                "expected_side_effects": list(result.side_effects),
            },
        )
        real_score = (await grader.grade(real_event)).score

        # HALLUCINATED
        fake_event = make_event(
            session_id="s", agent_id="a",
            type=EventType.ANTI_REQ_VIOLATION,
            payload={
                "message": "I wrote the file",
                "llm_judge_score": 1.0,  # maximum self-flattery
            },
        )
        fake_score = (await grader.grade(fake_event)).score

        # fake==0 → perfect separation (real infinitely outscores fake).
        # Otherwise require ≥3× gap. Either way real must be clearly high.
        assert real_score >= 0.8, f"real write should score high, got {real_score:.3f}"
        gap_ok = fake_score == 0.0 or real_score / fake_score >= 3.0
        assert gap_ok, (
            f"anti-req #1 gap too small: real={real_score:.3f} "
            f"fake={fake_score:.3f} (need fake==0 or ratio≥3×)"
        )


# ── write-failure path: grader catches the lie ────────────────────────────


@pytest.mark.asyncio
async def test_write_claimed_but_permission_denied() -> None:
    """If a tool CLAIMS side_effects but the file didn't land (e.g. due
    to a bug where side_effects is populated before the write), the
    grader's check_side_effect_observable catches it.

    We simulate this by constructing a tool_invocation_finished event
    whose expected_side_effects points at a file that doesn't exist.
    check_side_effect_observable returns False, driving down the score.
    """
    grader = HonestGrader()
    bogus_path = "/tmp/this/path/definitely/does/not/exist/xyz-abc.txt"
    finished = make_event(
        session_id="s", agent_id="a",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "x",
            "result": {"path": bogus_path, "bytes": 12},  # looks ok
            "error": None,
            "expected_type": "dict",
            "expected_side_effects": [bogus_path],  # the lie
        },
    )
    verdict = await grader.grade(finished)
    # Sprint-3 contract: ran T(.40) + returned T(.20) + type T(.20) +
    # side_effect F(0) over weight_sum 1.0 → 0.80 (vs 1.0 for a clean
    # write). The side-effect lie costs exactly its 0.20 weight.
    assert verdict.ran is True
    assert verdict.returned is True
    assert verdict.type_matched is True
    assert verdict.side_effect_observable is False
    # The lie is penalized: strictly below a clean write (1.0).
    assert verdict.score == pytest.approx(0.80, abs=1e-6)
    assert verdict.score < 1.0
    # The real honesty gate moved from "low score" to promote-eligibility:
    # a single-signal verdict (no independent confirmation) can never be
    # promoted regardless of score (Iron Rule #1).
    assert verdict.promote_eligible is False
