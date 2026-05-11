"""Metacognition module unit tests — R3 (2026-05-10).

Three layers:
  * DecisionTraceRecorder: SQLite write/read/outcome backfill.
  * MetaCognitionPass: heuristic gate (min_evidence, all-ok filter)
    + LLM extraction shape + confidence cap.
  * Reformer: pattern.kind → proposal.kind mapping + min_confidence
    filter + bus emission.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.metacognition.pass_ import (
    CONFIDENCE_CAP,
    MetaCognitionPass,
    Pattern,
)
from xmclaw.core.metacognition.reformer import Reformer, ReformProposal
from xmclaw.core.metacognition.trace import (
    DecisionTrace,
    DecisionTraceRecorder,
)


# ── DecisionTraceRecorder ────────────────────────────────────────


def test_recorder_writes_and_reads_back(tmp_path: Path) -> None:
    rec = DecisionTraceRecorder(db_path=tmp_path / "d.db")
    t = DecisionTrace(
        session_id="s1", turn_id="t1", step=2,
        kind="tool_choice", chosen="bash",
        alternatives=["file_read", "web_search"],
        reason="bash is the broadest tool",
    )
    tid = rec.record(t)
    assert tid == t.id

    pulled = rec.recent(limit=10)
    assert len(pulled) == 1
    p = pulled[0]
    assert p.id == t.id
    assert p.session_id == "s1"
    assert p.kind == "tool_choice"
    assert p.chosen == "bash"
    assert p.alternatives == ["file_read", "web_search"]
    assert p.outcome == "unknown"
    rec.close()


def test_recorder_set_outcome_backfill(tmp_path: Path) -> None:
    rec = DecisionTraceRecorder(db_path=tmp_path / "d.db")
    t = DecisionTrace(
        session_id="s", turn_id="t", kind="tool_choice", chosen="x",
    )
    tid = rec.record(t)
    assert rec.set_outcome(tid, "user_pushed_back", "user said 'too verbose'")
    pulled = rec.recent(limit=10)[0]
    assert pulled.outcome == "user_pushed_back"
    assert pulled.outcome_note == "user said 'too verbose'"
    # Non-existent id returns False.
    assert not rec.set_outcome("ghost", "ok")
    rec.close()


def test_recorder_recent_filters_by_kind(tmp_path: Path) -> None:
    rec = DecisionTraceRecorder(db_path=tmp_path / "d.db")
    rec.record(DecisionTrace(kind="tool_choice", chosen="a"))
    rec.record(DecisionTrace(kind="skill_choice", chosen="b"))
    rec.record(DecisionTrace(kind="tool_choice", chosen="c"))
    only_tool = rec.recent(kind="tool_choice")
    assert len(only_tool) == 2
    assert {t.chosen for t in only_tool} == {"a", "c"}
    rec.close()


def test_recorder_recent_orders_newest_first(tmp_path: Path) -> None:
    rec = DecisionTraceRecorder(db_path=tmp_path / "d.db")
    t_old = DecisionTrace(ts=1.0, kind="tool_choice", chosen="old")
    t_new = DecisionTrace(ts=2.0, kind="tool_choice", chosen="new")
    rec.record(t_old)
    rec.record(t_new)
    pulled = rec.recent()
    assert pulled[0].chosen == "new"
    assert pulled[1].chosen == "old"
    rec.close()


def test_recorder_count(tmp_path: Path) -> None:
    rec = DecisionTraceRecorder(db_path=tmp_path / "d.db")
    assert rec.count() == 0
    rec.record(DecisionTrace(kind="tool_choice", chosen="x"))
    rec.record(DecisionTrace(kind="tool_choice", chosen="y"))
    assert rec.count() == 2
    rec.close()


# ── MetaCognitionPass ────────────────────────────────────────────


@dataclass
class _FakeLLMResp:
    content: str


@dataclass
class _ScriptedLLM:
    next_content: str = "[]"
    last_prompt: str = ""
    calls: int = 0

    async def complete(self, messages: list, tools: Any = None) -> Any:  # noqa: ARG002
        self.calls += 1
        self.last_prompt = messages[-1].content if messages else ""
        return _FakeLLMResp(content=self.next_content)


@dataclass
class _StubRecorder:
    """Minimal recorder duck for pass tests."""
    traces: list[DecisionTrace] = field(default_factory=list)

    def recent(
        self, *, limit: int = 200, kind: Any = None, since: Any = None,
    ) -> list[DecisionTrace]:
        return list(self.traces[-limit:])


@pytest.mark.asyncio
async def test_pass_skips_when_below_min_evidence() -> None:
    """Only 2 traces → < default min_evidence=3 → no LLM call,
    no patterns."""
    rec = _StubRecorder(traces=[
        DecisionTrace(kind="tool_choice", chosen="x", outcome="error"),
        DecisionTrace(kind="tool_choice", chosen="y", outcome="error"),
    ])
    llm = _ScriptedLLM(next_content="[]")
    p = MetaCognitionPass(llm=llm, recorder=rec)
    out = await p.run()
    assert out == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_pass_skips_when_all_traces_ok() -> None:
    """Many traces but all outcome=ok → nothing to learn from."""
    rec = _StubRecorder(traces=[
        DecisionTrace(id=f"t{i}", kind="tool_choice",
                      chosen=str(i), outcome="ok")
        for i in range(10)
    ])
    llm = _ScriptedLLM(next_content="[]")
    p = MetaCognitionPass(llm=llm, recorder=rec)
    out = await p.run()
    assert out == []
    assert llm.calls == 0  # filter saved the LLM call


@pytest.mark.asyncio
async def test_pass_returns_valid_pattern_with_clamped_confidence() -> None:
    rec = _StubRecorder(traces=[
        DecisionTrace(id="t1", kind="tool_choice", chosen="bash",
                      outcome="error", reason="apply_patch failed"),
        DecisionTrace(id="t2", kind="tool_choice", chosen="bash",
                      outcome="error", reason="apply_patch failed"),
        DecisionTrace(id="t3", kind="tool_choice", chosen="bash",
                      outcome="error", reason="apply_patch failed"),
    ])
    # LLM returns a pattern + claims confidence 0.95 — must clamp to 0.6.
    llm = _ScriptedLLM(next_content=json.dumps([{
        "kind": "repeated_failure",
        "summary": "apply_patch keeps failing on stale text",
        "evidence": ["t1", "t2", "t3"],
        "confidence": 0.95,
        "suggestion": "always re-read the file before patching",
        "recurrence": 3,
    }]))
    p = MetaCognitionPass(llm=llm, recorder=rec)
    out = await p.run()
    assert len(out) == 1
    pat = out[0]
    assert pat.kind == "repeated_failure"
    assert pat.confidence == CONFIDENCE_CAP   # clamped from 0.95
    assert set(pat.evidence) == {"t1", "t2", "t3"}


@pytest.mark.asyncio
async def test_pass_filters_pattern_with_invalid_evidence_ids() -> None:
    """LLM returned evidence ids that don't exist → drop the pattern
    (defensive against fabrication)."""
    rec = _StubRecorder(traces=[
        DecisionTrace(id=f"real-{i}", kind="tool_choice",
                      chosen="x", outcome="error")
        for i in range(3)
    ])
    llm = _ScriptedLLM(next_content=json.dumps([{
        "kind": "repeated_failure",
        "summary": "fake",
        "evidence": ["fake-1", "fake-2"],   # IDs not in the trace store
        "confidence": 0.5,
        "suggestion": "x",
        "recurrence": 2,
    }]))
    out = await MetaCognitionPass(llm=llm, recorder=rec).run()
    assert out == []


@pytest.mark.asyncio
async def test_pass_filters_pattern_kind_unknown() -> None:
    rec = _StubRecorder(traces=[
        DecisionTrace(id=f"t{i}", kind="tool_choice",
                      chosen="x", outcome="error")
        for i in range(3)
    ])
    llm = _ScriptedLLM(next_content=json.dumps([{
        "kind": "imaginary_kind",
        "summary": "x",
        "evidence": ["t0", "t1", "t2"],
        "confidence": 0.4,
        "suggestion": "y",
    }]))
    out = await MetaCognitionPass(llm=llm, recorder=rec).run()
    assert out == []


@pytest.mark.asyncio
async def test_pass_strips_markdown_fence() -> None:
    rec = _StubRecorder(traces=[
        DecisionTrace(id=f"t{i}", kind="tool_choice",
                      chosen="x", outcome="error")
        for i in range(3)
    ])
    llm = _ScriptedLLM(next_content=(
        '```json\n[{"kind":"repeated_failure",'
        '"summary":"x","evidence":["t0","t1","t2"],'
        '"confidence":0.5,"suggestion":"do better"}]\n```'
    ))
    out = await MetaCognitionPass(llm=llm, recorder=rec).run()
    assert len(out) == 1
    assert out[0].summary == "x"


@pytest.mark.asyncio
async def test_pass_handles_llm_exception() -> None:
    class _BoomLLM:
        async def complete(self, *_a, **_kw):
            raise RuntimeError("network died")

    rec = _StubRecorder(traces=[
        DecisionTrace(id=f"t{i}", kind="tool_choice",
                      chosen="x", outcome="error")
        for i in range(3)
    ])
    out = await MetaCognitionPass(llm=_BoomLLM(), recorder=rec).run()
    assert out == []


@pytest.mark.asyncio
async def test_pass_rejects_pattern_with_all_ok_evidence() -> None:
    """LLM hallucinated a "pattern" pointing only at successful
    traces — must be rejected (we don't learn from successes here)."""
    rec = _StubRecorder(traces=[
        DecisionTrace(id="t1", kind="tool_choice", chosen="x", outcome="ok"),
        DecisionTrace(id="t2", kind="tool_choice", chosen="x", outcome="ok"),
        DecisionTrace(id="t3", kind="tool_choice", chosen="x", outcome="ok"),
        # An error-trace exists, allowing the heuristic gate to pass…
        DecisionTrace(id="t4", kind="tool_choice", chosen="y", outcome="error"),
        DecisionTrace(id="t5", kind="tool_choice", chosen="y", outcome="error"),
        DecisionTrace(id="t6", kind="tool_choice", chosen="y", outcome="error"),
    ])
    llm = _ScriptedLLM(next_content=json.dumps([{
        "kind": "repeated_failure",
        "summary": "everything looks fine, actually",
        "evidence": ["t1", "t2", "t3"],   # all OK
        "confidence": 0.5,
        "suggestion": "n/a",
    }]))
    out = await MetaCognitionPass(llm=llm, recorder=rec).run()
    assert out == []


# ── Reformer ─────────────────────────────────────────────────────


def _make_pattern(
    kind: str = "repeated_failure",
    confidence: float = 0.5,
    summary: str = "x",
    suggestion: str = "do this",
    evidence: tuple = ("t1", "t2", "t3"),
) -> Pattern:
    return Pattern(
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        evidence=evidence,
        confidence=confidence,
        suggestion=suggestion,
        recurrence=len(evidence),
    )


def test_reformer_repeated_failure_to_curriculum_edit() -> None:
    p = _make_pattern(kind="repeated_failure", confidence=0.5,
                      suggestion="re-read file before patching")
    rp = Reformer().propose(p)
    assert rp.kind == "curriculum_edit"
    assert rp.payload["addendum"] == "re-read file before patching"
    assert rp.payload["tag"] == "repeated_failure"
    assert rp.payload["evidence_count"] == 3


def test_reformer_decline_overuse_to_curriculum_edit() -> None:
    p = _make_pattern(kind="decline_overuse", confidence=0.5)
    rp = Reformer().propose(p)
    assert rp.kind == "curriculum_edit"


def test_reformer_missed_opportunity_to_skill_propose() -> None:
    p = _make_pattern(kind="missed_opportunity", confidence=0.5,
                      suggestion="add a screenshot tool")
    rp = Reformer().propose(p)
    assert rp.kind == "skill_propose"
    assert rp.payload["draft_intent"] == "add a screenshot tool"


def test_reformer_user_pushback_to_preference_update() -> None:
    p = _make_pattern(kind="user_pushback_pattern", confidence=0.5,
                      suggestion="user prefers concise replies")
    rp = Reformer().propose(p)
    assert rp.kind == "preference_update"
    assert rp.payload["section"] == "USER.md"
    assert rp.payload["fact"] == "user prefers concise replies"


def test_reformer_answer_style_mismatch_to_preference_update() -> None:
    p = _make_pattern(kind="answer_style_mismatch", confidence=0.5)
    rp = Reformer().propose(p)
    assert rp.kind == "preference_update"


def test_reformer_low_confidence_returns_no_op() -> None:
    p = _make_pattern(confidence=0.1)
    rp = Reformer(min_confidence=0.3).propose(p)
    assert rp.kind == "no_op"
    assert "confidence" in rp.payload["reason"]


def test_reformer_unknown_kind_returns_no_op() -> None:
    p = _make_pattern(kind="totally_made_up", confidence=0.5)
    rp = Reformer().propose(p)
    assert rp.kind == "no_op"
    assert "unrecognised" in rp.payload["reason"]


@pytest.mark.asyncio
async def test_reformer_emit_publishes_to_bus() -> None:
    @dataclass
    class _Bus:
        published: list = field(default_factory=list)

        async def publish(self, ev):
            self.published.append(ev)

    bus = _Bus()
    rp = ReformProposal(
        kind="curriculum_edit", pattern_summary="x",
        payload={"addendum": "y"}, confidence=0.5, why="test",
    )
    await Reformer.emit(rp, bus=bus)
    assert len(bus.published) == 1
    ev = bus.published[0]
    assert (
        ev.type.value if hasattr(ev.type, "value") else ev.type
    ) == "metacognition_proposal"


@pytest.mark.asyncio
async def test_reformer_emit_skips_no_op() -> None:
    @dataclass
    class _Bus:
        published: list = field(default_factory=list)

        async def publish(self, ev):
            self.published.append(ev)

    bus = _Bus()
    rp = ReformProposal(
        kind="no_op", pattern_summary="x",
        payload={"reason": "x"}, confidence=0.0, why="y",
    )
    await Reformer.emit(rp, bus=bus)
    assert bus.published == []


@pytest.mark.asyncio
async def test_reformer_emit_with_no_bus_is_noop() -> None:
    rp = ReformProposal(
        kind="curriculum_edit", pattern_summary="x",
        payload={"addendum": "y"}, confidence=0.5, why="t",
    )
    await Reformer.emit(rp, bus=None)  # must not raise


# ── ReflectionCycle.metacognize integration ────────────────────


@pytest.mark.asyncio
async def test_reflection_cycle_metacognize_runs_pass_and_emits() -> None:
    """End-to-end on the metacognize bucket: traces in → patterns
    out → proposals emitted on the bus."""
    from xmclaw.cognition.reflection_cycle import ReflectionCycle

    @dataclass
    class _Bus:
        published: list = field(default_factory=list)

        async def publish(self, ev):
            self.published.append(ev)

    rec = _StubRecorder(traces=[
        DecisionTrace(id="t1", kind="tool_choice",
                      chosen="apply_patch", outcome="error",
                      reason="stale text"),
        DecisionTrace(id="t2", kind="tool_choice",
                      chosen="apply_patch", outcome="error",
                      reason="stale text"),
        DecisionTrace(id="t3", kind="tool_choice",
                      chosen="apply_patch", outcome="error",
                      reason="stale text"),
    ])
    llm = _ScriptedLLM(next_content=json.dumps([{
        "kind": "repeated_failure",
        "summary": "apply_patch keeps failing on stale text",
        "evidence": ["t1", "t2", "t3"],
        "confidence": 0.5,
        "suggestion": "always re-read before patching",
        "recurrence": 3,
    }]))
    pass_ = MetaCognitionPass(llm=llm, recorder=rec)
    bus = _Bus()
    rc = ReflectionCycle(
        bus=bus,
        metacognition_pass=pass_,
        reformer=Reformer(),
    )

    result = await rc.metacognize(tick=1)
    assert result.ran is True
    assert result.summary["patterns_found"] == 1
    assert result.summary["proposals_emitted"] == 1
    assert result.summary["proposal_kinds"] == ["curriculum_edit"]
    # The proposal was published on the bus.
    types = [
        (e.type.value if hasattr(e.type, "value") else e.type)
        for e in bus.published
    ]
    assert types == ["metacognition_proposal"]


@pytest.mark.asyncio
async def test_reflection_cycle_metacognize_skips_when_no_pass() -> None:
    """Without metacognition_pass + reformer wired the bucket is a
    silent no-op (backward compat for daemons running R1 only)."""
    from xmclaw.cognition.reflection_cycle import ReflectionCycle

    rc = ReflectionCycle()  # no metacognition wires
    out = await rc.metacognize(tick=1)
    assert out.ran is False


@pytest.mark.asyncio
async def test_reflection_cycle_run_due_includes_metacognize_bucket() -> None:
    """``run_due`` should include the metacognize bucket on first
    tick when wired, since last_ran starts at -1 (always due)."""
    from xmclaw.cognition.reflection_cycle import ReflectionCycle

    rec = _StubRecorder(traces=[])  # empty: pass returns []
    pass_ = MetaCognitionPass(
        llm=_ScriptedLLM(next_content="[]"),
        recorder=rec,
    )
    rc = ReflectionCycle(
        metacognition_pass=pass_,
        reformer=Reformer(),
        # Disable other buckets so we only see metacognize fire.
        reflect_every_ticks=10**9,
        consolidate_every_ticks=10**9,
        groom_every_ticks=10**9,
        metacognize_every_ticks=1,
    )
    results = await rc.run_due(tick=1)
    # The cycle ran (= due) but found nothing (no traces). Still
    # appears in results so the daemon can count "I ran metacognize
    # but it was empty" for telemetry.
    assert len(results) == 1
    assert results[0].scope == "metacognize"
    assert results[0].summary["patterns_found"] == 0
    assert results[0].summary["proposals_emitted"] == 0
