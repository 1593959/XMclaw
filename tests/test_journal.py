"""Tests for the evolution journal (PR-E0-1/2).

The journal is the foundation of the self-evolution observability story:
every cycle must leave a complete, queryable record. Regressions here would
blind the meta-evaluation subsystem, so the tests lean heavily on end-to-end
round-tripping rather than mocking.
"""
from __future__ import annotations

import pytest

from xmclaw.evolution.journal import (
    CYCLE_PASSED,
    CYCLE_REJECTED,
    CYCLE_SKIPPED,
    EvolutionJournal,
    KIND_GENE,
    KIND_SKILL,
    STATUS_PROMOTED,
    STATUS_RETIRED,
    STATUS_SHADOW,
)
from xmclaw.memory.sqlite_store import SQLiteStore


@pytest.fixture
def journal(tmp_path):
    store = SQLiteStore(tmp_path / "test.db")
    yield EvolutionJournal(store, agent_id="test_agent")
    store.close()


@pytest.mark.asyncio
async def test_open_cycle_creates_pending_row(journal):
    cid = await journal.open_cycle(trigger="pattern_threshold")
    cycle = await journal.get_cycle(cid)
    assert cycle is not None
    assert cycle["agent_id"] == "test_agent"
    assert cycle["trigger"] == "pattern_threshold"
    assert cycle["verdict"] == "pending"
    assert cycle["inputs"] == {}
    assert cycle["artifacts"] == []
    assert cycle["ended_at"] is None


@pytest.mark.asyncio
async def test_full_cycle_lifecycle(journal):
    cid = await journal.open_cycle(trigger="conversation_end")
    await journal.record_inputs(cid, {
        "observations": [{"tool": "bash", "result": "ok"}],
        "reflection": {"summary": "bash works"},
    })
    await journal.record_decisions(cid, {
        "forge_skills": [{"name": "run_bash", "reason": "frequent"}],
    })
    await journal.record_artifact(
        cid, KIND_SKILL, "skill_abc123", status=STATUS_SHADOW,
    )
    await journal.close_cycle(cid, verdict=CYCLE_PASSED, metrics={"forged": 1})

    cycle = await journal.get_cycle(cid)
    assert cycle["verdict"] == "passed"
    assert cycle["inputs"]["reflection"]["summary"] == "bash works"
    assert cycle["decisions"]["forge_skills"][0]["name"] == "run_bash"
    assert cycle["artifacts"] == ["skill_abc123"]
    assert cycle["metrics"] == {"forged": 1}
    assert cycle["ended_at"] is not None


@pytest.mark.asyncio
async def test_reject_cycle_records_reason(journal):
    cid = await journal.open_cycle(trigger="manual")
    await journal.close_cycle(
        cid, verdict=CYCLE_REJECTED, reject_reason="validator_failed_syntax",
    )
    cycle = await journal.get_cycle(cid)
    assert cycle["verdict"] == "rejected"
    assert cycle["reject_reason"] == "validator_failed_syntax"


@pytest.mark.asyncio
async def test_skipped_cycle_is_a_first_class_record(journal):
    """Empty reflection inputs are a SIGNAL, not a bug: they must still be
    logged so meta-evaluation can see the agent ran but had nothing to learn."""
    cid = await journal.open_cycle(trigger="pattern_threshold")
    await journal.close_cycle(
        cid, verdict=CYCLE_SKIPPED, reject_reason="empty_signal",
    )
    cycles = await journal.list_cycles()
    assert len(cycles) == 1
    assert cycles[0]["verdict"] == "skipped"


@pytest.mark.asyncio
async def test_multiple_artifacts_are_appended(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "skill_a")
    await journal.record_artifact(cid, KIND_GENE, "gene_b")
    await journal.record_artifact(cid, KIND_SKILL, "skill_c")
    cycle = await journal.get_cycle(cid)
    assert cycle["artifacts"] == ["skill_a", "gene_b", "skill_c"]

    lineage = await journal.get_lineage(cid)
    kinds = {row["kind"] for row in lineage}
    assert kinds == {"gene", "skill"}


@pytest.mark.asyncio
async def test_artifact_status_transition(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "skill_x")
    await journal.update_artifact_status("skill_x", STATUS_PROMOTED)

    artifact = await journal.get_artifact("skill_x")
    assert artifact["status"] == "promoted"

    await journal.update_artifact_status("skill_x", STATUS_RETIRED)
    assert (await journal.get_artifact("skill_x"))["status"] == "retired"


@pytest.mark.asyncio
async def test_increment_metric(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "skill_m")
    await journal.increment_metric("skill_m", "matched_count", 1)
    await journal.increment_metric("skill_m", "matched_count", 2)
    await journal.increment_metric("skill_m", "helpful_count", 1)

    artifact = await journal.get_artifact("skill_m")
    assert artifact["matched_count"] == 3
    assert artifact["helpful_count"] == 1
    assert artifact["harmful_count"] == 0


@pytest.mark.asyncio
async def test_get_active_artifacts_filters_by_status(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "skill_shadow")
    await journal.record_artifact(cid, KIND_SKILL, "skill_promoted")
    await journal.record_artifact(cid, KIND_SKILL, "skill_retired")
    await journal.update_artifact_status("skill_promoted", STATUS_PROMOTED)
    await journal.update_artifact_status("skill_retired", STATUS_RETIRED)

    # Default: shadow + promoted are active; retired is excluded.
    active = await journal.get_active_artifacts(kind=KIND_SKILL)
    ids = {row["artifact_id"] for row in active}
    assert ids == {"skill_shadow", "skill_promoted"}

    # Promoted-only: the strict filter skill_matcher should use for auto-exec.
    promoted = await journal.get_active_artifacts(
        kind=KIND_SKILL, statuses=(STATUS_PROMOTED,),
    )
    assert {r["artifact_id"] for r in promoted} == {"skill_promoted"}


@pytest.mark.asyncio
async def test_list_cycles_is_newest_first(journal):
    ids = []
    for i in range(3):
        cid = await journal.open_cycle(trigger=f"t{i}")
        await journal.close_cycle(cid, verdict=CYCLE_PASSED)
        ids.append(cid)
    listed = [c["cycle_id"] for c in await journal.list_cycles()]
    assert listed[0] == ids[-1]
    assert len(listed) == 3


@pytest.mark.asyncio
async def test_invalid_kind_raises(journal):
    cid = await journal.open_cycle(trigger="x")
    with pytest.raises(ValueError):
        await journal.record_artifact(cid, "bogus", "foo")


@pytest.mark.asyncio
async def test_invalid_verdict_raises(journal):
    cid = await journal.open_cycle(trigger="x")
    with pytest.raises(ValueError):
        await journal.close_cycle(cid, verdict="maybe")


@pytest.mark.asyncio
async def test_invalid_status_raises(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "s1")
    with pytest.raises(ValueError):
        await journal.update_artifact_status("s1", "promoted?")


@pytest.mark.asyncio
async def test_invalid_metric_raises(journal):
    cid = await journal.open_cycle(trigger="x")
    await journal.record_artifact(cid, KIND_SKILL, "s1")
    with pytest.raises(ValueError):
        await journal.increment_metric("s1", "; DROP TABLE users; --")
