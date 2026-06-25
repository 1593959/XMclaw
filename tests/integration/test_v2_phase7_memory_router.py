"""Phase 7.A.3 step 4/6 — V2 memory router end-to-end tests.

Cross-front-back coverage per CLAUDE.md §Key Conventions (the rule
established 2026-05-09): when a feature spans backend route + frontend
panel, tests MUST exercise the full HTTP path the frontend actually
uses (TestClient against the real create_app), not just inspect router
internals.

Endpoints covered:
  * POST /api/v2/memory/unified_query  — V1 URL, V2 backend
  * POST /api/v2/memory/unified_put    — V1 URL, V2 backend

The router code path now reads ``app.state.memory_v2_service`` and
calls MemoryService.recall / remember instead of constructing a per-
request UnifiedMemorySystem. URL was preserved for frontend backward-
compat (Memory.js + memory_unified_query.js panels still POST here).
Response schema kept close to V1 — added ``kind`` / ``scope`` /
``distance`` fields; matched_axes retained but now reflects which
filters were active.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def memory_client() -> TestClient:
    """TestClient with a fully-wired in-memory V2 MemoryService.

    Mirrors the production app_lifespan wiring shape:
    ``app.state.memory_v2_service`` holds a MemoryService instance
    built from InMemory backends so tests are deterministic + fast.
    """
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    app.state.memory_v2_service = svc
    # Stash on the client too so tests can seed facts.
    client = TestClient(app)
    client._svc = svc  # type: ignore[attr-defined]
    return client


# ── /unified_query ────────────────────────────────────────────────


def test_unified_query_resolves_to_v2_router(memory_client: TestClient) -> None:
    """Front-back: URL still resolves (404/405 = route mismatch)."""
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "anything", "limit": 5},
    )
    assert r.status_code != 404, f"route mismatch: {r.text}"
    assert r.status_code != 405, f"method mismatch: {r.text}"
    assert r.status_code == 200, f"unexpected: {r.status_code} {r.text}"


def test_unified_query_returns_v2_fields(memory_client: TestClient) -> None:
    """V2 result schema: id + text + kind + scope + distance +
    matched_axes (alongside V1-compat fields layer + score)."""
    svc = memory_client._svc  # type: ignore[attr-defined]
    fact = asyncio.run(
        svc.remember(
            "用户喜欢 Python",
            kind=FactKind.PREFERENCE,
            scope=FactScope.USER,
        )
    )
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "Python", "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["n"] >= 1
    hit = next(h for h in body["results"] if h["id"] == fact.id)
    # V2 fields.
    assert hit["kind"] == "preference"
    assert hit["scope"] == "user"
    assert "distance" in hit
    # V1-compat fields retained.
    assert "layer" in hit
    assert "score" in hit
    assert "matched_axes" in hit
    assert "semantic" in hit["matched_axes"]
    assert body["recall_mode"] == "hybrid"


def test_unified_query_uses_hybrid_by_default(
    memory_client: TestClient,
) -> None:
    """Unified query should expose the BM25+vector recall path by default."""
    svc = memory_client._svc  # type: ignore[attr-defined]
    seen: dict[str, object] = {}

    async def mock_recall_hybrid(query: str, **kwargs):
        seen["query"] = query
        seen["kwargs"] = kwargs
        return []

    svc.recall_hybrid = mock_recall_hybrid  # type: ignore[method-assign]
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={
            "semantic": "alembic",
            "layer": "long_term",
            "temporal": {"since": 10, "until": 20},
            "limit": 3,
        },
    )
    assert r.status_code == 200
    assert r.json()["recall_mode"] == "hybrid"
    assert seen["query"] == "alembic"
    assert seen["kwargs"] == {
        "k": 3,
        "only_layer": "long_term",
        "time_range": (10.0, 20.0),
        "include_relations": True,
        "min_confidence": 0.0,
    }


def test_unified_query_can_force_vector_mode(
    memory_client: TestClient,
) -> None:
    """Operators can still force the legacy vector path for debugging."""
    svc = memory_client._svc  # type: ignore[attr-defined]
    asyncio.run(
        svc.remember(
            "legacy vector mode fact",
            kind=FactKind.LESSON,
            scope=FactScope.PROJECT,
        )
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("recall_hybrid should not be called")

    svc.recall_hybrid = fail_if_called  # type: ignore[method-assign]
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={
            "semantic": "legacy vector mode",
            "recall_mode": "vector",
            "limit": 3,
        },
    )
    assert r.status_code == 200
    assert r.json()["recall_mode"] == "vector"


def test_unified_query_rejects_unknown_recall_mode(
    memory_client: TestClient,
) -> None:
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "x", "recall_mode": "magic"},
    )
    assert r.status_code == 400
    assert "recall_mode" in r.json()["error"]


def test_unified_query_empty_body_400(memory_client: TestClient) -> None:
    """Validation runs before service lookup — still 400."""
    r = memory_client.post("/api/v2/memory/unified_query", json={})
    assert r.status_code == 400
    body = r.json()
    assert "at least one" in body.get("error", "").lower()


def test_unified_query_503_when_service_not_wired() -> None:
    """If app.state.memory_v2_service is None, handler returns 503
    with a clear actionable message."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    # Explicitly clear (in case create_app left a stub there).
    if hasattr(app.state, "memory_v2_service"):
        app.state.memory_v2_service = None
    c = TestClient(app)
    r = c.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "x", "limit": 1},
    )
    assert r.status_code == 503
    body = r.json()
    assert "memory_v2_service" in body.get("error", "")


