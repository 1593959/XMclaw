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
        # All four hard checks pass → 1.0 × 0.80 = 0.80 (no LLM opinion)
        assert verdict.score == pytest.approx(0.80, abs=1e-6)


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
        # ran + returned + type_matched all pass; redistributed weights
        # give 1.0 × 0.80 for the non-LLM slot.
        assert r_verdict.score == pytest.approx(0.80, abs=1e-6)


# ── anti-req #1 counter-case: hallucinated tool call scores badly ─────────


@pytest.mark.asyncio
async def test_hallucinated_tool_call_scores_at_most_llm_cap() -> None:
    """Model said "I ran the tool" but no tool_invocation_finished happened.

    The bus records an anti_req_violation instead. Even if the model
    rates itself 1.0, the grader caps total score at 0.20 (anti-req #4).
    This is anti-req #1 end-to-end: the runtime never rewards
    tool-call claims that didn't actually execute.
    """
    grader = HonestGrader()
    violation = make_event(
        session_id="s", agent_id="a",
        type=EventType.ANTI_REQ_VIOLATION,
        payload={
            "message": "model emitted text describing a tool call, no call fired",
            "llm_judge_opinion": "I definitely wrote the file!",
            "llm_judge_score": 1.0,
        },
    )
    verdict = await grader.grade(violation)
    assert verdict.ran is False
    assert verdict.returned is False
    assert verdict.type_matched is False
    # With all hard checks failing and LLM opinion capped at 0.20:
    assert verdict.score == pytest.approx(0.20, abs=1e-6)


@pytest.mark.asyncio
async def test_real_call_outscores_hallucination_by_wide_margin() -> None:
    """The separation between REAL-TOOL and TEXT-CLAIMED-TOOL is the
    numeric embodiment of anti-req #1. This test asserts the gap is
    ≥ 3x — large enough to drive any reasonable bandit toward the real
    path and away from hallucination, no matter the signal noise."""
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

        ratio = real_score / fake_score
        assert ratio >= 3.0, (
            f"anti-req #1 gap too small: real={real_score:.3f} "
            f"fake={fake_score:.3f} ratio={ratio:.2f}x (need ≥3×)"
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
    # ran: T, returned: T, type_matched: T, side_effect_observable: F
    # score = 0.30×1 + 0.20×1 + 0.25×1 + 0.25×0 = 0.75 of the hard slot
    # → total = 0.75 × 0.80 = 0.60
    assert verdict.ran is True
    assert verdict.returned is True
    assert verdict.type_matched is True
    assert verdict.side_effect_observable is False
    assert verdict.score < 0.65  # a real successful write would score 0.80
