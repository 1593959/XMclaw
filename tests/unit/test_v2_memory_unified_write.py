"""Unified write-side tests — ``xmclaw-architecture-redesign.md §3.3.4``.

Front-back coverage per the 2026-05-09 standing rule:

Layer 1 (module direct):
  * ``mint_unified_id`` shape + uniqueness
  * ``UnifiedMemorySystem.put`` happy path → vec + graph both write
    with the SAME id
  * Failure on graph step → no vec write, error carries empty
    ``compensated``
  * Failure on vec step AFTER graph wrote → graph rolled back,
    error reports compensated=["graph"]
  * Relations turn into graph edges with a derived id each
  * ``UnifiedMemorySystem.delete`` removes from both indices
  * ``delete`` returns False when nothing matched anywhere
  * ``UnifiedWriteError`` carries the inconsistency surface

Layer 2 (TestClient end-to-end):
  * POST /api/v2/memory/unified_put resolves (route order pitfall)
  * Successful POST returns ``{ok, id}``
  * Returned id is observable via /unified_query semantic axis
  * Empty body → 400, never 5xx
  * Bad ``relations`` shape ignored, not crashed
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.memory import UnifiedMemorySystem, UnifiedWriteError, mint_unified_id


# ── Layer 1 — module direct ───────────────────────────────────────


def test_mint_unified_id_shape_24_hex() -> None:
    """The id is a 24-char lowercase hex string."""
    iid = mint_unified_id("hello")
    assert len(iid) == 24
    assert iid == iid.lower()
    int(iid, 16)  # raises if non-hex


def test_mint_unified_id_uniqueness_under_load() -> None:
    """1000 calls → 1000 distinct ids — uuid4 randomness must
    survive the SHA-256 truncation. Catches a regression where
    someone "optimises" the helper to drop the uuid4 and produce
    deterministic ids per (text, ts)."""
    seen: set[str] = set()
    for _ in range(1000):
        seen.add(mint_unified_id("identical text", ts=1715000000.0))
    assert len(seen) == 1000


def test_mint_unified_id_default_ts_uses_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-ts call still works (defaults to time.time())."""
    iid = mint_unified_id("payload")
    assert len(iid) == 24


def test_unified_write_error_carries_inconsistency_surface() -> None:
    err = UnifiedWriteError(
        "vec failed mid-write",
        indices_written=["graph", "vec"],
        compensated=["graph"],
        original=RuntimeError("disk full"),
    )
    assert err.indices_written == ["graph", "vec"]
    assert err.compensated == ["graph"]
    # Anything in indices_written and not in compensated is dirty.
    dirty = [i for i in err.indices_written if i not in err.compensated]
    assert dirty == ["vec"]
    assert isinstance(err.original, RuntimeError)


# ── Fakes for the put/delete fan-out ──────────────────────────────


@dataclass
class _FakeMemItem:
    id: str
    text: str
    score: float = 0.5
    ts: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeNode:
    id: str
    type: str
    content: str
    created_at: float = 0.0


