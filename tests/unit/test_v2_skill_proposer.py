"""SkillProposer — unit tests (Epic #24 Phase 3.1).

Locks the contract:

* Pattern detection: tools used in ≥ ``min_pattern_count`` distinct
  sessions surface; below threshold drops.
* Avg grader score is computed only across entries that had grader
  data (None when none did).
* Empty journal → no patterns → no extractor invocation.
* Extractor failures are isolated; ``propose()`` returns [].
* Bad extractor returns (non-list, wrong types) drop cleanly.
* Confidence floor drops low-confidence drafts.
* ``ProposedSkill`` rejects empty evidence (anti-req #12).
* Async extractor callables work.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.evolution import ProposedSkill, SkillProposer
from xmclaw.core.journal import (
    JournalEntry,
    JournalReader,
    JournalWriter,
    ToolCallSummary,
)
from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event


# ── ProposedSkill validation ────────────────────────────────────────


def test_proposed_skill_rejects_empty_evidence() -> None:
    """anti-req #12: a proposal with no evidence cannot exist."""
    with pytest.raises(ValueError, match="anti-req #12"):
        ProposedSkill(
            skill_id="x", title="X", description="", body="",
            triggers=(), confidence=0.9,
            evidence=(),  # empty → boom
            source_pattern="test",
        )


def test_proposed_skill_to_jsonable_round_trip() -> None:
    p = ProposedSkill(
        skill_id="demo.foo", title="Foo", description="d", body="b",
        triggers=("foo", "bar"), confidence=0.8,
        evidence=("sess-1", "sess-2"),
        source_pattern="tool 'foo' in 3 sessions",
    )
    data = p.to_jsonable()
    assert data["skill_id"] == "demo.foo"
    assert data["evidence"] == ["sess-1", "sess-2"]
    assert data["confidence"] == 0.8


# ── pattern detection ───────────────────────────────────────────────


def _entry(
    sid: str, *, tools: list[str], grader_avg: float | None = None,
) -> JournalEntry:
    return JournalEntry(
        session_id=sid, agent_id="agent",
        ts_start=0.0, ts_end=1.0, duration_s=1.0,
        turn_count=1,
        tool_calls=tuple(
            ToolCallSummary(name=t, ok=True) for t in tools
        ),
        grader_avg_score=grader_avg,
        grader_play_count=1 if grader_avg is not None else 0,
    )


def test_pattern_detection_threshold_filter(tmp_path: Path) -> None:
    """Tool 'rare' shows up in 2 sessions; tool 'common' in 4. With
    min_pattern_count=3, only 'common' qualifies."""
    reader = JournalReader(root=tmp_path)
    proposer = SkillProposer(reader, min_pattern_count=3)

    entries = [
        _entry("s1", tools=["common", "rare"]),
        _entry("s2", tools=["common", "rare"]),
        _entry("s3", tools=["common"]),
        _entry("s4", tools=["common"]),
    ]
    patterns = proposer.detect_patterns(entries)
    assert len(patterns) == 1
    assert patterns[0].tool_name == "common"
    assert patterns[0].occurrence_count == 4
    assert set(patterns[0].session_ids) == {"s1", "s2", "s3", "s4"}


def test_pattern_avg_grader_score(tmp_path: Path) -> None:
    """Avg grader score is computed only across entries with grader data."""
    reader = JournalReader(root=tmp_path)
    proposer = SkillProposer(reader, min_pattern_count=2)

    entries = [
        _entry("s1", tools=["a"], grader_avg=0.8),
        _entry("s2", tools=["a"], grader_avg=0.4),
        _entry("s3", tools=["a"], grader_avg=None),  # no grader → excluded
    ]
    patterns = proposer.detect_patterns(entries)
    assert len(patterns) == 1
    assert patterns[0].avg_grader_score == pytest.approx(0.6)


def test_pattern_avg_none_when_no_grader_data(tmp_path: Path) -> None:
    reader = JournalReader(root=tmp_path)
    proposer = SkillProposer(reader, min_pattern_count=2)

    entries = [
        _entry("s1", tools=["a"]),
        _entry("s2", tools=["a"]),
    ]
    [p] = proposer.detect_patterns(entries)
    assert p.avg_grader_score is None


def test_pattern_sorted_by_count_desc(tmp_path: Path) -> None:
    reader = JournalReader(root=tmp_path)
    proposer = SkillProposer(reader, min_pattern_count=2)

    entries = [
        _entry("s1", tools=["light", "heavy"]),
        _entry("s2", tools=["light", "heavy"]),
        _entry("s3", tools=["heavy"]),
        _entry("s4", tools=["heavy"]),
    ]
    patterns = proposer.detect_patterns(entries)
    assert [p.tool_name for p in patterns] == ["heavy", "light"]


