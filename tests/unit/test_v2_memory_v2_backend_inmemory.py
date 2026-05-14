"""Phase 1a — InMemory backend behaviour tests.

These tests pin the OBSERVABLE contract that all Protocol
implementations must match. The LanceDB backend tests (separate
file) re-use the same scenarios to verify parity.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.memory.v2 import (
    Fact,
    FactKind,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    Relation,
    RelationKind,
    VectorBackend,
    GraphBackend,
)


# ── Helpers ───────────────────────────────────────────────────────


def _mk_fact(
    text: str,
    *,
    kind: str = "preference",
    scope: str = "user",
    confidence: float = 0.8,
    embedding: tuple[float, ...] | None = None,
    layer: str = "working",
    evidence_count: int = 1,
) -> Fact:
    fid = Fact.compute_id(kind=kind, scope=scope, text=text)
    return Fact(
        id=fid, kind=kind, scope=scope, text=text,
        confidence=confidence, embedding=embedding,
        layer=layer, evidence_count=evidence_count,
    )


# ── Protocol conformance ──────────────────────────────────────────


def test_inmemory_vector_satisfies_protocol() -> None:
    backend = InMemoryVectorBackend()
    assert isinstance(backend, VectorBackend)


def test_inmemory_graph_satisfies_protocol() -> None:
    backend = InMemoryGraphBackend()
    assert isinstance(backend, GraphBackend)


# ── Vector backend behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_inserts_new_fact() -> None:
    b = InMemoryVectorBackend()
    f = _mk_fact("用户喜欢简短回复")
    await b.upsert([f])
    assert await b.count() == 1
    got = await b.get(f.id)
    assert got is not None
    assert got.text == "用户喜欢简短回复"
    assert got.evidence_count == 1


@pytest.mark.asyncio
async def test_upsert_idempotent_bumps_evidence() -> None:
    """Same content twice ⇒ one row + evidence_count=2."""
    b = InMemoryVectorBackend()
    f = _mk_fact("X", confidence=0.5)
    await b.upsert([f])
    f2 = _mk_fact("X", confidence=0.9)
    await b.upsert([f2])
    assert await b.count() == 1
    got = await b.get(f.id)
    assert got is not None
    assert got.evidence_count == 2
    # Higher confidence wins.
    assert got.confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_search_vector_returns_nearest() -> None:
    b = InMemoryVectorBackend()
    a = _mk_fact("aaa", embedding=(1.0, 0.0, 0.0))
    bb = _mk_fact("bbb", embedding=(0.0, 1.0, 0.0))
    c = _mk_fact("ccc", embedding=(0.9, 0.1, 0.0))
    await b.upsert([a, bb, c])
    hits = await b.search([1.0, 0.0, 0.0], limit=2)
    assert len(hits) == 2
    # Closest to (1,0,0) is a, then c.
    assert hits[0].text == "aaa"
    assert hits[1].text == "ccc"


@pytest.mark.asyncio
async def test_search_with_where_filter() -> None:
    b = InMemoryVectorBackend()
    pref = _mk_fact("X", kind="preference", confidence=0.9)
    proj = _mk_fact("Y", kind="project", confidence=0.5)
    await b.upsert([pref, proj])
    hits = await b.search(where="kind = 'preference'")
    assert len(hits) == 1
    assert hits[0].text == "X"


@pytest.mark.asyncio
async def test_search_keyword_fallback() -> None:
    b = InMemoryVectorBackend()
    await b.upsert([
        _mk_fact("用户喜欢深色主题"),
        _mk_fact("陪玩店业务"),
    ])
    hits = await b.search("陪玩店")
    assert len(hits) == 1
    assert hits[0].text == "陪玩店业务"


@pytest.mark.asyncio
async def test_filter_compound_and_or() -> None:
    b = InMemoryVectorBackend()
    await b.upsert([
        _mk_fact("a", kind="preference", confidence=0.9),
        _mk_fact("b", kind="preference", confidence=0.4),
        _mk_fact("c", kind="project", confidence=0.95),
    ])
    hits = await b.search(
        where="kind = 'preference' AND confidence > 0.5",
    )
    texts = sorted(h.text for h in hits)
    assert texts == ["a"]
    hits2 = await b.search(
        where="confidence > 0.9 OR kind = 'project'",
    )
    texts2 = sorted(h.text for h in hits2)
    assert texts2 == ["c"]


@pytest.mark.asyncio
async def test_delete_by_where() -> None:
    b = InMemoryVectorBackend()
    await b.upsert([
        _mk_fact("a", kind="preference"),
        _mk_fact("b", kind="commitment"),
    ])
    n = await b.delete("kind = 'commitment'")
    assert n == 1
    assert await b.count() == 1


# ── Graph backend behaviour ───────────────────────────────────────


def _mk_rel(src: str, dst: str, kind: str = "CAUSED_BY") -> Relation:
    rid = Relation.compute_id(
        source_fact_id=src, target_fact_id=dst, relation=kind,
    )
    return Relation(
        id=rid, source_fact_id=src, target_fact_id=dst,
        relation=kind, strength=0.8,
    )


@pytest.mark.asyncio
async def test_graph_add_relation_idempotent() -> None:
    g = InMemoryGraphBackend()
    r = _mk_rel("a", "b")
    await g.add_relation(r)
    await g.add_relation(r)  # idempotent
    neighbors = await g.neighbors("a")
    assert len(neighbors) == 1


@pytest.mark.asyncio
async def test_graph_neighbors_1hop() -> None:
    g = InMemoryGraphBackend()
    await g.add_relations([
        _mk_rel("a", "b"),
        _mk_rel("a", "c"),
        _mk_rel("b", "d"),
    ])
    n = await g.neighbors("a")
    targets = sorted(t for _, t in n)
    assert targets == ["b", "c"]


@pytest.mark.asyncio
async def test_graph_neighbors_2hop() -> None:
    g = InMemoryGraphBackend()
    await g.add_relations([
        _mk_rel("a", "b"),
        _mk_rel("b", "c"),
        _mk_rel("c", "d"),
    ])
    n = await g.neighbors("a", max_hops=2)
    targets = sorted(t for _, t in n)
    # 1-hop: b. 2-hop: c (via b). 3-hop (d) not reached.
    assert targets == ["b", "c"]


@pytest.mark.asyncio
async def test_graph_neighbors_filter_by_type() -> None:
    g = InMemoryGraphBackend()
    await g.add_relations([
        _mk_rel("a", "b", "CAUSED_BY"),
        _mk_rel("a", "c", "CONTRADICTS"),
    ])
    n = await g.neighbors("a", relation_types=["CONTRADICTS"])
    assert len(n) == 1
    assert n[0][1] == "c"
    assert n[0][0].relation == "CONTRADICTS"


@pytest.mark.asyncio
async def test_graph_contradictions_of() -> None:
    g = InMemoryGraphBackend()
    await g.add_relations([
        _mk_rel("fact-1", "fact-9", "CONTRADICTS"),
        _mk_rel("fact-1", "fact-2", "PART_OF"),
    ])
    c = await g.contradictions_of("fact-1")
    assert c == ["fact-9"]


@pytest.mark.asyncio
async def test_graph_find_related_subgraph() -> None:
    g = InMemoryGraphBackend()
    await g.add_relations([
        _mk_rel("a", "b", "PART_OF"),
        _mk_rel("a", "c", "CONTRADICTS"),
        _mk_rel("b", "d", "REFERS_TO"),
    ])
    sub = await g.find_related(["a"], max_hops=2)
    assert sorted(sub["nodes"]) == ["a", "b", "c", "d"]
    assert len(sub["edges"]) == 3


@pytest.mark.asyncio
async def test_graph_remove_relation() -> None:
    g = InMemoryGraphBackend()
    r = _mk_rel("a", "b")
    await g.add_relation(r)
    await g.remove_relation(r.id)
    n = await g.neighbors("a")
    assert n == []
