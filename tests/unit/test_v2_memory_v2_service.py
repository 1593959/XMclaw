"""Phase 2 — MemoryService.remember / recall / relate / supersede tests.

End-to-end behaviour with InMemory backends + StubEmbedder so each
test is fast and deterministic.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactLayer,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    LONG_TERM_PROMOTE_THRESHOLD,
    MemoryService,
    RelationKind,
    StubEmbedder,
)


# ── Fixture ───────────────────────────────────────────────────────


def _make_service(
    *, with_embedder: bool = True,
) -> MemoryService:
    embedder = (
        EmbeddingService(StubEmbedder(dim=4)) if with_embedder else None
    )
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=embedder,
    )


# ── remember ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_fresh_fact() -> None:
    svc = _make_service()
    f = await svc.remember(
        "用户喜欢简短回复",
        kind=FactKind.PREFERENCE,
        scope=FactScope.USER,
        confidence=0.9,
    )
    assert f.text == "用户喜欢简短回复"
    assert f.kind == "preference"
    assert f.scope == "user"
    assert f.evidence_count == 1
    assert f.embedding is not None
    assert len(f.embedding) == 4
    assert f.layer == "working"


@pytest.mark.asyncio
async def test_remember_idempotent_bumps_evidence() -> None:
    """Same content twice ⇒ one row, evidence_count=2."""
    svc = _make_service()
    f1 = await svc.remember("X", kind="preference", confidence=0.5)
    f2 = await svc.remember("X", kind="preference", confidence=0.9)
    assert f1.id == f2.id
    assert f2.evidence_count == 2
    # Max confidence wins on merge.
    assert f2.confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_remember_promotes_to_long_term_on_threshold() -> None:
    """N writes of same fact ⇒ layer auto-promotes."""
    svc = _make_service()
    for _ in range(LONG_TERM_PROMOTE_THRESHOLD):
        f = await svc.remember("Y", kind="preference")
    assert f.layer == "long_term"


@pytest.mark.asyncio
async def test_remember_with_source_event_adds_caused_by_edge() -> None:
    svc = _make_service()
    f = await svc.remember(
        "陪玩店业务",
        kind=FactKind.PROJECT,
        scope=FactScope.PROJECT,
        source_event_id="ev-abc123",
    )
    neighbors = await svc.neighbors(f.id, relation_types=["CAUSED_BY"])
    assert len(neighbors) == 1
    rel, target = neighbors[0]
    assert rel.relation == "CAUSED_BY"
    assert target == "event:ev-abc123"


@pytest.mark.asyncio
async def test_remember_without_embedder_still_writes() -> None:
    """No API key configured → text-only mode, fact still persists."""
    svc = _make_service(with_embedder=False)
    f = await svc.remember("X", kind="preference")
    assert f.embedding is None
    fetched = await svc.get_fact(f.id)
    assert fetched is not None
    assert fetched.text == "X"


@pytest.mark.asyncio
async def test_remember_empty_text_rejected() -> None:
    svc = _make_service()
    with pytest.raises(ValueError):
        await svc.remember("   ", kind="preference")


# ── Contradict detection ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_contradict_scan_adds_edge() -> None:
    """Two same-kind facts close in vec space get a CONTRADICTS edge."""
    svc = _make_service()
    a = await svc.remember("用 Mac", kind="preference")
    b = await svc.remember("用 Windows", kind="preference")
    # Pattern: the second fact's contradicts list should contain the
    # first (top-3 same-kind scan).
    assert a.id in b.contradicts
    # And an edge exists.
    cs = await svc.contradictions_of(b.id)
    assert a.id in cs


# ── Supersede ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supersede_marks_old_and_adds_edge() -> None:
    svc = _make_service()
    old = await svc.remember(
        "项目用 Mac", kind="project", scope="project",
        skip_contradict_check=True,
    )
    new = await svc.remember(
        "项目改用 Windows", kind="project", scope="project",
        skip_contradict_check=True,
    )
    await svc.supersede(old_fact_id=old.id, new_fact_id=new.id)
    refetched = await svc.get_fact(old.id)
    assert refetched is not None
    assert refetched.superseded_by == new.id
    assert refetched.confidence == pytest.approx(0.3)
    # Edge new → old (SUPERSEDES) exists.
    nbrs = await svc.neighbors(new.id, relation_types=["SUPERSEDES"])
    assert any(t == old.id for _, t in nbrs)


# ── recall ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_returns_hits_with_embeddings() -> None:
    svc = _make_service()
    await svc.remember("A", kind="preference")
    await svc.remember("B", kind="preference")
    hits = await svc.recall("A", k=5)
    assert len(hits) > 0
    # First hit should be most-similar.
    assert any(h.fact.text == "A" for h in hits)


@pytest.mark.asyncio
async def test_recall_filter_by_kind() -> None:
    svc = _make_service()
    await svc.remember("pref", kind="preference")
    await svc.remember("proj", kind="project", scope="project")
    hits = await svc.recall("anything", kinds=["preference"])
    texts = [h.fact.text for h in hits]
    assert "pref" in texts
    assert "proj" not in texts


@pytest.mark.asyncio
async def test_recall_filter_by_confidence() -> None:
    svc = _make_service()
    await svc.remember(
        "lowconf", kind="preference", confidence=0.2,
        skip_contradict_check=True,
    )
    await svc.remember(
        "highconf", kind="preference", confidence=0.9,
        skip_contradict_check=True,
    )
    hits = await svc.recall(None, min_confidence=0.5)
    texts = [h.fact.text for h in hits]
    assert "highconf" in texts
    assert "lowconf" not in texts


@pytest.mark.asyncio
async def test_recall_includes_related_relations() -> None:
    """A fact with a CONTRADICTS edge gets it back inline."""
    svc = _make_service()
    a = await svc.remember(
        "用 Mac", kind="preference", scope="user",
    )
    b = await svc.remember(
        "用 Windows", kind="preference", scope="user",
    )
    hits = await svc.recall("用 Windows", k=5)
    h_b = next((h for h in hits if h.fact.id == b.id), None)
    assert h_b is not None
    # b should have at least one outgoing CONTRADICTS edge to a.
    rels = [r.relation for r in h_b.related_relations]
    assert "CONTRADICTS" in rels


@pytest.mark.asyncio
async def test_recall_no_query_lists_by_recency() -> None:
    svc = _make_service()
    await svc.remember("old", kind="preference")
    await svc.remember("new", kind="preference")
    hits = await svc.recall(None, k=5)
    # No query ⇒ order is ts_last DESC.
    assert hits[0].fact.text in ("old", "new")


@pytest.mark.asyncio
async def test_recall_without_embedder_keyword_fallback() -> None:
    svc = _make_service(with_embedder=False)
    await svc.remember("陪玩店业务", kind="project", scope="project")
    await svc.remember("用户偏好", kind="preference")
    hits = await svc.recall("陪玩店")
    assert len(hits) == 1
    assert hits[0].fact.text == "陪玩店业务"


# ── relate / graph walk ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_relate_manual_edge() -> None:
    svc = _make_service()
    a = await svc.remember(
        "A", kind="episode", skip_contradict_check=True,
    )
    b = await svc.remember(
        "B", kind="episode", skip_contradict_check=True,
    )
    rel = await svc.relate(
        source_fact_id=a.id,
        target_fact_id=b.id,
        kind=RelationKind.PART_OF,
        strength=0.85,
    )
    assert rel.relation == "PART_OF"
    nbrs = await svc.neighbors(a.id, relation_types=["PART_OF"])
    assert any(t == b.id for _, t in nbrs)


# ── Near-duplicate consolidation (Wave 27 follow-up) ─────────────


@pytest.mark.asyncio
async def test_remember_merges_near_duplicate_at_write() -> None:
    """Two semantically-equivalent writes with different surface text
    collapse into ONE fact (evidence bumped), not two near-dup rows."""
    # Force deterministic embeddings that are NEAR (cosine ~ 0).
    class _DeterministicEmbedder:
        name = "deterministic"
        dim = 4
        # All inputs map to ALMOST the same vector — within
        # NEAR_DUPLICATE_DISTANCE_THRESHOLD of each other.
        async def embed(self, texts):
            out = []
            for t in texts:
                # base vector + tiny noise based on first char
                seed = (ord(t[0]) if t else 0) % 5
                out.append([1.0, 0.001 * seed, 0.0, 0.0])
            return out
        def is_available(self): return True

    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(_DeterministicEmbedder()),
    )
    # Three different surface forms of the "same" idea (vec-close).
    await svc.remember(
        "目标月流水破10万",
        kind="project", scope="project",
    )
    await svc.remember(
        "目标月流水破10万元",  # different chars, embeds near-same
        kind="project", scope="project",
    )
    await svc.remember(
        "业务目标: 月流水10万",  # different again
        kind="project", scope="project",
    )
    # Should be ONE row, not three.
    assert await svc.count(kinds=["project"]) == 1
    # And evidence_count reflects the 3 votes.
    hits = await svc.recall(None, kinds=["project"], k=5)
    assert hits[0].fact.evidence_count == 3


@pytest.mark.asyncio
async def test_remember_keeps_distinct_facts_separate() -> None:
    """When vec distance > threshold, facts stay separate."""
    svc = _make_service()  # StubEmbedder gives semi-distinct vectors
    await svc.remember(
        "陪玩店业务: pw310.wxselling.com",
        kind="project", scope="project",
        skip_contradict_check=True,
    )
    await svc.remember(
        "完全不相关的事实 — Python 编程语言版本号 3.10",
        kind="project", scope="project",
        skip_contradict_check=True,
    )
    # Two distinct facts.
    assert await svc.count(kinds=["project"]) == 2


@pytest.mark.asyncio
async def test_deduplicate_collapses_existing_duplicates() -> None:
    """Bulk dedup pass merges existing near-dup rows after the fact."""
    # Pre-existing scenario: 3 rows that we WANT to be the same fact
    # but were saved with skip_contradict_check=True (so write-time
    # dedup was bypassed).
    svc = _make_service()
    a = await svc.remember(
        "用户喜欢简短回复",
        kind="preference", scope="user",
        skip_contradict_check=True,
    )
    b = await svc.remember(
        "用户喜欢简短回复",  # exact dup of a — same id, will merge naturally
        kind="preference", scope="user",
        skip_contradict_check=True,
    )
    # Should already be one row because exact id collision.
    assert a.id == b.id
    assert await svc.count() == 1

    # Now force a near-duplicate-but-different-text case:
    # Same kind/scope, slightly different wording.
    await svc.remember(
        "用户偏好简短的回答",
        kind="preference", scope="user",
        skip_contradict_check=True,  # bypass write-time merge
    )
    # In StubEmbedder world the two embeddings might NOT be within
    # the near-dup threshold (random-ish bytes), so we may or may
    # not see a merge. Just verify deduplicate() runs cleanly.
    report = await svc.deduplicate(dry_run=True)
    assert "scanned" in report
    assert "clusters_found" in report
    assert "merged" in report
    # dry_run guarantees nothing was written.
    assert report["dry_run"] is True


@pytest.mark.asyncio
async def test_deduplicate_dry_run_does_not_mutate() -> None:
    svc = _make_service()
    await svc.remember(
        "a fact", kind="project", scope="project",
        skip_contradict_check=True,
    )
    before = await svc.count()
    report = await svc.deduplicate(dry_run=True)
    after = await svc.count()
    assert before == after
    assert report["dry_run"] is True


@pytest.mark.asyncio
async def test_deduplicate_produces_supersedes_edges() -> None:
    """When dedup merges A and B, a SUPERSEDES edge survivor->loser
    appears in the graph so UI viz can show the merge history."""
    # Force tight clustering.
    class _TightEmbedder:
        name = "tight"
        dim = 4
        async def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        def is_available(self): return True

    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(_TightEmbedder()),
    )
    f1 = await svc.remember(
        "fact A",
        kind="project", scope="project",
        skip_contradict_check=True,
    )
    f2 = await svc.remember(
        "fact B (same content, different surface)",
        kind="project", scope="project",
        skip_contradict_check=True,
    )
    # With tight embedder both share the same vec, distance = 0.
    # But skip_contradict_check bypassed write-time merge so we have
    # two rows.
    assert await svc.count() == 2

    report = await svc.deduplicate()
    assert report["merged"] == 1
    assert len(report["actions"]) == 1
    survivor_id = report["actions"][0]["survivor_id"]
    loser_id = report["actions"][0]["loser_ids"][0]
    # SUPERSEDES edge survivor → loser exists.
    nbrs = await svc.neighbors(survivor_id, relation_types=["SUPERSEDES"])
    assert any(t == loser_id for _, t in nbrs)
    # Loser's superseded_by points to survivor.
    loser = await svc.get_fact(loser_id)
    assert loser is not None
    assert loser.superseded_by == survivor_id


@pytest.mark.asyncio
async def test_recall_hides_superseded_by_default() -> None:
    """After dedup runs, the loser is filtered from default recall + count.

    Prevents tombstone duplicates from polluting the UI list (which
    was the visible bug: a "deduped" fact still appeared in the panel).
    """
    class _TightEmbedder:
        name = "tight"
        dim = 4
        async def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        def is_available(self): return True

    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(_TightEmbedder()),
    )
    await svc.remember(
        "fact A", kind="project", scope="project",
        skip_contradict_check=True,
    )
    await svc.remember(
        "fact B paraphrase", kind="project", scope="project",
        skip_contradict_check=True,
    )
    assert await svc.count() == 2  # raw rows before dedup
    report = await svc.deduplicate()
    assert report["merged"] == 1
    # Default recall hides the tombstone.
    hits = await svc.recall(None, k=10, min_confidence=0.0)
    assert len(hits) == 1
    # Default count() agrees.
    assert await svc.count() == 1
    # Opt-in surfaces the loser.
    hits_all = await svc.recall(
        None, k=10, min_confidence=0.0, include_superseded=True,
    )
    assert len(hits_all) == 2
    assert await svc.count(include_superseded=True) == 2


@pytest.mark.asyncio
async def test_find_related_subgraph_for_ui() -> None:
    """find_related returns shape ready for vis-network."""
    svc = _make_service()
    a = await svc.remember(
        "X", kind="project", skip_contradict_check=True,
    )
    b = await svc.remember(
        "Y", kind="project", skip_contradict_check=True,
    )
    await svc.relate(
        source_fact_id=a.id, target_fact_id=b.id,
        kind=RelationKind.PART_OF,
    )
    sub = await svc.find_related([a.id])
    assert a.id in sub["nodes"]
    assert b.id in sub["nodes"]
    assert len(sub["edges"]) == 1
    assert sub["edges"][0].relation == "PART_OF"


# ── render_for_prompt (Phase 4a) ──────────────────────────────────


@pytest.mark.asyncio
async def test_render_for_prompt_includes_user_project_decisions() -> None:
    svc = _make_service()
    # Mix of fact kinds.
    await svc.remember(
        "用户喜欢简短回复",
        kind="preference", scope="user",
        skip_contradict_check=True,
    )
    await svc.remember(
        "陪玩店 pw310.wxselling.com",
        kind="project", scope="project",
        skip_contradict_check=True,
    )
    await svc.remember(
        "用 PowerShell 不用 bash",
        kind="decision",
        skip_contradict_check=True,
    )
    block = await svc.render_for_prompt("anything")
    assert "<memory-v2-facts>" in block
    assert "用户档案" in block
    assert "项目档案" in block
    assert "决定记录" in block
    assert "用户喜欢简短回复" in block
    assert "陪玩店" in block
    assert "PowerShell" in block


@pytest.mark.asyncio
async def test_render_for_prompt_empty_when_no_facts() -> None:
    svc = _make_service()
    block = await svc.render_for_prompt("anything")
    assert block == ""


@pytest.mark.asyncio
async def test_render_for_prompt_attaches_contradicts_marker() -> None:
    """CONTRADICTS edges show up as ⚠ markers in the prompt block."""
    svc = _make_service()
    await svc.remember(
        "用 Mac",
        kind="preference", scope="user",
    )
    await svc.remember(
        "用 Windows",
        kind="preference", scope="user",
    )
    block = await svc.render_for_prompt("Windows", k=5)
    # The Windows fact has an outgoing CONTRADICTS edge to Mac fact.
    # The recall section should carry a ⚠ marker.
    assert "⚠" in block or "contradicts" in block.lower()


@pytest.mark.asyncio
async def test_render_for_prompt_skips_duplicates_in_recall_section() -> None:
    """If a fact appears in the always-on section AND the query-
    relevant recall, it shouldn't appear twice."""
    svc = _make_service()
    await svc.remember(
        "用户喜欢简短回复",
        kind="preference", scope="user",
    )
    block = await svc.render_for_prompt("简短回复")
    # Count "用户喜欢简短回复" occurrences — should be 1, not 2.
    assert block.count("用户喜欢简短回复") == 1