# ── propose() integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_empty_journal_returns_empty(tmp_path: Path) -> None:
    reader = JournalReader(root=tmp_path)
    called = {"n": 0}

    def crashing(_p, _e):
        called["n"] += 1
        raise RuntimeError("should never be called")

    proposer = SkillProposer(reader, extractor_callable=crashing)
    result = await proposer.propose()
    assert result == []
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_propose_no_patterns_returns_empty(tmp_path: Path) -> None:
    """If no pattern crosses min_pattern_count, extractor isn't called."""
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        # 1 session with tool 'a' - below threshold of 3.
        await bus.publish(make_event(
            session_id="s1", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
        ))
        await bus.publish(make_event(
            session_id="s1", agent_id="agent",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={"name": "a", "ok": True},
        ))
        await bus.publish(make_event(
            session_id="s1", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
        ))
        await bus.drain()
    finally:
        await w.stop()

    reader = JournalReader(root=tmp_path)
    called = {"n": 0}

    def fake(_p, _e):
        called["n"] += 1
        return []

    proposer = SkillProposer(
        reader, extractor_callable=fake, min_pattern_count=3,
    )
    result = await proposer.propose()
    assert result == []
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_propose_calls_extractor_with_patterns(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        for sid in ("s1", "s2", "s3"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={"name": "shared", "ok": True},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
            ))
        await bus.drain()
    finally:
        await w.stop()

    captured = []

    def fake_extractor(patterns, entries):
        captured.append((patterns, entries))
        return [
            ProposedSkill(
                skill_id="shared.workflow",
                title="Shared Workflow",
                description="agent uses 'shared' a lot",
                body="step 1: do shared",
                triggers=("shared",),
                confidence=0.9,
                evidence=tuple(p.session_ids[0] for p in patterns),
                source_pattern=f"tool 'shared' in {patterns[0].occurrence_count} sessions",
            ),
        ]

    proposer = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=fake_extractor,
        min_pattern_count=3,
    )
    result = await proposer.propose()

    assert len(captured) == 1
    patterns, entries = captured[0]
    assert any(p.tool_name == "shared" for p in patterns)
    assert len(entries) == 3

    assert len(result) == 1
    assert result[0].skill_id == "shared.workflow"


@pytest.mark.asyncio
async def test_extractor_exception_isolated(tmp_path: Path) -> None:
    """Extractor crash → propose() returns [], not raises."""
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        for sid in ("s1", "s2", "s3"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={"name": "x", "ok": True},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
            ))
        await bus.drain()
    finally:
        await w.stop()

    def crashing(_p, _e):
        raise RuntimeError("boom")

    proposer = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=crashing,
        min_pattern_count=3,
    )
    result = await proposer.propose()
    assert result == []


@pytest.mark.asyncio
async def test_confidence_floor(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        for sid in ("s1", "s2", "s3"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={"name": "x", "ok": True},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
            ))
        await bus.drain()
    finally:
        await w.stop()

    def fake(_p, _e):
        return [
            ProposedSkill(
                skill_id="confident", title="C", description="",
                body="b", triggers=("c",), confidence=0.9,
                evidence=("s1",), source_pattern="p",
            ),
            ProposedSkill(
                skill_id="hesitant", title="H", description="",
                body="b", triggers=("h",), confidence=0.2,
                evidence=("s1",), source_pattern="p",
            ),
        ]

    proposer = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=fake, min_pattern_count=3,
        min_confidence=0.5,
    )
    result = await proposer.propose()
    ids = [r.skill_id for r in result]
    assert ids == ["confident"]


@pytest.mark.asyncio
async def test_async_extractor_supported(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        for sid in ("s1", "s2", "s3"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={"name": "x", "ok": True},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
            ))
        await bus.drain()
    finally:
        await w.stop()

    async def async_extractor(patterns, _entries):
        return [
            ProposedSkill(
                skill_id="async.skill", title="Async", description="",
                body="b", triggers=("a",), confidence=0.9,
                evidence=("s1",), source_pattern="from async",
            ),
        ]

    proposer = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=async_extractor, min_pattern_count=3,
    )
    result = await proposer.propose()
    assert len(result) == 1
    assert result[0].skill_id == "async.skill"


@pytest.mark.asyncio
async def test_bad_extractor_return_drops_cleanly(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    w = JournalWriter(bus, root=tmp_path)
    await w.start()
    try:
        for sid in ("s1", "s2", "s3"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "create"},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={"name": "x", "ok": True},
            ))
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.SESSION_LIFECYCLE, payload={"phase": "destroy"},
            ))
        await bus.drain()
    finally:
        await w.stop()

    proposer_str = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=lambda _p, _e: "not a list",  # type: ignore[arg-type]
        min_pattern_count=3,
    )
    assert await proposer_str.propose() == []

    # Mixed list with non-ProposedSkill entries also drops.
    proposer_mixed = SkillProposer(
        JournalReader(root=tmp_path),
        extractor_callable=lambda _p, _e: ["not a ProposedSkill", 42],
        min_pattern_count=3,
    )
    assert await proposer_mixed.propose() == []
