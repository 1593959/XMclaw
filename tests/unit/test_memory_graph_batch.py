"""Graph batch-neighbor query tests.

Covers:
- neighbors_batch / reverse_neighbors_batch correctness
- Empty-list / missing-id safety
- Parity with single-query neighbors() / reverse_neighbors()
- recall() integration: mock-verified query count drops from 2×k to 2.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    RelationKind,
    StubEmbedder,
)
from xmclaw.memory.v2.models import Fact, Relation


# ── Fixtures ──


@pytest.fixture
def graph_backend():
    return InMemoryGraphBackend()


@pytest.fixture
def counting_backend(graph_backend):
    """Wrap InMemoryGraphBackend with call counters."""
    class _Counter:
        def __init__(self, inner):
            self._inner = inner
            self.neighbors_calls = 0
            self.reverse_neighbors_calls = 0
            self.neighbors_batch_calls = 0
            self.reverse_neighbors_batch_calls = 0

        async def neighbors(self, *args, **kwargs):
            self.neighbors_calls += 1
            return await self._inner.neighbors(*args, **kwargs)

        async def neighbors_batch(self, *args, **kwargs):
            self.neighbors_batch_calls += 1
            return await self._inner.neighbors_batch(*args, **kwargs)

        async def reverse_neighbors(self, *args, **kwargs):
            self.reverse_neighbors_calls += 1
            return await self._inner.reverse_neighbors(*args, **kwargs)

        async def reverse_neighbors_batch(self, *args, **kwargs):
            self.reverse_neighbors_batch_calls += 1
            return await self._inner.reverse_neighbors_batch(*args, **kwargs)

        async def add_relation(self, *args, **kwargs):
            return await self._inner.add_relation(*args, **kwargs)

        async def add_relations(self, *args, **kwargs):
            return await self._inner.add_relations(*args, **kwargs)

        async def remove_relation(self, *args, **kwargs):
            return await self._inner.remove_relation(*args, **kwargs)

        async def find_related(self, *args, **kwargs):
            return await self._inner.find_related(*args, **kwargs)

        async def contradictions_of(self, *args, **kwargs):
            return await self._inner.contradictions_of(*args, **kwargs)

        async def all_nodes(self, *args, **kwargs):
            return await self._inner.all_nodes(*args, **kwargs)

        async def close(self, *args, **kwargs):
            return await self._inner.close(*args, **kwargs)

    return _Counter(graph_backend)


def _make_service(graph_backend=None, *, with_embedder: bool = True):
    embedder = EmbeddingService(StubEmbedder(dim=4)) if with_embedder else None
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=graph_backend or InMemoryGraphBackend(),
        embedder=embedder,
    )


# ── Batch correctness ──


@pytest.mark.asyncio
async def test_neighbors_batch_grouping(graph_backend) -> None:
    """neighbors_batch([id1, id2]) returns correct per-source grouping."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="b",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="a", target_fact_id="c",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r3", source_fact_id="b", target_fact_id="d",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
    ])

    batch = await graph_backend.neighbors_batch(["a", "b"], max_hops=1)
    assert set(batch.keys()) == {"a", "b"}
    a_targets = {tgt for _rel, tgt in batch["a"]}
    b_targets = {tgt for _rel, tgt in batch["b"]}
    assert a_targets == {"b", "c"}
    assert b_targets == {"d"}


@pytest.mark.asyncio
async def test_reverse_neighbors_batch_grouping(graph_backend) -> None:
    """reverse_neighbors_batch([id1, id2]) returns correct per-target grouping."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="c",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="b", target_fact_id="c",
                 relation=RelationKind.CONTRADICTS.value, strength=0.85),
        Relation(id="r3", source_fact_id="c", target_fact_id="d",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
    ])

    batch = await graph_backend.reverse_neighbors_batch(["c", "d"], max_hops=1)
    assert set(batch.keys()) == {"c", "d"}
    c_sources = {src for _rel, src in batch["c"]}
    d_sources = {src for _rel, src in batch["d"]}
    assert c_sources == {"a", "b"}
    assert d_sources == {"c"}


@pytest.mark.asyncio
async def test_neighbors_batch_empty_list(graph_backend) -> None:
    """Empty list → empty dict, no error."""
    result = await graph_backend.neighbors_batch([])
    assert result == {}


@pytest.mark.asyncio
async def test_reverse_neighbors_batch_empty_list(graph_backend) -> None:
    """Empty list → empty dict, no error."""
    result = await graph_backend.reverse_neighbors_batch([])
    assert result == {}


@pytest.mark.asyncio
async def test_neighbors_batch_missing_ids(graph_backend) -> None:
    """Non-existent IDs return empty lists, no error."""
    result = await graph_backend.neighbors_batch(["ghost1", "ghost2"])
    assert result == {"ghost1": [], "ghost2": []}


@pytest.mark.asyncio
async def test_reverse_neighbors_batch_missing_ids(graph_backend) -> None:
    """Non-existent IDs return empty lists, no error."""
    result = await graph_backend.reverse_neighbors_batch(["ghost1", "ghost2"])
    assert result == {"ghost1": [], "ghost2": []}


# ── Parity with single-query methods ──


@pytest.mark.asyncio
async def test_neighbors_batch_matches_single_query(graph_backend) -> None:
    """neighbors_batch([id1]) produces identical output to neighbors(id1)."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="b",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="a", target_fact_id="c",
                 relation=RelationKind.CONTRADICTS.value, strength=0.85),
        Relation(id="r3", source_fact_id="b", target_fact_id="d",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
    ])

    single = await graph_backend.neighbors("a", max_hops=1)
    batch = await graph_backend.neighbors_batch(["a"], max_hops=1)
    assert batch["a"] == single


