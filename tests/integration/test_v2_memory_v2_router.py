"""Integration tests for /api/v2/memory/v2 router (Phase 5a).

Cross-boundary tests per CLAUDE.md rule: tests must exercise the
full HTTP path the frontend actually uses. Hits a real
``TestClient(create_app(...))`` with a mock-but-real
MemoryService attached to app.state.

Covers the 6 endpoints:
  GET    /status
  GET    /facts (with filters, with include_superseded)
  GET    /facts/{id}
  POST   /facts
  POST   /deduplicate
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
    MemoryCandidate,
    MemoryCandidateStore,
    MemoryService,
    StubEmbedder,
)


# ── Tight embedder for forcing near-dup clustering ──────────────


class _TightEmbedder:
    """Always-same-vector embedder so deduplicate() reliably clusters."""
    name = "tight"
    dim = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def is_available(self) -> bool:
        return True


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


def test_candidate_list_reject_and_approve(tmp_path) -> None:
    app = _build_app()
    store = MemoryCandidateStore(tmp_path / "candidates.db")
    reject = store.create(MemoryCandidate.create(
        text="未验证的失败方法",
        kind="lesson",
        scope="project",
        bucket="failure_modes",
        source="post_sampling",
        reason="unverified_extracted_lesson",
    ))
    approve = store.create(MemoryCandidate.create(
        text="用户偏好使用中文交流",
        kind="preference",
        scope="user",
        bucket="user_preference",
        source="post_sampling",
        reason="explicit_preference",
    ))
    app.state.memory_candidate_store = store

    with TestClient(app) as client:
        listed = client.get("/api/v2/memory/v2/candidates").json()
        assert listed["enabled"] is True
        assert listed["stats"]["by_status"]["pending"] == 2

        r1 = client.post(
            f"/api/v2/memory/v2/candidates/{reject.id}/reject",
            json={"reason": "wrong method"},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["candidate"]["status"] == "rejected"

        r2 = client.post(
            f"/api/v2/memory/v2/candidates/{approve.id}/approve",
            json={"reason": "user preference"},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["candidate"]["status"] == "promoted"
        assert body["fact"]["text"] == "用户偏好使用中文交流"


def test_create_rejects_invalid_kind() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "x", "kind": "garbage", "scope": "user"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_kind"


def test_create_accepts_lesson_kind() -> None:
    """Wave-27 follow-up: lesson must be a valid POST kind so the
    extract-hooks dual-write path + manual UI create both work."""
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "grep before reading huge files",
                "kind": "lesson",
                "scope": "project",
                "confidence": 0.7,
            },
        )
        assert r.status_code == 200, r.text
        created = r.json()["created"]
        assert created["kind"] == "lesson"
        # Lessons surface under the kind filter.
        r2 = client.get("/api/v2/memory/v2/facts?kind=lesson")
        assert r2.status_code == 200
        body = r2.json()
        assert body["total"] == 1
        assert body["facts"][0]["text"] == "grep before reading huge files"


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


# ── deduplicate + superseded filter ──────────────────────────────


def _build_app_with_tight_embedder() -> FastAPI:
    """Variant of _build_app() using the tight embedder so writes
    cluster reliably for dedup-related tests."""
    app = FastAPI()
    app.include_router(memory_v2_router.router)
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(_TightEmbedder()),
    )
    app.state.memory_v2_service = svc
    return app


def test_deduplicate_dry_run_reports_clusters() -> None:
    """POST /deduplicate {dry_run:true} returns the report without writing."""
    app = _build_app_with_tight_embedder()
    with TestClient(app) as client:
        # Force two rows by bypassing write-time merge via different
        # texts that map to the same embedding.
        for text in ["fact A", "fact B paraphrase"]:
            r = client.post(
                "/api/v2/memory/v2/facts",
                json={"text": text, "kind": "project", "scope": "project"},
            )
            assert r.status_code == 200, r.text

        # NB: the tight embedder also makes write-time near-dup merge
        # fire, so we may end up with 1 row already. Either way the
        # endpoint should respond cleanly.
        r = client.post(
            "/api/v2/memory/v2/deduplicate", json={"dry_run": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ("scanned", "clusters_found", "merged", "dry_run"):
            assert key in body
        assert body["dry_run"] is True


def test_list_facts_hides_superseded_by_default() -> None:
    """GET /facts skips deduped tombstones unless include_superseded=true.

    Flow: POST two distinct facts → POST /deduplicate (clusters them
    via tight embedder) → confirm default GET /facts returns 1, and
    GET /facts?include_superseded=true returns both rows.
    """
    app = _build_app_with_tight_embedder()
    with TestClient(app) as client:
        # Use two distinct kinds so write-time near-dup merge can't
        # collapse them at POST time — then re-tag them in the second
        # step by running deduplicate() once we have rows landed.
        # Simpler path: same kind/scope, but write-time merge will
        # fire (tight embedder). To get 2 rows we POST two distinct
        # kinds first…
        client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "fact A", "kind": "project", "scope": "project"},
        )
        client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "fact B paraphrase",
                "kind": "preference",  # different kind → no merge
                "scope": "user",
            },
        )

        # Two rows landed.
        all_before = client.get(
            "/api/v2/memory/v2/facts?include_superseded=true",
        ).json()
        assert all_before["total"] == 2

        # Manually mark one as superseded via the underlying service
        # (the router exposes deduplicate() for the auto path; the
        # explicit supersede() is a service method without a route).
        svc = app.state.memory_v2_service
        ids = sorted(f["id"] for f in all_before["facts"])
        survivor_id, loser_id = ids[0], ids[1]
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            svc.supersede(
                old_fact_id=loser_id, new_fact_id=survivor_id,
            ),
        )

        # Default list hides the loser.
        r_default = client.get("/api/v2/memory/v2/facts")
        body_default = r_default.json()
        assert body_default["total"] == 1, (
            f"superseded loser leaked into default list: {body_default}"
        )
        assert body_default["facts"][0]["id"] == survivor_id

        # Opt-in surfaces both rows for debugging / dedup verification.
        r_all = client.get(
            "/api/v2/memory/v2/facts?include_superseded=true",
        )
        body_all = r_all.json()
        assert body_all["total"] == 2


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


# 2026-05-26: new curation endpoints — forget / restore / correct /
# dedup_scope. Mirrors the agent-tool surface so the UI can do
# everything the LLM can.


def test_forget_endpoint_soft_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app()
    with TestClient(app) as client:
        # Plant a fact.
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "user is 张伟", "kind": "identity", "scope": "user"},
        )
        assert r.status_code == 200
        fact_id = r.json()["created"]["id"]

        # Forget it.
        r = client.post(
            f"/api/v2/memory/v2/facts/{fact_id}/forget",
            json={"reason": "user clicked trash"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        # Default list hides it.
        r = client.get("/api/v2/memory/v2/facts")
        ids = {f["id"] for f in r.json()["facts"]}
        assert fact_id not in ids

        # include_superseded=true surfaces it with forgotten=true.
        r = client.get(
            "/api/v2/memory/v2/facts?include_superseded=true",
        )
        rows = [f for f in r.json()["facts"] if f["id"] == fact_id]
        assert len(rows) == 1
        assert rows[0]["forgotten"] is True
        assert rows[0]["superseded_by"] == "__forgotten__"


def test_forget_unknown_id_returns_ok_false() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts/does:not:exist/forget",
            json={},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False


def test_restore_endpoint_clears_supersede() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "test bullet", "kind": "preference", "scope": "user"},
        )
        fact_id = r.json()["created"]["id"]
        # Forget then restore.
        client.post(f"/api/v2/memory/v2/facts/{fact_id}/forget", json={})
        r = client.post(f"/api/v2/memory/v2/facts/{fact_id}/restore", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["fact"]["superseded_by"] in (None, "")
        # Should reappear in default list.
        r = client.get("/api/v2/memory/v2/facts")
        ids = {f["id"] for f in r.json()["facts"]}
        assert fact_id in ids


def test_restore_unknown_returns_ok_false() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts/does:not:exist/restore", json={},
        )
        assert r.json()["ok"] is False


def test_correct_endpoint_supersedes_match() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts",
            json={"text": "user is 张伟", "kind": "identity", "scope": "user"},
        )
        old_id = r.json()["created"]["id"]
        r = client.post(
            "/api/v2/memory/v2/facts/correct",
            json={
                "old_text": "user is 张伟",
                "new_text": "user is 何鹏",
                "kind": "identity", "scope": "user",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["matched"] is True
        assert body["old_fact_id"] == old_id
        assert body["new_fact_id"] != old_id


def test_correct_missing_args_returns_400() -> None:
    app = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/v2/facts/correct",
            json={"old_text": "", "new_text": "x"},
        )
        assert r.status_code == 400


def test_dedup_scope_dry_run() -> None:
    app = _build_app()
    with TestClient(app) as client:
        # Plant some facts in a bucket.
        for txt in ("a", "b"):
            client.post(
                "/api/v2/memory/v2/facts",
                json={
                    "text": txt, "kind": "preference",
                    "scope": "user", "bucket": "user_preference",
                },
            )
        r = client.post(
            "/api/v2/memory/v2/dedup_scope",
            json={
                "scope": "user", "bucket": "user_preference",
                "dry_run": True,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["scanned"] >= 2


def test_list_facts_includes_bucket_field() -> None:
    """B6: bucket was missing from the GET /facts response shape."""
    app = _build_app()
    with TestClient(app) as client:
        client.post(
            "/api/v2/memory/v2/facts",
            json={
                "text": "x", "kind": "preference", "scope": "user",
                "bucket": "user_preference",
            },
        )
        r = client.get("/api/v2/memory/v2/facts")
        facts = r.json()["facts"]
        assert facts, "no facts returned"
        assert "bucket" in facts[0]
        assert facts[0]["bucket"] == "user_preference"
        assert facts[0]["forgotten"] is False


# ── overview (Phase M2) ───────────────────────────────────────────


def test_overview_disabled_when_no_service() -> None:
    app = _build_app(with_service=False)
    with TestClient(app) as client:
        r = client.get("/api/v2/memory/v2/overview")
        assert r.status_code in (200, 503)


def test_overview_aggregates() -> None:
    app = _build_app()
    with TestClient(app) as client:
        client.post("/api/v2/memory/v2/facts",
                    json={"text": "用户喜欢简短回复", "kind": "preference", "scope": "user"})
        client.post("/api/v2/memory/v2/facts",
                    json={"text": "用户在做电商项目", "kind": "identity", "scope": "user"})
        r = client.get("/api/v2/memory/v2/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["total"] == 2
        assert body["by_kind"].get("preference") == 1
        assert body["by_kind"].get("identity") == 1
        assert body["by_scope"].get("user") == 2
        assert body["embedder_dim"] == 4
        assert isinstance(body["recent"], list) and len(body["recent"]) == 2
        # 干净库无矛盾/过期
        assert body["contradictions"] == 0
        assert body["stale"] == 0
