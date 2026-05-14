"""Phase 1a — LanceDB backend parity tests.

These tests exercise the LanceDB-backed VectorBackend / GraphBackend
with the same scenarios as the InMemory test suite. Both backends
MUST yield equivalent observable behaviour.

Skipped automatically when ``lancedb`` isn't installed — CI's
minimal lane stays green even on the bare runtime.

Each test uses a fresh tmpdir for LanceDB storage so they're
independent. Closure of the connection is best-effort (LanceDB
async API doesn't have explicit close in 0.30; GC handles it).
"""
from __future__ import annotations

import pytest

# Skip entire module when lancedb isn't installed.
lancedb = pytest.importorskip("lancedb")

from xmclaw.memory.v2 import (
    Fact,
    Relation,
    RelationKind,
)
from xmclaw.memory.v2.backend_lancedb import (
    LanceDBGraphBackend,
    LanceDBVectorBackend,
)


# ── Helpers ───────────────────────────────────────────────────────


EMB_DIM = 4  # tiny dim for fast tests


def _mk_fact(
    text: str,
    *,
    kind: str = "preference",
    scope: str = "user",
    confidence: float = 0.8,
    embedding: tuple[float, ...] | None = None,
    layer: str = "working",
) -> Fact:
    fid = Fact.compute_id(kind=kind, scope=scope, text=text)
    return Fact(
        id=fid, kind=kind, scope=scope, text=text,
        confidence=confidence,
        embedding=embedding or (0.1, 0.2, 0.3, 0.4),
        layer=layer,
    )


def _mk_rel(src: str, dst: str, kind: str = "CAUSED_BY") -> Relation:
    rid = Relation.compute_id(
        source_fact_id=src, target_fact_id=dst, relation=kind,
    )
    return Relation(
        id=rid, source_fact_id=src, target_fact_id=dst,
        relation=kind, strength=0.8,
    )


# ── Vector backend ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lance_upsert_and_get(tmp_path) -> None:
    b = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    f = _mk_fact("陪玩店业务", kind="project", scope="project")
    await b.upsert([f])
    got = await b.get(f.id)
    assert got is not None
    assert got.text == "陪玩店业务"
    assert got.kind == "project"


@pytest.mark.asyncio
async def test_lance_upsert_idempotent_replace(tmp_path) -> None:
    """Same id ⇒ row replaced (no UNIQUE bug like vec0)."""
    b = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    f1 = _mk_fact("X", confidence=0.5)
    await b.upsert([f1])
    f2 = _mk_fact("X", confidence=0.9)
    f2.evidence_count = 5  # mutated stand-in for "merged"
    await b.upsert([f2])
    assert await b.count() == 1
    got = await b.get(f1.id)
    assert got is not None
    assert got.confidence == pytest.approx(0.9)
    assert got.evidence_count == 5


@pytest.mark.asyncio
async def test_lance_search_vector(tmp_path) -> None:
    b = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    a = _mk_fact("aaa", embedding=(1.0, 0.0, 0.0, 0.0))
    c = _mk_fact("ccc", embedding=(0.0, 0.0, 0.0, 1.0))
    await b.upsert([a, c])
    hits = await b.search([1.0, 0.0, 0.0, 0.0], limit=2)
    assert len(hits) == 2
    # First should be 'aaa' (cosine-equivalent to query).
    assert hits[0].text == "aaa"


@pytest.mark.asyncio
async def test_lance_search_with_where(tmp_path) -> None:
    b = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    await b.upsert([
        _mk_fact("X", kind="preference"),
        _mk_fact("Y", kind="project"),
    ])
    hits = await b.search(where="kind = 'preference'")
    texts = [h.text for h in hits]
    assert texts == ["X"]


@pytest.mark.asyncio
async def test_lance_delete(tmp_path) -> None:
    b = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    f = _mk_fact("disposable")
    await b.upsert([f])
    assert await b.count() == 1
    n = await b.delete(f"id = '{f.id}'")
    assert n == 1
    assert await b.count() == 0


# ── Graph backend ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lance_graph_neighbors(tmp_path) -> None:
    g = LanceDBGraphBackend(str(tmp_path))
    await g.add_relations([
        _mk_rel("a", "b"),
        _mk_rel("a", "c"),
    ])
    n = await g.neighbors("a")
    targets = sorted(t for _, t in n)
    assert targets == ["b", "c"]


@pytest.mark.asyncio
async def test_lance_graph_contradictions(tmp_path) -> None:
    g = LanceDBGraphBackend(str(tmp_path))
    await g.add_relations([
        _mk_rel("fact-1", "fact-9", "CONTRADICTS"),
        _mk_rel("fact-1", "fact-2", "PART_OF"),
    ])
    c = await g.contradictions_of("fact-1")
    assert c == ["fact-9"]


@pytest.mark.asyncio
async def test_lance_graph_remove(tmp_path) -> None:
    g = LanceDBGraphBackend(str(tmp_path))
    r = _mk_rel("a", "b")
    await g.add_relation(r)
    await g.remove_relation(r.id)
    n = await g.neighbors("a")
    assert n == []


@pytest.mark.asyncio
async def test_lance_graph_2hop(tmp_path) -> None:
    g = LanceDBGraphBackend(str(tmp_path))
    await g.add_relations([
        _mk_rel("a", "b"),
        _mk_rel("b", "c"),
        _mk_rel("c", "d"),
    ])
    n = await g.neighbors("a", max_hops=2)
    targets = sorted(t for _, t in n)
    assert targets == ["b", "c"]


# ── Co-existence — both backends on same DB dir ───────────────────


@pytest.mark.asyncio
async def test_vec_and_graph_coexist_in_same_dir(tmp_path) -> None:
    """One LanceDB dir hosts both 'facts' and 'relations' tables."""
    vb = LanceDBVectorBackend(str(tmp_path), embedding_dim=EMB_DIM)
    gb = LanceDBGraphBackend(str(tmp_path))
    f = _mk_fact("foo", kind="project", scope="project")
    await vb.upsert([f])
    r = _mk_rel(f.id, "event:ev-001", "CAUSED_BY")
    await gb.add_relation(r)
    assert await vb.count() == 1
    n = await gb.neighbors(f.id)
    assert len(n) == 1
    assert n[0][1] == "event:ev-001"
