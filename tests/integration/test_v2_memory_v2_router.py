"""Integration tests for /api/v2/memory/v2 router (Phase 5a).

Cross-boundary tests per CLAUDE.md rule: tests must exercise the
full HTTP path the frontend actually uses. Hits a real
``TestClient(create_app(...))`` with a mock-but-real
MemoryService attached to app.state.

Covers the 6 endpoints:
  GET    /status
  GET    /facts (with filters)
  GET    /facts/{id}
  POST   /facts
  DELETE /facts/{id}
  GET    /graph
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xmclaw.daemon.routers import memory_v2 as memory_v2_router
from xmclaw.memory.v2 import (
    EmbeddingService,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)


# ── Fixture: minimal app with v2 router + service ────────────────


def _build_app(with_service: bool = True) -> FastAPI:
    """Build a minimal FastAPI that mounts only the v2 router.

    Bypasses the full daemon factory (heavy) since we're just
    testing HTTP wiring + router behaviour against a real
    in-memory MemoryService.
    """
    app = FastAPI()
    app.include_router(memory_v2_router.router)
    if with_service:
        svc = MemoryService(
            vector_backend=InMemoryVectorBackend(),
            graph_backend=InMemoryGraphBackend(),
            embedder=EmbeddingService(StubEmbedder(dim=4)),
        )
        app.state.memory_v2_service = svc
    return app


# ── status ────────────────────────────────────────────────────────


def test_status_disabled_when_no_service() -> None:
    app = _build_app(with_service=False)
    with TestClient(app) as client:
        r = client.get("/api/v2/memory/v2/status")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False


def test_status_healthy_when_service_attached() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/api/v2/memory/v2/status")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["healthy"] is True
        assert body["embedder_dim"] == 4
        assert body["fact_count"] == 0


# ── facts list + create ──────────────────────────────────────────


def test_create_and_list_facts() -> None:
    app = _build_app()
    with TestClient(app) as client:
        # Create one.
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "用户喜欢简短回复",
                "kind": "preference",
                "scope": "user",
            },
        )
        assert r.status_code == 200, r.text
        created = r.json()["created"]
        assert created["text"] == "用户喜欢简短回复"

        # List back.
        r2 = client.get("/api/v2/memory/v2/facts")
        body = r2.json()
        assert body["total"] == 1
        assert body["facts"][0]["text"] == "用户喜欢简短回复"


def test_create_rejects_invalid_kind() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "x", "kind": "garbage", "scope": "user"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_kind"


def test_create_rejects_empty_text() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "  ", "kind": "preference", "scope": "user"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "missing_text"


def test_list_facts_filter_by_kind() -> None:
    app = _build_app()
    with TestClient(app) as client:
        client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "X", "kind": "preference", "scope": "user"},
        )
        client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "Y", "kind": "project", "scope": "project"},
        )
        r = client.get("/api/v2/memory/v2/facts?kind=preference")
        body = r.json()
        assert body["total"] == 1
        assert body["facts"][0]["text"] == "X"


def test_list_facts_keyword_search() -> None:
    app = _build_app()
    with TestClient(app) as client:
        client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "陪玩店业务",
                "kind": "project", "scope": "project",
            },
        )
        client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "用户偏好",
                "kind": "preference", "scope": "user",
            },
        )
        r = client.get("/api/v2/memory/v2/facts?q=陪玩店")
        body = r.json()
        assert body["total"] == 1
        assert "陪玩店" in body["facts"][0]["text"]


# ── single fact + neighbors ──────────────────────────────────────


def test_get_fact_with_neighbors() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "X",
                "kind": "preference",
                "scope": "user",
            },
        )
        fid = r.json()["created"]["id"]
        # Get detail.
        r2 = client.get(f"/api/v2/memory/v2/facts/{fid}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["fact"]["id"] == fid
        assert "neighbors" in body


def test_get_fact_404_when_absent() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/api/v2/memory/v2/facts/nope:nope:000000000000")
        assert r.status_code == 404


# ── delete ───────────────────────────────────────────────────────


def test_delete_fact() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "X", "kind": "preference", "scope": "user"},
        )
        fid = r.json()["created"]["id"]
        r2 = client.delete(f"/api/v2/memory/v2/facts/{fid}")
        assert r2.status_code == 200
        assert r2.json()["deleted"] == 1
        # Subsequent get → 404.
        r3 = client.get(f"/api/v2/memory/v2/facts/{fid}")
        assert r3.status_code == 404


# ── graph endpoint ───────────────────────────────────────────────


def test_graph_overview_empty() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.get("/api/v2/memory/v2/graph")
        assert r.status_code == 200
        body = r.json()
        assert body["nodes"] == []
        assert body["edges"] == []


def test_graph_overview_returns_nodes() -> None:
    app = _build_app()
    with TestClient(app) as client:
        for text in ["A", "B"]:
            client.post(
                "/api/v2/memory/v2/facts",
                json={"text": text, "kind": "preference", "scope": "user"},
            )
        r = client.get("/api/v2/memory/v2/graph?limit=5")
        body = r.json()
        assert len(body["nodes"]) == 2


def test_graph_focus_returns_subgraph() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "X", "kind": "preference", "scope": "user"},
        )
        fid = r.json()["created"]["id"]
        r2 = client.get(
            f"/api/v2/memory/v2/graph?focus_fact_id={fid}&max_hops=1",
        )
        assert r2.status_code == 200
        body = r2.json()
        assert any(n["id"] == fid for n in body["nodes"])


# ── 503 when v2 disabled ─────────────────────────────────────────


def test_endpoints_return_503_when_service_missing() -> None:
    app = _build_app(with_service=False)
    with TestClient(app) as client:
        # status returns enabled:false (200, special case)
        r1 = client.get("/api/v2/memory/v2/status")
        assert r1.status_code == 200
        assert r1.json()["enabled"] is False
        # Other endpoints → 503
        for path in [
            "/api/v2/memory/v2/facts",
            "/api/v2/memory/v2/graph",
        ]:
            r = client.get(path)
            assert r.status_code == 503
            assert r.json()["error"] == "memory_v2_disabled"