def _make_fakes(
    *,
    mm_put_raises: Exception | None = None,
    graph_add_node_raises: Exception | None = None,
    graph_add_edge_raises: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    mm = MagicMock()
    mm.put = AsyncMock(side_effect=mm_put_raises) if mm_put_raises \
        else AsyncMock(return_value="forced_id_unused")
    mm.forget = AsyncMock(return_value=None)
    mm.query = AsyncMock(return_value=[])

    graph = MagicMock()
    graph.add_node = AsyncMock(side_effect=graph_add_node_raises) if graph_add_node_raises \
        else AsyncMock(return_value=None)
    graph.add_edge = AsyncMock(side_effect=graph_add_edge_raises) if graph_add_edge_raises \
        else AsyncMock(return_value=None)
    graph.remove_node = AsyncMock(return_value=None)
    graph.remove_edge = AsyncMock(return_value=None)
    graph.get_node = AsyncMock(return_value=None)
    return mm, graph


@pytest.mark.asyncio
async def test_put_happy_path_writes_both_indices_with_same_id() -> None:
    mm, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    new_id = await system.put(text="kettle is in the kitchen")

    # Returned id is the §3.3.4 unified id.
    assert len(new_id) == 24
    # Graph was called with a GraphNode whose id == new_id.
    assert graph.add_node.await_count == 1
    node_arg = graph.add_node.await_args.args[0]
    assert node_arg.id == new_id
    assert node_arg.content == "kettle is in the kitchen"
    assert node_arg.type == "event"
    # Vec store got the SAME id via MemoryItem.id.
    assert mm.put.await_count == 1
    item_arg = mm.put.await_args.args[1]
    assert item_arg.id == new_id
    # Layer maps long_term → long for the legacy provider.
    assert mm.put.await_args.args[0] == "long"
    assert item_arg.layer == "long"
    # Logical layer is preserved in metadata.
    assert item_arg.metadata.get("layer") == "long_term"


@pytest.mark.asyncio
async def test_put_layer_mapping_short_term_to_short() -> None:
    mm, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    await system.put(text="ephemeral", layer="short_term")
    assert mm.put.await_args.args[0] == "short"


@pytest.mark.asyncio
async def test_put_node_type_threads_through_to_graph() -> None:
    mm, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    await system.put(text="Alice is a project lead", node_type="entity")
    node_arg = graph.add_node.await_args.args[0]
    assert node_arg.type == "entity"


@pytest.mark.asyncio
async def test_put_graph_failure_does_not_write_vec() -> None:
    """When graph.add_node raises FIRST, vec must NEVER be touched —
    no compensation needed because no other index wrote."""
    mm, graph = _make_fakes(graph_add_node_raises=RuntimeError("schema reject"))
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    with pytest.raises(UnifiedWriteError) as exc_info:
        await system.put(text="bad shape")

    # Vec was never touched.
    assert mm.put.await_count == 0
    # Empty inconsistency surface — nothing to roll back, nothing dirty.
    err = exc_info.value
    assert err.indices_written == []
    assert err.compensated == []
    assert isinstance(err.original, RuntimeError)


@pytest.mark.asyncio
async def test_put_vec_failure_after_graph_rolls_back_graph() -> None:
    """Graph wrote OK, vec failed → compensation deletes the graph
    node so the indices stay consistent."""
    mm, graph = _make_fakes(mm_put_raises=RuntimeError("disk full"))
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    with pytest.raises(UnifiedWriteError) as exc_info:
        await system.put(text="will fail vec")

    err = exc_info.value
    assert "graph" in err.indices_written
    assert "graph" in err.compensated
    # Compensation called remove_node on the same id.
    assert graph.remove_node.await_count == 1


@pytest.mark.asyncio
async def test_put_relations_each_become_a_graph_edge() -> None:
    mm, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    new_id = await system.put(
        text="root",
        relations=[("target_a", "RELATED_TO"), ("target_b", "CAUSED_BY")],
    )
    assert graph.add_edge.await_count == 2
    edge_args = [c.args[0] for c in graph.add_edge.await_args_list]
    targets = {e.target_id for e in edge_args}
    assert targets == {"target_a", "target_b"}
    relations_seen = {e.relation for e in edge_args}
    assert relations_seen == {"RELATED_TO", "CAUSED_BY"}
    # Source on every edge is the new unified id.
    assert all(e.source_id == new_id for e in edge_args)


@pytest.mark.asyncio
async def test_put_relation_failure_after_vec_rolls_back_vec_and_graph() -> None:
    mm, graph = _make_fakes(graph_add_edge_raises=RuntimeError("edge schema reject"))
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    with pytest.raises(UnifiedWriteError) as exc_info:
        await system.put(
            text="root",
            relations=[("target", "RELATED_TO")],
        )
    err = exc_info.value
    # Both graph node + vec wrote before edges failed.
    assert "graph" in err.indices_written
    assert "vec" in err.indices_written
    # Both should have been compensated.
    assert "graph" in err.compensated
    assert "vec" in err.compensated


@pytest.mark.asyncio
async def test_put_with_metadata_and_embedding() -> None:
    mm, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    await system.put(
        text="vector entry",
        metadata={"source": "user", "kind": "preference"},
        embedding=[0.1, 0.2, 0.3],
    )
    item_arg = mm.put.await_args.args[1]
    assert item_arg.embedding == (0.1, 0.2, 0.3)
    assert item_arg.metadata["source"] == "user"
    # Node also got the embedding for graph-side similarity search.
    node_arg = graph.add_node.await_args.args[0]
    assert node_arg.embedding == (0.1, 0.2, 0.3)


@pytest.mark.asyncio
async def test_put_no_graph_falls_through_to_vec_only() -> None:
    """When the graph index isn't wired, write only goes to vec —
    no error, just a single-index entry. This is the soft-degrade
    path documented on the constructor."""
    mm, _ = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=None)
    new_id = await system.put(text="vec-only")
    assert mm.put.await_count == 1
    assert len(new_id) == 24


