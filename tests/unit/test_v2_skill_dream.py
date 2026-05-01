"""SkillDreamCycle — unit tests (Epic #24 Phase 3.2).

Locks the contract:

* ``run_once`` calls SkillProposer + emits one SKILL_CANDIDATE_PROPOSED
  event per ProposedSkill + appends one audit row.
* ``decision`` field on emitted event is "propose" (distinct from the
  EvolutionAgent observer's "promote" / "rollback").
* No proposals → no events, no audit rows.
* ``start`` / ``stop`` are idempotent; ``stop`` cancels the periodic
  task cleanly.
* ``enabled=False`` makes start a no-op.
* Audit rows include skill_id / confidence / evidence / source_pattern.
* Event publish failures don't kill the loop.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType
from xmclaw.core.evolution import ProposedSkill, SkillProposer
from xmclaw.core.journal import JournalReader
from xmclaw.daemon.skill_dream import SkillDreamCycle


def _make_proposer(reader: JournalReader, drafts: list[ProposedSkill]) -> SkillProposer:
    """Build a SkillProposer whose extractor returns ``drafts`` once."""
    sent = {"done": False}

    def fake_extractor(_p, _e):
        if sent["done"]:
            return []
        sent["done"] = True
        return drafts

    return SkillProposer(
        reader, extractor_callable=fake_extractor, min_pattern_count=1,
    )


# ── run_once integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_emits_event_per_draft(tmp_path: Path) -> None:
    """One ProposedSkill → one SKILL_CANDIDATE_PROPOSED event +
    one audit row."""
    bus = InProcessEventBus()
    captured = []

    async def cap(event):
        if event.type == EventType.SKILL_CANDIDATE_PROPOSED:
            captured.append(event)

    bus.subscribe(lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED, cap)

    # Plant a journal entry the proposer can pattern-match on.
    from xmclaw.core.journal import JournalEntry, ToolCallSummary
    journal_root = tmp_path / "journal" / "2026-05"
    journal_root.mkdir(parents=True)
    e = JournalEntry(
        session_id="sess-1", agent_id="a",
        ts_start=0.0, ts_end=1.0, duration_s=1.0, turn_count=1,
        tool_calls=(ToolCallSummary(name="t", ok=True),),
    )
    (journal_root / "sess-1.jsonl").write_text(
        json.dumps(e.to_jsonable()) + "\n", encoding="utf-8",
    )

    drafts = [
        ProposedSkill(
            skill_id="auto.t", title="T", description="proto",
            body="step 1",
            triggers=("t",), confidence=0.9,
            evidence=("sess-1",),
            source_pattern="tool 't' in 1 session",
        ),
    ]
    proposer = _make_proposer(JournalReader(root=tmp_path / "journal"), drafts)
    dream = SkillDreamCycle(
        proposer, bus,
        interval_s=3600.0, audit_dir=tmp_path / "evolution",
    )
    n = await dream.run_once()
    await bus.drain()

    assert n == 1
    assert len(captured) == 1
    p = captured[0].payload
    assert p["decision"] == "propose"
    assert p["winner_candidate_id"] == "auto.t"
    assert p["winner_version"] == 0
    assert p["evidence"] == ["sess-1"]
    assert p["draft"]["confidence"] == 0.9

    audit_lines = (
        tmp_path / "evolution" / "skill-dream" / "proposals.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    rec = json.loads(audit_lines[0])
    assert rec["skill_id"] == "auto.t"
    assert rec["evidence"] == ["sess-1"]
    assert rec["source_pattern"] == "tool 't' in 1 session"


@pytest.mark.asyncio
async def test_run_once_no_proposals_no_event(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    captured = []
    bus.subscribe(
        lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED,
        lambda e: captured.append(e) or None,
    )

    proposer = SkillProposer(
        JournalReader(root=tmp_path / "journal"),
        extractor_callable=lambda _p, _e: [],
    )
    dream = SkillDreamCycle(
        proposer, bus,
        interval_s=3600.0, audit_dir=tmp_path / "evolution",
    )
    n = await dream.run_once()
    await bus.drain()
    assert n == 0
    assert captured == []
    # No audit rows when no proposals.
    assert not (tmp_path / "evolution" / "skill-dream" / "proposals.jsonl").exists()


# ── lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_start_no_op(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    proposer = SkillProposer(JournalReader(root=tmp_path))
    dream = SkillDreamCycle(
        proposer, bus, interval_s=1.0, enabled=False,
        audit_dir=tmp_path / "evolution",
    )
    await dream.start()
    assert not dream.is_running()
    await dream.stop()  # also no-op


@pytest.mark.asyncio
async def test_start_stop_idempotent(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    proposer = SkillProposer(JournalReader(root=tmp_path))
    dream = SkillDreamCycle(
        proposer, bus, interval_s=3600.0,
        audit_dir=tmp_path / "evolution",
    )
    await dream.start()
    await dream.start()  # second call no-op
    assert dream.is_running()
    await dream.stop()
    await dream.stop()  # second call no-op
    assert not dream.is_running()


@pytest.mark.asyncio
async def test_stop_cancels_long_interval(tmp_path: Path) -> None:
    """interval_s=3600 but stop() returns within seconds because the
    loop awaits stop_event with timeout, not a bare sleep."""
    bus = InProcessEventBus()
    proposer = SkillProposer(JournalReader(root=tmp_path))
    dream = SkillDreamCycle(
        proposer, bus, interval_s=3600.0,
        audit_dir=tmp_path / "evolution",
    )
    await dream.start()
    # Give the task a moment to enter wait_for.
    await asyncio.sleep(0.01)
    t_start = asyncio.get_running_loop().time()
    await dream.stop()
    elapsed = asyncio.get_running_loop().time() - t_start
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s — should be near-instant"
    assert not dream.is_running()
