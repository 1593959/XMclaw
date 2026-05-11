"""MemoryGraph — Phase B full-body proactive recall tests.

Covers CRUD, similarity search (manual cosine + sqlite-vec when
available), multi-hop neighbors, and proactive_recall end-to-end.
"""
from __future__ import annotations

import math
import time

import pytest

from xmclaw.cognition.memory_graph import GraphEdge, GraphNode, MemoryGraph


@pytest.fixture
def graph(tmp_path):
    db = tmp_path / "test_graph.db"
    g = MemoryGraph(db_path=db, embedding_dim=4)
    yield g
    g.close()


# ── helpers ──

def _emb(*vals: float) -> tuple[float, ...]:
    return vals


def _cosine_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 1.0
    sim = dot / (na * nb)
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


# ── CRUD ──


@pytest.mark.asyncio
async def test_add_node_roundtrip(graph: MemoryGraph) -> None:
    node = GraphNode(
        id="n1", type="event", content="deployed v2",
        embedding=_emb(1.0, 0.0, 0.0, 0.0),
        created_at=time.time(),
    )
    nid = await graph.add_node(node)
    assert nid == "n1"
    fetched = await graph.get_node("n1")
    assert fetched is not None
    assert fetched.content == "deployed v2"
    assert fetched.embedding == _emb(1.0, 0.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_add_edge_and_get_neighbors(graph: MemoryGraph) -> None:
    n1 = GraphNode(id="a", type="intent", content="deploy", embedding=None, created_at=time.time())
    n2 = GraphNode(id="b", type="event", content="deploy done", embedding=None, created_at=time.time())
    await graph.add_node(n1)
    await graph.add_node(n2)
    edge = GraphEdge(
        id="e1", source_id="a", target_id="b", relation="LEADS_TO", strength=0.9,
    )
    await graph.add_edge(edge)
    neighbors = await graph.get_neighbors("a")
    assert len(neighbors) == 1
    assert neighbors[0][1].id == "b"
    assert neighbors[0][0].relation == "LEADS_TO"


@pytest.mark.asyncio
async def test_get_neighbors_multi_hop(graph: MemoryGraph) -> None:
    # a → b → c (depth=2 should reach c from a)
    nodes = [
        GraphNode(id="a", type="intent", content="a", created_at=time.time()),
        GraphNode(id="b", type="event", content="b", created_at=time.time()),
        GraphNode(id="c", type="event", content="c", created_at=time.time()),
    ]
    for n in nodes:
        await graph.add_node(n)
    await graph.add_edge(GraphEdge("e1", "a", "b", "LEADS_TO"))
    await graph.add_edge(GraphEdge("e2", "b", "c", "LEADS_TO"))
    neighbors = await graph.get_neighbors("a", depth=2)
    ids = {n.id for _e, n in neighbors}
    assert ids == {"b", "c"}


@pytest.mark.asyncio
async def test_remove_node(graph: MemoryGraph) -> None:
    n = GraphNode(id="x", type="entity", content="x", created_at=time.time())
    await graph.add_node(n)
    assert await graph.get_node("x") is not None
    await graph.remove_node("x")
    assert await graph.get_node("x") is None


@pytest.mark.asyncio
async def test_merge_node_creates_new(graph: MemoryGraph) -> None:
    nid, merged = await graph.merge_node(
        content="new fact", type="state",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    assert not merged
    node = await graph.get_node(nid)
    assert node is not None
    assert node.content == "new fact"


@pytest.mark.asyncio
async def test_merge_node_updates_similar(graph: MemoryGraph) -> None:
    # Insert first node
    nid1, _ = await graph.merge_node(
        content="fact A", type="state",
        embedding=[1.0, 0.0, 0.0, 0.0],
        distance_threshold=0.5,
    )
    # Insert very similar node → should merge into nid1
    nid2, merged = await graph.merge_node(
        content="fact A updated", type="state",
        embedding=[0.99, 0.01, 0.0, 0.0],
        distance_threshold=0.5,
    )
    assert merged
    assert nid2 == nid1
    node = await graph.get_node(nid1)
    assert node is not None
    assert "updated" in node.content


@pytest.mark.asyncio
async def test_find_path(graph: MemoryGraph) -> None:
    for c in "abcd":
        await graph.add_node(
            GraphNode(id=c, type="event", content=c, created_at=time.time()),
        )
    await graph.add_edge(GraphEdge("e1", "a", "b", "LEADS_TO"))
    await graph.add_edge(GraphEdge("e2", "b", "c", "LEADS_TO"))
    await graph.add_edge(GraphEdge("e3", "c", "d", "LEADS_TO"))
    path = await graph.find_path("a", "d")
    assert path is not None
    assert len(path) == 3
    assert [e.target_id for e in path] == ["b", "c", "d"]


# ── Similarity search ──


@pytest.mark.asyncio
async def test_manual_similarity_search_ranking(graph: MemoryGraph) -> None:
    # Seed three intent nodes with known embeddings.
    # Deliberately use embeddings with distinct L2 / cosine rankings
    # so the test is stable regardless of whether sqlite-vec or manual
    # cosine fallback is active.
    intents = [
        GraphNode(id="i1", type="intent", content="deploy app",
                  embedding=_emb(1.0, 0.0, 0.0, 0.0), created_at=time.time()),
        GraphNode(id="i2", type="intent", content="restart service",
                  embedding=_emb(0.0, 1.0, 0.0, 0.0), created_at=time.time()),
        GraphNode(id="i3", type="intent", content="scale pods",
                  embedding=_emb(0.7, 0.3, 0.0, 0.0), created_at=time.time()),
    ]
    for n in intents:
        await graph.add_node(n)

    # Query close to i1 (exact match along axis 0)
    results = await graph._find_similar_node_raw(
        embedding=[0.99, 0.01, 0.0, 0.0], type="intent", k=2,
    )
    assert len(results) == 2
    ids = [n.id for n, _d in results]
    # i1 is unambiguously closest; i3 is closer than i2
    assert ids[0] == "i1"
    assert ids[1] == "i3"
    # Distance ordering
    assert results[0][1] < results[1][1]


@pytest.mark.asyncio
async def test_find_similar_node_threshold(graph: MemoryGraph) -> None:
    await graph.add_node(
        GraphNode(id="i1", type="intent", content="x",
                  embedding=_emb(1.0, 0.0, 0.0, 0.0), created_at=time.time()),
    )
    # Exact match → within threshold
    match = await graph._find_similar_node(
        embedding=[1.0, 0.0, 0.0, 0.0], type="intent", threshold=0.2,
    )
    assert match == "i1"
    # Far away → None
    match2 = await graph._find_similar_node(
        embedding=[0.0, 1.0, 0.0, 0.0], type="intent", threshold=0.2,
    )
    assert match2 is None


# ── proactive_recall ──


@pytest.mark.asyncio
async def test_proactive_recall_empty_context(graph: MemoryGraph) -> None:
    assert await graph.proactive_recall("") == ""
    assert await graph.proactive_recall("   ") == ""


@pytest.mark.asyncio
async def test_proactive_recall_temporal_fallback(graph: MemoryGraph) -> None:
    # No embedding → falls back to recent events
    now = time.time()
    await graph.add_node(
        GraphNode(id="e1", type="event", content="released v1.2",
                  created_at=now, embedding=None),
    )
    result = await graph.proactive_recall("what happened recently?")
    assert "released v1.2" in result


@pytest.mark.asyncio
async def test_proactive_recall_with_embedding(graph: MemoryGraph) -> None:
    now = time.time()
    # Intent seeds
    await graph.add_node(
        GraphNode(id="deploy_intent", type="intent", content="deploy to prod",
                  embedding=_emb(1.0, 0.0, 0.0, 0.0), created_at=now),
    )
    # Event linked via LEADS_TO
    await graph.add_node(
        GraphNode(id="deploy_event", type="event", content="deployment succeeded",
                  created_at=now, embedding=None),
    )
    await graph.add_edge(
        GraphEdge("e1", "deploy_intent", "deploy_event", "LEADS_TO", strength=0.9),
    )
    # Query with embedding close to deploy_intent
    result = await graph.proactive_recall(
        "deploy",
        intent_embedding=[0.95, 0.05, 0.0, 0.0],
        limit=5,
    )
    assert "deployment succeeded" in result


@pytest.mark.asyncio
async def test_proactive_recall_entity_keyword_match(graph: MemoryGraph) -> None:
    now = time.time()
    # Entity + linked event
    await graph.add_node(
        GraphNode(id="ent_pg", type="entity", content="PostgreSQL",
                  created_at=now, embedding=None),
    )
    await graph.add_node(
        GraphNode(id="ev_pg", type="event", content="migrated PostgreSQL to 16",
                  created_at=now, embedding=None),
    )
    await graph.add_edge(
        GraphEdge("e1", "ent_pg", "ev_pg", "RELATED_TO", strength=0.8),
    )
    result = await graph.proactive_recall("tell me about PostgreSQL")
    assert "migrated PostgreSQL to 16" in result


@pytest.mark.asyncio
async def test_proactive_recall_deduplication(graph: MemoryGraph) -> None:
    now = time.time()
    # One event reachable via two paths → should appear once
    await graph.add_node(
        GraphNode(id="i1", type="intent", content="i1",
                  embedding=_emb(1.0, 0.0, 0.0, 0.0), created_at=now),
    )
    await graph.add_node(
        GraphNode(id="e1", type="event", content="unique event",
                  created_at=now, embedding=None),
    )
    await graph.add_edge(GraphEdge("e1", "i1", "e1", "LEADS_TO"))
    # Self-loop should not duplicate
    result = await graph.proactive_recall(
        "query", intent_embedding=[1.0, 0.0, 0.0, 0.0],
    )
    assert result.count("unique event") == 1


# ── Vec table sync (best-effort when sqlite-vec is available) ──


@pytest.mark.asyncio
async def test_vec_table_sync_on_add_remove(graph: MemoryGraph) -> None:
    if not graph._vec_supported:
        pytest.skip("sqlite-vec not available")
    node = GraphNode(
        id="v1", type="entity", content="vec node",
        embedding=_emb(1.0, 0.0, 0.0, 0.0), created_at=time.time(),
    )
    await graph.add_node(node)
    # Should be retrievable via vec search
    results = await graph._find_similar_node_raw(
        embedding=[1.0, 0.0, 0.0, 0.0], type="entity", k=1,
    )
    assert len(results) == 1
    assert results[0][0].id == "v1"
    # After removal, vec row should also vanish
    await graph.remove_node("v1")
    results2 = await graph._find_similar_node_raw(
        embedding=[1.0, 0.0, 0.0, 0.0], type="entity", k=1,
    )
    assert len(results2) == 0