@pytest.mark.asyncio
async def test_reverse_neighbors_batch_matches_single_query(graph_backend) -> None:
    """reverse_neighbors_batch([id1]) produces identical output to reverse_neighbors(id1)."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="c",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="b", target_fact_id="c",
                 relation=RelationKind.CONTRADICTS.value, strength=0.85),
    ])

    single = await graph_backend.reverse_neighbors("c", max_hops=1)
    batch = await graph_backend.reverse_neighbors_batch(["c"], max_hops=1)
    assert batch["c"] == single


# ── Multi-hop batch correctness ──


@pytest.mark.asyncio
async def test_neighbors_batch_multi_hop(graph_backend) -> None:
    """2-hop batch walk should match individual 2-hop results."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="b",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="b", target_fact_id="c",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
    ])

    single = await graph_backend.neighbors("a", max_hops=2)
    batch = await graph_backend.neighbors_batch(["a"], max_hops=2)
    assert batch["a"] == single


@pytest.mark.asyncio
async def test_reverse_neighbors_batch_multi_hop(graph_backend) -> None:
    """2-hop reverse batch walk should match individual 2-hop results."""
    await graph_backend.add_relations([
        Relation(id="r1", source_fact_id="a", target_fact_id="b",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
        Relation(id="r2", source_fact_id="b", target_fact_id="c",
                 relation=RelationKind.SAME_TOPIC.value, strength=0.6),
    ])

    single = await graph_backend.reverse_neighbors("c", max_hops=2)
    batch = await graph_backend.reverse_neighbors_batch(["c"], max_hops=2)
    assert batch["c"] == single


# ── recall() integration: query-count reduction ──


@pytest.mark.asyncio
async def test_recall_uses_batch_queries(counting_backend) -> None:
    """With k=10 hits, recall() should fire exactly 2 graph queries
    (neighbors_batch + reverse_neighbors_batch) instead of 20 individual
    calls (10 neighbors + 10 reverse_neighbors).
    """
    svc = _make_service(graph_backend=counting_backend)

    # Seed 10 facts in the vector store.
    for i in range(10):
        await svc.remember(
            f"fact {i}", kind="preference", scope="user",
            skip_contradict_check=True,
        )

    # Add a few cross-edges so relation enrichment is non-empty.
    await counting_backend.add_relations([
        Relation(
            id=f"r{i}", source_fact_id=f"preference:user:fact {i}",
            target_fact_id=f"preference:user:fact {(i + 1) % 10}",
            relation=RelationKind.SAME_TOPIC.value, strength=0.6,
        )
        for i in range(10)
    ])

    hits = await svc.recall("fact", k=10, include_relations=True)
    assert len(hits) == 10

    # Batch methods must be called exactly once each.
    assert counting_backend.neighbors_batch_calls == 1
    assert counting_backend.reverse_neighbors_batch_calls == 1

    # Individual methods must NOT be called by recall().
    assert counting_backend.neighbors_calls == 0
    assert counting_backend.reverse_neighbors_calls == 0


@pytest.mark.asyncio
async def test_recall_batch_includes_reverse_edges(counting_backend) -> None:
    """When B -> A (CONTRADICTS), recalling A should include the reverse edge
    via reverse_neighbors_batch so the caller sees 'B contradicts A'.
    """
    svc = _make_service(graph_backend=counting_backend)
    a = await svc.remember("用 Mac", kind="preference", scope="user",
                           skip_contradict_check=True)
    b = await svc.remember("用 Windows", kind="preference", scope="user",
                           skip_contradict_check=True)
    await svc.relate(source_fact_id=b.id, target_fact_id=a.id,
                     kind=RelationKind.CONTRADICTS)

    hits = await svc.recall("用 Mac", k=5, include_relations=True)
    hit_a = next((h for h in hits if h.fact.id == a.id), None)
    assert hit_a is not None
    rels = [r.relation for r in hit_a.related_relations]
    assert "CONTRADICTS" in rels

    # Verify batch methods were used.
    assert counting_backend.neighbors_batch_calls == 1
    assert counting_backend.reverse_neighbors_batch_calls == 1


@pytest.mark.asyncio
async def test_recall_no_relations_skips_graph(counting_backend) -> None:
    """When include_relations=False, recall() should not touch the graph at all."""
    svc = _make_service(graph_backend=counting_backend)
    await svc.remember("X", kind="preference", scope="user",
                       skip_contradict_check=True)

    hits = await svc.recall("X", k=5, include_relations=False)
    assert len(hits) == 1

    assert counting_backend.neighbors_batch_calls == 0
    assert counting_backend.reverse_neighbors_batch_calls == 0
    assert counting_backend.neighbors_calls == 0
    assert counting_backend.reverse_neighbors_calls == 0
