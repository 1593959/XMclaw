"""Chaos / stress tests for MemoryGraph.

Validate concurrency safety, embedding-stress resilience, and
graceful degradation when sqlite-vec is unavailable.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from xmclaw.cognition.memory_graph import GraphNode, GraphEdge, MemoryGraph


@pytest.fixture
def graph(tmp_path: Path):
    db = tmp_path / "mg_stress.db"
    g = MemoryGraph(db_path=str(db))
    yield g
    g.close()


# ── concurrency ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_add_node_race_safety(graph: MemoryGraph) -> None:
    """50 asyncio tasks hammering add_node simultaneously must not
    corrupt the SQLite database or lose writes."""
    async def _worker(worker_id: int) -> None:
        for i in range(20):
            await graph.add_node(
                GraphNode(
                    id=f"w{worker_id}-e{i}",
                    type="event",
                    content=f"w{worker_id}-e{i}",
                    created_at=asyncio.get_event_loop().time(),
                )
            )

    await asyncio.gather(*[_worker(w) for w in range(50)])

    # All 1000 nodes must be present.
    nodes = await graph.query_by_type("event", limit=2000)
    assert len(nodes) == 1000


@pytest.mark.asyncio
async def test_concurrent_mixed_ops(graph: MemoryGraph) -> None:
    """Interleaved reads and writes under load."""
    async def _writer() -> None:
        for i in range(100):
            nid = f"entity-{i}"
            await graph.add_node(
                GraphNode(
                    id=nid,
                    type="entity",
                    content=f"entity-{i}",
                    created_at=asyncio.get_event_loop().time(),
                )
            )
            if i > 0:
                await graph.add_edge(
                    GraphEdge(
                        id=f"edge-{i}",
                        source_id=nid,
                        target_id=f"entity-{i - 1}",
                        relation="RELATED_TO",
                        strength=0.5,
                        created_at=asyncio.get_event_loop().time(),
                    )
                )

    async def _reader() -> None:
        for _ in range(50):
            await graph.query_by_type("entity", limit=10)
            await graph.get_neighbors("entity-0", depth=1)

    await asyncio.gather(_writer(), _reader(), _reader())

    nodes = await graph.query_by_type("entity", limit=200)
    assert len(nodes) == 100


# ── embedding stress ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proactive_recall_under_load(graph: MemoryGraph) -> None:
    """Seed 500 intent nodes with random embeddings, then stress
    proactive_recall with varying contexts."""
    import random
    rng = random.Random(7)

    # Seed nodes
    for i in range(500):
        emb = [rng.random() for _ in range(64)]
        await graph.add_node(
            GraphNode(
                id=f"intent-{i}",
                type="intent",
                content=f"intent-{i}",
                embedding=tuple(emb),
                created_at=asyncio.get_event_loop().time(),
            )
        )

    # Recall stress
    tasks = [
        graph.proactive_recall(
            context=f"search context {i}",
            intent_embedding=[rng.random() for _ in range(64)],
            limit=5,
        )
        for i in range(100)
    ]
    results = await asyncio.gather(*tasks)

    # Every call must return a str — never crash.
    for r in results:
        assert isinstance(r, str)


# ── graceful degradation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_works_without_sqlite_vec() -> None:
    """When sqlite-vec is not available, MemoryGraph must still
    support all non-semantic operations (add, query, neighbors,
    proactive_recall with keyword fallback)."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "no_vec.db"
        # Force _vec_supported = False by monkey-patching the import
        # path inside the module.  We can't easily uninstall sqlite_vec
        # at runtime, so we patch the flag post-construction.
        g = MemoryGraph(db_path=str(db))
        g._vec_supported = False

        try:
            await g.add_node(GraphNode(
                id="evt-1", type="event", content="backup completed",
                created_at=asyncio.get_event_loop().time(),
            ))
            await g.add_node(GraphNode(
                id="ent-1", type="entity", content="backup",
                created_at=asyncio.get_event_loop().time(),
            ))

            # Keyword-based proactive_recall must still work.
            memories = await g.proactive_recall(context="backup")
            assert isinstance(memories, str)
            # Should find the "backup" entity via keyword fallback.
            assert "backup" in memories
        finally:
            g.close()


# ── pathological inputs ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unicode_and_large_content(graph: MemoryGraph) -> None:
    """Nodes with very long or weird content must not break storage."""
    long_text = "A" * 100_000
    unicode_text = "\u0000\u0001\u2603\U0001F600" * 1000

    n1 = await graph.add_node(GraphNode(
        id="long", type="event", content=long_text,
        created_at=asyncio.get_event_loop().time(),
    ))
    n2 = await graph.add_node(GraphNode(
        id="unicode", type="event", content=unicode_text,
        created_at=asyncio.get_event_loop().time(),
    ))

    fetched = await graph.get_node(n1)
    assert fetched is not None
    assert len(fetched.content) == 100_000

    fetched2 = await graph.get_node(n2)
    assert fetched2 is not None
    assert "\u2603" in fetched2.content
