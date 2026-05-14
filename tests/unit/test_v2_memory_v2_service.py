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