@pytest.mark.asyncio
async def test_put_no_vec_falls_through_to_graph_only() -> None:
    """Same soft-degrade in the other direction."""
    _, graph = _make_fakes()
    system = UnifiedMemorySystem(memory_manager=None, memory_graph=graph)
    new_id = await system.put(text="graph-only")
    assert graph.add_node.await_count == 1
    assert len(new_id) == 24


@pytest.mark.asyncio
async def test_delete_removes_from_both_indices() -> None:
    """``delete()`` should call forget on the vec store AND
    remove_node on the graph."""
    mm, graph = _make_fakes()
    # Stage a row so the existence probes return True.
    graph.get_node = AsyncMock(return_value=_FakeNode(
        id="abc123", type="event", content="x", created_at=0.0,
    ))
    mm.query = AsyncMock(return_value=[_FakeMemItem(id="abc123", text="x")])
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    ok = await system.delete("abc123")
    assert ok is True
    assert mm.forget.await_count == 1
    assert mm.forget.await_args.args[0] == "abc123"
    assert graph.remove_node.await_count == 1
    assert graph.remove_node.await_args.args[0] == "abc123"


@pytest.mark.asyncio
async def test_delete_returns_false_when_nothing_matched() -> None:
    """Stale ids should return False, not raise."""
    mm, graph = _make_fakes()
    # Probes both report "no such id".
    mm.query = AsyncMock(return_value=[])
    graph.get_node = AsyncMock(return_value=None)
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    ok = await system.delete("nonexistent")
    assert ok is False


@pytest.mark.asyncio
async def test_delete_keeps_going_past_one_index_error() -> None:
    """If vec.forget raises, graph.remove_node should still be
    attempted (best-effort delete is the contract)."""
    mm, graph = _make_fakes()
    mm.forget = AsyncMock(side_effect=RuntimeError("vec lost"))
    graph.get_node = AsyncMock(return_value=_FakeNode(
        id="abc", type="event", content="x", created_at=0.0,
    ))
    system = UnifiedMemorySystem(memory_manager=mm, memory_graph=graph)

    ok = await system.delete("abc")
    # Graph reported existence + remove called → True
    assert ok is True
    assert graph.remove_node.await_count == 1


# ── Layer 2 — TestClient end-to-end ───────────────────────────────


@pytest.fixture
def memory_client() -> TestClient:
    """Daemon test client with fakes wired into ``app.state``.

    The unified router constructs ``UnifiedMemorySystem`` per-request
    from ``app.state.memory_manager`` + ``app.state.memory_graph``, so
    seeding fakes here is enough — no factory/lifespan dance.
    """
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})

    fake_mm = MagicMock()
    fake_mm.put = AsyncMock(return_value="provider_returns_unused")
    fake_mm.forget = AsyncMock(return_value=None)
    # query is used by the post-put /unified_query in the round-trip
    # case; return whatever was last put-ted.
    fake_mm._stored: list[Any] = []
    async def _query_stub(layer: str, *, text: Any = None, embedding: Any = None,
                          k: int = 10, filters: Any = None) -> list[Any]:
        return list(fake_mm._stored)
    fake_mm.query = AsyncMock(side_effect=_query_stub)
    async def _put_stub(layer: str, item: Any) -> str:
        fake_mm._stored.append(_FakeMemItem(
            id=item.id, text=item.text, score=1.0, ts=item.ts,
            metadata=dict(item.metadata or {}),
        ))
        return item.id
    fake_mm.put = AsyncMock(side_effect=_put_stub)
    app.state.memory_manager = fake_mm

    fake_graph = MagicMock()
    fake_graph._nodes: dict[str, Any] = {}
    async def _add_node_stub(node: Any) -> str:
        fake_graph._nodes[node.id] = node
        return node.id
    fake_graph.add_node = AsyncMock(side_effect=_add_node_stub)
    fake_graph.add_edge = AsyncMock(return_value=None)
    fake_graph.get_node = AsyncMock(
        side_effect=lambda nid: fake_graph._nodes.get(nid),
    )
    fake_graph.remove_node = AsyncMock(return_value=None)
    fake_graph.remove_edge = AsyncMock(return_value=None)
    fake_graph.query_by_type = AsyncMock(return_value=[])
    fake_graph.query_by_time_range = AsyncMock(return_value=[])
    fake_graph.get_neighbors = AsyncMock(return_value=[])
    app.state.memory_graph = fake_graph

    return TestClient(app)