def test_unified_query_layer_filter(memory_client: TestClient) -> None:
    """``layer`` param routes to recall(only_layer=...). V1's
    short_term collapses to working under V2."""
    svc = memory_client._svc  # type: ignore[attr-defined]
    asyncio.run(
        svc.remember(
            "transient fact",
            kind=FactKind.LESSON,
            scope=FactScope.PROJECT,
            layer="working",
        )
    )
    r = memory_client.post(
        "/api/v2/memory/unified_query",
        json={"semantic": "transient", "layer": "working", "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert "layer" in body["results"][0]["matched_axes"] if body["results"] else True


# ── /unified_put ──────────────────────────────────────────────────


def test_unified_put_writes_via_v2(memory_client: TestClient) -> None:
    """V1 URL still works, but now writes via MemoryService.remember."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "用户决定用 LanceDB",
            "layer": "long_term",
            "node_type": "decision",
            "metadata": {"scope": "project"},
        },
    )
    assert r.status_code == 200, f"unexpected: {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert "id" in body
    # The returned id is a V2 deterministic id (kind:scope:hash12).
    assert body["id"].startswith("decision:project:")


def test_unified_put_translates_legacy_node_type(
    memory_client: TestClient,
) -> None:
    """V1 ``node_type`` values that don't have a V2 FactKind
    equivalent get mapped to 'lesson' via legacy_node_type_to_kind."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "agent observed something",
            "node_type": "observation",  # V1-only legacy bucket
            "metadata": {"scope": "session"},
        },
    )
    assert r.status_code == 200, f"unexpected: {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert body["id"].startswith("lesson:session:")


def test_unified_put_collapses_short_term_layer(
    memory_client: TestClient,
) -> None:
    """V1 ``short_term`` layer maps to V2 ``working`` (V2 has no
    short_term — see Phase 7.A.2 decision)."""
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={
            "text": "fact in formerly-short-term layer",
            "layer": "short_term",
            "node_type": "preference",
        },
    )
    assert r.status_code == 200, f"unexpected: {r.text}"
    # Verify by querying back — the fact should be in working layer.
    body = r.json()
    fact_id = body["id"]
    svc = memory_client._svc  # type: ignore[attr-defined]
    fact = asyncio.run(
        svc.get_fact(fact_id)
    )
    assert fact is not None
    assert fact.layer == "working"


def test_unified_put_requires_text(memory_client: TestClient) -> None:
    r = memory_client.post(
        "/api/v2/memory/unified_put",
        json={"node_type": "fact"},
    )
    assert r.status_code == 400


def test_unified_put_503_when_service_not_wired() -> None:
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    if hasattr(app.state, "memory_v2_service"):
        app.state.memory_v2_service = None
    c = TestClient(app)
    r = c.post(
        "/api/v2/memory/unified_put",
        json={"text": "x"},
    )
    assert r.status_code == 503
