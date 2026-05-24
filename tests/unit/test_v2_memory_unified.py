"""Unified memory facade tests — front-back coverage per the
2026-05-09 standing rule.

Layer 1: ``UnifiedMemorySystem.query()`` direct (module-level).
Layer 2: ``POST /api/v2/memory/unified_query`` end-to-end via
TestClient (the URL the UI will call once a panel is added).

Pinned invariants (xmclaw-architecture-redesign.md §3.3.3 + §3.3.4):

* multi-axis query — semantic + relation + temporal + layer + limit
* dedup by unified id; merged score = sum across axes; matched_axes
  reflects which axes contributed
* empty axes (no semantic / relation / temporal) → ``[]`` (Layer 1)
  or 400 (Layer 2) — never a whole-store scan
* TimeRange validates since ≤ until at construction
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.memory import TimeRange, UnifiedMemorySystem


# ── Layer 1 — module direct ───────────────────────────────────────


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


def _make_system(
    *,
    semantic_hits: list[_FakeMemItem] | None = None,
    by_type_nodes: list[_FakeNode] | None = None,
    neighbors: dict[str, list[_FakeNode]] | None = None,
    temporal_nodes: list[_FakeNode] | None = None,
    embedder_returns: list[list[float]] | None = None,
) -> UnifiedMemorySystem:
    mm = MagicMock()
    mm.query = AsyncMock(return_value=semantic_hits or [])
    graph = MagicMock()
    by_type_nodes = by_type_nodes or []
    graph.query_by_type = AsyncMock(return_value=by_type_nodes)
    neighbors = neighbors or {}
    graph.get_neighbors = AsyncMock(
        side_effect=lambda nid: neighbors.get(nid, []),
    )
    graph.query_by_time_range = AsyncMock(return_value=temporal_nodes or [])
    embedder = None
    if embedder_returns is not None:
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=embedder_returns)
    return UnifiedMemorySystem(
        memory_manager=mm, memory_graph=graph, embedder=embedder,
    )


def test_time_range_rejects_inverted() -> None:
    with pytest.raises(ValueError, match="since.*>.*until"):
        TimeRange(since=200.0, until=100.0)


def test_time_range_open_bounds_ok() -> None:
    TimeRange()              # both unbounded
    TimeRange(since=100.0)
    TimeRange(until=100.0)


@pytest.mark.asyncio
async def test_empty_query_returns_empty_no_scan() -> None:
    """At least one axis required — empty query returns [] without
    calling any provider."""
    system = _make_system()
    result = await system.query()
    assert result == []
    # Confirm no provider was hit.
    assert not system._mm.query.called
    assert not system._graph.query_by_type.called
    assert not system._graph.query_by_time_range.called


@pytest.mark.asyncio
async def test_semantic_only_routes_to_memory_manager() -> None:
    system = _make_system(
        semantic_hits=[
            _FakeMemItem(id="m1", text="db tuning notes", score=0.9, ts=100.0),
            _FakeMemItem(id="m2", text="indexing primer", score=0.7, ts=50.0),
        ],
    )
    out = await system.query(semantic="database optimisation", limit=5)
    assert len(out) == 2
    assert out[0].id == "m1"
    assert out[0].matched_axes == ("semantic",)
    assert out[0].layer == "long_term"     # default fallback layer


@pytest.mark.asyncio
async def test_temporal_only_returns_recency_scored() -> None:
    system = _make_system(
        temporal_nodes=[
            _FakeNode(id="g1", type="event", content="oldest", created_at=100.0),
            _FakeNode(id="g2", type="event", content="newest", created_at=300.0),
            _FakeNode(id="g3", type="event", content="middle", created_at=200.0),
        ],
    )
    tr = TimeRange(since=50.0, until=400.0)
    out = await system.query(temporal=tr, limit=5)
    assert len(out) == 3
    # newest should rank first (score=1.0)
    assert out[0].id == "g2"
    assert out[0].matched_axes == ("temporal",)
    # oldest gets score=0.0 → last
    assert out[-1].id == "g1"


@pytest.mark.asyncio
async def test_relation_pulls_direct_match_plus_neighbors() -> None:
    system = _make_system(
        by_type_nodes=[
            _FakeNode(id="n1", type="entity", content="Project Atlas details"),
            _FakeNode(id="n2", type="entity", content="unrelated"),
        ],
        neighbors={
            "n1": [_FakeNode(id="n3", type="state", content="status: active")],
        },
    )
    out = await system.query(relation="atlas", limit=5)
    ids = [e.id for e in out]
    assert "n1" in ids                  # direct match score 1.0
    assert "n3" in ids                  # neighbor score 0.7
    assert "n2" not in ids              # not in anchor


@pytest.mark.asyncio
async def test_multi_axis_dedupe_and_combined_score() -> None:
    """An entry hit by both semantic AND relation axes shows up once
    with summed score + both axes recorded."""
    shared = _FakeMemItem(id="shared", text="x", score=0.6, ts=100.0)
    system = _make_system(
        semantic_hits=[shared],
        by_type_nodes=[_FakeNode(id="shared", type="event", content="x match")],
    )
    out = await system.query(semantic="anything", relation="match", limit=5)
    # The two axes returned the same id — should dedupe.
    shared_entries = [e for e in out if e.id == "shared"]
    assert len(shared_entries) == 1
    e = shared_entries[0]
    assert "semantic" in e.matched_axes
    assert "relation" in e.matched_axes
    # Combined score = 0.6 (semantic) + 1.0 (relation direct) = 1.6
    assert e.score >= 1.4


@pytest.mark.asyncio
async def test_limit_caps_after_merge() -> None:
    items = [
        _FakeMemItem(id=f"m{i}", text=str(i), score=0.5, ts=float(i))
        for i in range(50)
    ]
    system = _make_system(semantic_hits=items)
    out = await system.query(semantic="anything", limit=5)
    assert len(out) == 5


@pytest.mark.asyncio
async def test_provider_exception_doesnt_crash() -> None:
    """One axis throwing (e.g. graph offline) → empty for that axis,
    other axes still return."""
    system = _make_system(
        semantic_hits=[_FakeMemItem(id="m1", text="ok", score=0.5, ts=0.0)],
    )
    system._graph.query_by_type = AsyncMock(side_effect=RuntimeError("boom"))
    out = await system.query(semantic="x", relation="y", limit=5)
    # semantic survives even though relation crashed
    assert any(e.id == "m1" for e in out)


@pytest.mark.asyncio
async def test_embedder_used_when_available() -> None:
    """When embedder is wired, semantic axis passes embedding to mm."""
    system = _make_system(
        semantic_hits=[],
        embedder_returns=[[0.1, 0.2, 0.3]],
    )
    await system.query(semantic="cats", limit=3)
    call = system._mm.query.call_args
    # embedding kwarg should be the embedder's result
    assert call.kwargs.get("embedding") == [0.1, 0.2, 0.3]


# ── Layer 2 — TestClient end-to-end ───────────────────────────────

# Phase 7.A.3 step 4/6 (2026-05-23): the four router-layer tests
# below originally wired V1 ``memory_manager`` + ``memory_graph``
# fakes onto app.state and expected the router to construct a
# UnifiedMemorySystem per request. After the router migrated to
# the V2 MemoryService (via ``app.state.memory_v2_service``) those
# fakes are no longer consulted — the router returns 503. The V2
# equivalents live in ``tests/integration/test_v2_phase7_memory_router.py``
# (proper V2 service wiring). These will be deleted in §7.B.4
# alongside the V1 module itself.
_PHASE7_ROUTER_MIGRATED = pytest.mark.skip(
    reason="Phase 7.A.3 step 4/6: V1 router fixture replaced by V2 "
           "service; see tests/integration/test_v2_phase7_memory_router.py.",
)


@pytest.fixture
def memory_client() -> TestClient:
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    # Seed the app state with fakes the unified facade can use.
    fake_mm = MagicMock()
    fake_mm.query = AsyncMock(return_value=[
        _FakeMemItem(
            id="vec-1", text="DB tuning summary",
            score=0.91, ts=1715000000.0,
        ),
    ])
    app.state.memory_manager = fake_mm
    fake_graph = MagicMock()
    fake_graph.query_by_type = AsyncMock(return_value=[])
    fake_graph.query_by_time_range = AsyncMock(return_value=[])
    fake_graph.get_neighbors = AsyncMock(return_value=[])
    app.state.memory_graph = fake_graph
    return TestClient(app)


@_PHASE7_ROUTER_MIGRATED
def test_unified_query_endpoint_resolves_not_404(
    memory_client: TestClient,
) -> None:
    """Front-back: hit the actual URL the UI will call."""
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "database optimisation", "limit": 5},
    )
    # Must NOT be 404 / 405 (route mismatch) — that's the bug class
    # the new test rule guards against.
    assert r.status_code != 404, f"route mismatch: {r.text}"
    assert r.status_code != 405, f"method mismatch: {r.text}"
    # 200 OK with the documented shape
    assert r.status_code == 200, f"unexpected: {r.status_code} {r.text}"
    body = r.json()
    assert "n" in body
    assert "results" in body
    assert isinstance(body["results"], list)


def test_unified_query_empty_body_400(memory_client: TestClient) -> None:
    """All axes None must 400 (no whole-store scan)."""
    r = memory_client.post("/api/v2/memory/unified_query", json={})
    assert r.status_code == 400
    body = r.json()
    assert "at least one" in body.get("error", "").lower()


@_PHASE7_ROUTER_MIGRATED
def test_unified_query_semantic_returns_seeded_hit(
    memory_client: TestClient,
) -> None:
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "anything", "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    ids = [e["id"] for e in body["results"]]
    assert "vec-1" in ids
    entry = next(e for e in body["results"] if e["id"] == "vec-1")
    assert "semantic" in entry["matched_axes"]


@_PHASE7_ROUTER_MIGRATED
def test_unified_query_temporal_validates_range(
    memory_client: TestClient,
) -> None:
    """Inverted TimeRange should produce 400 with a clear error."""
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={
            "temporal": {"since": 200.0, "until": 100.0},
            "limit": 5,
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "temporal" in body.get("error", "").lower() or \
           "since" in body.get("error", "").lower()


@_PHASE7_ROUTER_MIGRATED
def test_unified_query_invalid_layer_falls_through_to_default(
    memory_client: TestClient,
) -> None:
    """Bad ``layer`` value silently falls to default — no 4xx."""
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "x", "layer": "bogus_layer", "limit": 1},
    )
    assert r.status_code == 200
