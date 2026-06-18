"""Phase 8 ⑩ — bi-temporal validity (Zep/Graphiti route).

Contradiction/supersession TIME-BOUNDS the loser (``invalid_at``)
instead of deleting it: hidden from default recall, kept for history.
"""
from __future__ import annotations

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
from xmclaw.memory.v2.models import Fact


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ─── model round-trip ─────────────────────────────────────────────


def test_fact_roundtrips_temporal_fields():
    f = Fact(
        id="x", kind="preference", scope="user", text="t",
        valid_at=1000.0, invalid_at=2000.0,
    )
    d = f.to_dict()
    assert d["valid_at"] == 1000.0
    assert d["invalid_at"] == 2000.0
    f2 = Fact.from_dict(d)
    assert f2.valid_at == 1000.0
    assert f2.invalid_at == 2000.0


def test_fact_temporal_defaults_none():
    f = Fact(id="x", kind="preference", scope="user", text="t")
    assert f.valid_at is None
    assert f.invalid_at is None
    # 0.0 sentinel (legacy / lance) deserialises back to None.
    f2 = Fact.from_dict({
        "id": "y", "kind": "preference", "scope": "user", "text": "t",
        "valid_at": 0.0, "invalid_at": 0.0,
    })
    assert f2.valid_at is None
    assert f2.invalid_at is None


# ─── recall filters invalidated ───────────────────────────────────


@pytest.mark.asyncio
async def test_recall_hides_invalidated_by_default():
    svc = _make_service()
    f = await svc.remember(
        "用户喜欢咖啡", kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    # Invalidate it as of one hour ago.
    f.invalid_at = time.time() - 3600
    await svc._vec.upsert([f])

    default_hits = await svc.recall(None, scopes=["user"], k=10)
    assert all(h.fact.id != f.id for h in default_hits)

    # …but it's still on disk and retrievable for history.
    with_inv = await svc.recall(
        None, scopes=["user"], k=10, include_invalidated=True,
    )
    assert any(h.fact.id == f.id for h in with_inv)


@pytest.mark.asyncio
async def test_recall_keeps_future_invalid_at():
    """A fact whose invalid_at is in the FUTURE is still valid now."""
    svc = _make_service()
    f = await svc.remember(
        "限时优惠进行中", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    f.invalid_at = time.time() + 86400  # expires tomorrow
    await svc._vec.upsert([f])
    hits = await svc.recall(None, scopes=["project"], k=10)
    assert any(h.fact.id == f.id for h in hits)


# ─── supersede stamps invalid_at ──────────────────────────────────


@pytest.mark.asyncio
async def test_supersede_sets_invalid_at():
    svc = _make_service()
    old = await svc.remember(
        "旧事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    new = await svc.remember(
        "新事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    await svc.supersede(old_fact_id=old.id, new_fact_id=new.id)
    refreshed = await svc.get_fact(old.id)
    assert refreshed.invalid_at is not None
    assert refreshed.invalid_at <= time.time()
    # Hidden from default recall via BOTH superseded_by and invalid_at.
    hits = await svc.recall(None, scopes=["project"], k=10)
    assert all(h.fact.id != old.id for h in hits)


# ─── curator contradiction invalidates the older fact ─────────────


@pytest.mark.asyncio
async def test_curator_contradiction_invalidates_older():
    from xmclaw.memory.v2.curator import MemoryCurator

    class _FakeLLM:
        def __init__(self, response: dict):
            import json
            self._payload = json.dumps(response, ensure_ascii=False)

        async def complete(self, *, messages, **kwargs):
            payload = self._payload

            class _R:
                content = payload
            return _R()

    svc = _make_service()
    older = await svc.remember(
        "用户住在北京", kind=FactKind.IDENTITY, scope=FactScope.USER,
        confidence=0.8,
    )
    older.ts_last = time.time() - 100000  # clearly older
    await svc._vec.upsert([older])
    newer = await svc.remember(
        "用户住在上海", kind=FactKind.IDENTITY, scope=FactScope.USER,
        confidence=0.6,
    )
    # recall order isn't guaranteed; LLM references them by index, so
    # fetch the rendered order the curator will see.
    hits = await svc.recall(
        None, scopes=["user"], k=10, min_confidence=0.0,
    )
    idx = {h.fact.id: i + 1 for i, h in enumerate(hits)}
    llm = _FakeLLM({
        "contradictions": [
            {"a": idx[older.id], "b": idx[newer.id], "reason": "城市冲突"},
        ],
    })
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["user"], do_dedup=False, do_prune=False,
        do_crystallize=False, dry_run=False,
        # The LLM passes are gated behind a "≥N changed facts" cost guard
        # (default 10); this 2-fact test exercises the contradiction logic
        # itself, so force the gate open.
        min_changes_for_llm=0,
    )
    assert report.contradictions_found == 1
    # The OLDER fact (北京) is invalidated; the newer (上海) wins.
    r_old = await svc.get_fact(older.id)
    r_new = await svc.get_fact(newer.id)
    assert r_old.invalid_at is not None and r_old.invalid_at <= time.time()
    assert r_new.invalid_at is None
    # Default recall now surfaces 上海, not 北京.
    visible = await svc.recall(None, scopes=["user"], k=10)
    ids = {h.fact.id for h in visible}
    assert newer.id in ids
    assert older.id not in ids