def test_unified_put_endpoint_resolves_not_404(memory_client: TestClient) -> None:
    """Front-back: hit the URL the UI will call. Catches the
    /{filename} catch-all shadowing bug."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={"text": "kettle is in the kitchen"},
    )
    # Anything but 404/405 means the route matched the right handler.
    assert r.status_code != 404, f"route mismatch: {r.text}"
    assert r.status_code != 405, f"method mismatch: {r.text}"
    assert r.status_code == 200, f"unexpected: {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["id"], str)
    assert len(body["id"]) == 24


def test_unified_put_returned_id_observable_via_query(
    memory_client: TestClient,
) -> None:
    """Round-trip: POST /unified_put → POST /unified_query (semantic)
    finds the just-written entry by its unified id."""
    put_resp = memory_client.post(
        "/api/v2/memory/unified_put",
        json={"text": "Alice prefers tea over coffee"},
    )
    assert put_resp.status_code == 200
    new_id = put_resp.json()["id"]

    q_resp = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "Alice", "limit": 10},
    )
    assert q_resp.status_code == 200
    body = q_resp.json()
    ids = [r["id"] for r in body["results"]]
    assert new_id in ids


def test_unified_put_empty_body_400(memory_client: TestClient) -> None:
    """Empty body → 400 with a clear error, never 5xx."""
    r = memory_client.post("/api/v2/memory/unified_put", json={})
    assert r.status_code == 400
    body = r.json()
    assert "text" in body.get("error", "").lower()


def test_unified_put_blank_text_400(memory_client: TestClient) -> None:
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={"text": "   "},
    )
    assert r.status_code == 400


def test_unified_put_invalid_layer_falls_to_default(
    memory_client: TestClient,
) -> None:
    """Bad layer string → silently coerce to long_term, not 4xx —
    same convention as /unified_query's invalid-layer path."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={"text": "x", "layer": "bogus_layer"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unified_put_bad_relations_shape_ignored(
    memory_client: TestClient,
) -> None:
    """Malformed relations entries should be dropped, not crash."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "x",
            "relations": [
                ["valid_target", "RELATED_TO"],
                "not a tuple",
                ["only_one_field"],
                {"target": "t"},
                ["t", 123],  # second field non-string
            ],
        },
    )
    # Endpoint stays 200 — only the valid entry survives the filter.
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unified_put_relations_threaded_through(
    memory_client: TestClient,
) -> None:
    """Well-formed relations should result in graph add_edge calls."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "Alice founded Project Atlas",
            "relations": [["alice_id", "CAUSED_BY"]],
        },
    )
    assert r.status_code == 200
    fake_graph = memory_client.app.state.memory_graph
    assert fake_graph.add_edge.await_count == 1


def test_unified_put_metadata_threaded(memory_client: TestClient) -> None:
    """Caller-supplied metadata is forwarded into the vec entry."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "x",
            "metadata": {"source": "import", "kind": "fact"},
        },
    )
    assert r.status_code == 200
    fake_mm = memory_client.app.state.memory_manager
    item_arg = fake_mm.put.await_args.args[1]
    assert item_arg.metadata.get("source") == "import"
    assert item_arg.metadata.get("kind") == "fact"
