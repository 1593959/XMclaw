"""Phase E8 regression tests: retrospective query surface over the journal.

Pins:

* **PR-E8-1** — ``SQLiteStore.journal_list_cycles_since``,
  ``lineage_all``, and ``lineage_by_status`` return the expected rows
  with correct filtering and ordering.
* **PR-E8-2** — ``EvolutionJournal.cycle_summary``,
  ``artifact_funnel``, ``reject_reason_histogram``, and
  ``rollback_history`` aggregate the underlying rows into the shapes
  the dashboard consumes. All are pure reads — no event emission, no
  state mutation.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.evolution.journal import (
    CYCLE_PASSED,
    CYCLE_REJECTED,
    CYCLE_SKIPPED,
    EvolutionJournal,
    KIND_GENE,
    KIND_SKILL,
    STATUS_NEEDS_APPROVAL,
    STATUS_PROMOTED,
    STATUS_RETIRED,
    STATUS_ROLLED_BACK,
    STATUS_SHADOW,
)
from xmclaw.memory.sqlite_store import SQLiteStore


# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_journal(tmp_path, agent_id: str = "agent_e8") -> EvolutionJournal:
    db = tmp_path / "mem.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db)
    return EvolutionJournal(store, agent_id=agent_id)


async def _seed_cycles(journal: EvolutionJournal) -> dict[str, str]:
    """Seed a known mix of cycles; return a dict of name→cycle_id."""
    ids: dict[str, str] = {}
    ids["pass_manual"] = await journal.open_cycle(trigger="manual")
    await journal.close_cycle(ids["pass_manual"], verdict=CYCLE_PASSED)

    ids["reject_manual"] = await journal.open_cycle(trigger="manual")
    await journal.close_cycle(
        ids["reject_manual"], verdict=CYCLE_REJECTED,
        reject_reason="all_candidates_failed",
    )

    ids["skip_pattern"] = await journal.open_cycle(trigger="pattern_threshold")
    await journal.close_cycle(
        ids["skip_pattern"], verdict=CYCLE_SKIPPED,
        reject_reason="no_insights",
    )

    ids["reject_pattern"] = await journal.open_cycle(trigger="pattern_threshold")
    await journal.close_cycle(
        ids["reject_pattern"], verdict=CYCLE_REJECTED,
        reject_reason="all_candidates_failed",
    )
    return ids


# ── PR-E8-1: SQLiteStore helpers ────────────────────────────────────────────

def test_journal_list_cycles_since_respects_window(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        await _seed_cycles(journal)
        # A generous window must include everything we just wrote.
        rows = journal._store.journal_list_cycles_since(
            "agent_e8", window_seconds=3600, limit=100,
        )
        assert len(rows) == 4
        # A zero-second window should return nothing — the rows we just
        # inserted have started_at at "now" but strictly before the
        # comparator's later "now".
        rows_zero = journal._store.journal_list_cycles_since(
            "agent_e8", window_seconds=0, limit=100,
        )
        assert rows_zero == []

    asyncio.run(run())


def test_lineage_all_returns_every_status(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        cid = await journal.open_cycle(trigger="manual")
        await journal.record_artifact(cid, KIND_SKILL, "s_promoted",
                                      status=STATUS_PROMOTED)
        await journal.record_artifact(cid, KIND_SKILL, "s_rolled_back",
                                      status=STATUS_ROLLED_BACK)
        await journal.record_artifact(cid, KIND_GENE, "g_retired",
                                      status=STATUS_RETIRED)

        all_rows = journal._store.lineage_all("agent_e8")
        ids = {r["artifact_id"] for r in all_rows}
        assert ids == {"s_promoted", "s_rolled_back", "g_retired"}

        # kind filter must narrow to one row.
        skills_only = journal._store.lineage_all("agent_e8", kind=KIND_SKILL)
        assert {r["artifact_id"] for r in skills_only} == {
            "s_promoted", "s_rolled_back",
        }

    asyncio.run(run())


def test_lineage_by_status_filters_and_orders(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        cid = await journal.open_cycle(trigger="manual")
        await journal.record_artifact(cid, KIND_SKILL, "a",
                                      status=STATUS_PROMOTED)
        await journal.record_artifact(cid, KIND_SKILL, "b",
                                      status=STATUS_ROLLED_BACK)
        await journal.record_artifact(cid, KIND_SKILL, "c",
                                      status=STATUS_ROLLED_BACK)

        rolled = journal._store.lineage_by_status(
            "agent_e8", status=STATUS_ROLLED_BACK,
        )
        assert {r["artifact_id"] for r in rolled} == {"b", "c"}
        # Newest first — c was inserted last, so it must appear first.
        assert rolled[0]["artifact_id"] == "c"

    asyncio.run(run())


# ── PR-E8-2: EvolutionJournal aggregate queries ────────────────────────────

def test_cycle_summary_groups_by_verdict_trigger_and_reason(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        await _seed_cycles(journal)
        summary = await journal.cycle_summary()
        assert summary["total"] == 4
        assert summary["by_verdict"] == {
            CYCLE_PASSED: 1,
            CYCLE_REJECTED: 2,
            CYCLE_SKIPPED: 1,
        }
        assert summary["by_trigger"] == {
            "manual": 2, "pattern_threshold": 2,
        }
        # Two cycles carry reject_reason "all_candidates_failed"; one
        # carries "no_insights". The passed cycle has no reject_reason
        # and must NOT appear in the histogram.
        assert summary["by_reject_reason"] == {
            "all_candidates_failed": 2,
            "no_insights": 1,
        }

    asyncio.run(run())


def test_cycle_summary_respects_window(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        await _seed_cycles(journal)
        # Generous window → everything.
        full = await journal.cycle_summary(window_seconds=3600)
        assert full["total"] == 4
        # Zero-second window → nothing.
        empty = await journal.cycle_summary(window_seconds=0)
        assert empty["total"] == 0
        assert empty["by_verdict"] == {}
        assert empty["window_seconds"] == 0

    asyncio.run(run())


def test_artifact_funnel_counts_every_status(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        cid = await journal.open_cycle(trigger="manual")
        await journal.record_artifact(cid, KIND_SKILL, "s_a",
                                      status=STATUS_PROMOTED)
        await journal.record_artifact(cid, KIND_SKILL, "s_b",
                                      status=STATUS_NEEDS_APPROVAL)
        await journal.record_artifact(cid, KIND_GENE, "g_a",
                                      status=STATUS_SHADOW)
        await journal.record_artifact(cid, KIND_GENE, "g_b",
                                      status=STATUS_ROLLED_BACK)
        await journal.record_artifact(cid, KIND_GENE, "g_c",
                                      status=STATUS_RETIRED)

        funnel = await journal.artifact_funnel()
        assert funnel["total"] == 5
        assert funnel["by_status"] == {
            STATUS_PROMOTED: 1,
            STATUS_NEEDS_APPROVAL: 1,
            STATUS_SHADOW: 1,
            STATUS_ROLLED_BACK: 1,
            STATUS_RETIRED: 1,
        }
        assert funnel["by_kind"] == {KIND_SKILL: 2, KIND_GENE: 3}

        # Kind filter narrows counts but preserves the status groupings.
        gene_only = await journal.artifact_funnel(kind=KIND_GENE)
        assert gene_only["total"] == 3
        assert gene_only["by_kind"] == {KIND_GENE: 3}
        assert gene_only["kind_filter"] == KIND_GENE

    asyncio.run(run())


def test_reject_reason_histogram_ranks_by_count(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        # Seed an uneven distribution so ranking matters.
        for _ in range(3):
            cid = await journal.open_cycle(trigger="manual")
            await journal.close_cycle(cid, verdict=CYCLE_REJECTED,
                                      reject_reason="all_candidates_failed")
        cid = await journal.open_cycle(trigger="manual")
        await journal.close_cycle(cid, verdict=CYCLE_SKIPPED,
                                  reject_reason="no_insights")

        hist = await journal.reject_reason_histogram()
        assert hist[0] == {"reason": "all_candidates_failed", "count": 3}
        assert hist[1] == {"reason": "no_insights", "count": 1}

        # limit=1 must cut to just the top-ranked reason.
        top = await journal.reject_reason_histogram(limit=1)
        assert len(top) == 1
        assert top[0]["reason"] == "all_candidates_failed"

    asyncio.run(run())


def test_rollback_history_returns_rolled_back_only(tmp_path):
    journal = _make_journal(tmp_path)

    async def run():
        cid = await journal.open_cycle(trigger="manual")
        await journal.record_artifact(cid, KIND_SKILL, "alive",
                                      status=STATUS_PROMOTED)
        await journal.record_artifact(cid, KIND_SKILL, "rolled_1",
                                      status=STATUS_ROLLED_BACK)
        await journal.record_artifact(cid, KIND_GENE, "rolled_2",
                                      status=STATUS_ROLLED_BACK)

        rows = await journal.rollback_history()
        ids = {r["artifact_id"] for r in rows}
        assert ids == {"rolled_1", "rolled_2"}
        assert "alive" not in ids
        # rolled_2 is a gene — ensure kind survives through the query.
        kinds = {r["artifact_id"]: r["kind"] for r in rows}
        assert kinds["rolled_2"] == KIND_GENE

    asyncio.run(run())


def test_queries_scope_to_agent_id(tmp_path):
    """Retrospective queries MUST filter by agent_id. A dashboard serving
    agent_A should never see agent_B's cycles or lineage rows."""
    store = SQLiteStore(tmp_path / "mem.db")
    j_a = EvolutionJournal(store, agent_id="agent_A")
    j_b = EvolutionJournal(store, agent_id="agent_B")

    async def run():
        cid_a = await j_a.open_cycle(trigger="manual")
        await j_a.close_cycle(cid_a, verdict=CYCLE_PASSED)
        await j_a.record_artifact(cid_a, KIND_SKILL, "only_in_A",
                                  status=STATUS_PROMOTED)

        cid_b = await j_b.open_cycle(trigger="manual")
        await j_b.close_cycle(cid_b, verdict=CYCLE_REJECTED,
                              reject_reason="all_candidates_failed")
        await j_b.record_artifact(cid_b, KIND_SKILL, "only_in_B",
                                  status=STATUS_ROLLED_BACK)

        a_summary = await j_a.cycle_summary()
        assert a_summary["total"] == 1
        assert a_summary["by_verdict"] == {CYCLE_PASSED: 1}

        a_funnel = await j_a.artifact_funnel()
        assert a_funnel["total"] == 1
        assert {r["artifact_id"] for r in await j_a.rollback_history()} == set()

        b_rollbacks = await j_b.rollback_history()
        assert [r["artifact_id"] for r in b_rollbacks] == ["only_in_B"]

    asyncio.run(run())
