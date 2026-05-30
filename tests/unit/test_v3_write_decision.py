"""Phase 8 ⑨ — write-time memory decision (Mem0 route).

remember_with_decision() decides ADD / UPDATE / DELETE / NOOP against
the new fact's nearest neighbours AT WRITE TIME, instead of inserting
blindly and cleaning up in an offline sweep.
"""
from __future__ import annotations

import json
import time

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


class _FakeLLM:
    def __init__(self, response: dict):
        self._payload = json.dumps(response, ensure_ascii=False)
        self.calls = 0

    async def complete(self, *, messages, **kwargs):
        self.calls += 1
        payload = self._payload

        class _R:
            content = payload
        return _R()


# ─── safe fallbacks ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_llm_falls_back_to_plain_add():
    svc = _make_service()  # no llm
    r = await svc.remember_with_decision(
        "全新事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    assert r["action"] == "ADD"
    assert r["fact"] is not None
    assert r["reason"] == "no_llm_or_embedder"


@pytest.mark.asyncio
async def test_no_close_neighbour_skips_llm():
    svc = _make_service()
    llm = _FakeLLM({"action": "NOOP"})  # would NOOP if consulted
    svc.set_llm(llm)
    # Empty store → no neighbour → pure ADD, LLM never called.
    r = await svc.remember_with_decision(
        "孤立的新事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    assert r["action"] == "ADD"
    assert r["reason"] == "no_related_neighbour"
    assert llm.calls == 0


# ─── the four operations ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_evidence_votes_existing():
    svc = _make_service()
    # StubEmbedder: identical text → identical embedding → distance 0.
    existing = await svc.remember(
        "用户偏好简洁回复", kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    ev0 = existing.evidence_count
    svc.set_llm(_FakeLLM({"action": "NOOP", "target": 1, "reason": "已知"}))
    r = await svc.remember_with_decision(
        "用户偏好简洁回复", kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    assert r["action"] == "NOOP"
    assert r["fact"].id == existing.id
    # Evidence-voted, not duplicated.
    refreshed = await svc.get_fact(existing.id)
    assert refreshed.evidence_count == ev0 + 1
    all_user = await svc.recall(None, scopes=["user"], k=10)
    assert len(all_user) == 1  # no new row


@pytest.mark.asyncio
async def test_update_merges_and_supersedes():
    svc = _make_service()
    old = await svc.remember(
        "用户在做电商", kind=FactKind.IDENTITY, scope=FactScope.USER,
        confidence=0.6,
    )
    svc.set_llm(_FakeLLM({
        "action": "UPDATE", "target": 1,
        "merged_text": "用户在做跨境电商,主营服装",
        "reason": "新信息更完整",
    }))
    r = await svc.remember_with_decision(
        "用户做跨境电商卖服装", kind=FactKind.IDENTITY, scope=FactScope.USER,
        relate_distance=2.0,  # force LLM path regardless of stub embed
    )
    assert r["action"] == "UPDATE"
    assert "跨境电商" in r["fact"].text
    # Old fact superseded → gone from default recall; merged one present.
    hits = await svc.recall(None, scopes=["user"], k=10)
    texts = {h.fact.text for h in hits}
    assert "用户在做电商" not in texts
    assert any("跨境电商" in t for t in texts)
    refreshed_old = await svc.get_fact(old.id)
    assert refreshed_old.superseded_by is not None


@pytest.mark.asyncio
async def test_delete_invalidates_contradicted_neighbour():
    svc = _make_service()
    old = await svc.remember(
        "用户住在北京", kind=FactKind.IDENTITY, scope=FactScope.USER,
    )
    svc.set_llm(_FakeLLM({
        "action": "DELETE", "target": 1, "reason": "城市变了",
    }))
    r = await svc.remember_with_decision(
        "用户搬到了上海", kind=FactKind.IDENTITY, scope=FactScope.USER,
        relate_distance=2.0,
    )
    assert r["action"] == "DELETE"
    # New fact written; old one time-failed (kept for history).
    refreshed_old = await svc.get_fact(old.id)
    assert refreshed_old.invalid_at is not None
    assert refreshed_old.invalid_at <= time.time()
    # Default recall shows the new fact, not the contradicted old one.
    hits = await svc.recall(None, scopes=["user"], k=10)
    ids = {h.fact.id for h in hits}
    assert r["fact"].id in ids
    assert old.id not in ids


@pytest.mark.asyncio
async def test_add_when_llm_says_add():
    svc = _make_service()
    await svc.remember(
        "用户喜欢咖啡", kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    svc.set_llm(_FakeLLM({"action": "ADD", "target": None, "reason": "新偏好"}))
    # A semantically-distinct fact (StubEmbedder keys on text) — but to
    # force the LLM path we need a close neighbour; use near-identical
    # text so it's "related", and let the LLM still choose ADD.
    r = await svc.remember_with_decision(
        "用户喜欢咖啡吗", kind=FactKind.PREFERENCE, scope=FactScope.USER,
        relate_distance=2.0,
    )
    assert r["action"] == "ADD"
    assert r["fact"] is not None


@pytest.mark.asyncio
async def test_bad_json_falls_back_to_add():
    svc = _make_service()
    await svc.remember(
        "邻居事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )

    class _BadLLM:
        async def complete(self, *, messages, **kwargs):
            class _R:
                content = "not json"
            return _R()

    svc.set_llm(_BadLLM())
    r = await svc.remember_with_decision(
        "邻居事实相关", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
        relate_distance=2.0,
    )
    # Degrades to a safe ADD, never raises.
    assert r["action"] == "ADD"
    assert r["fact"] is not None
