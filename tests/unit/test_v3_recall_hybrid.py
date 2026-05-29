"""Memory v3 phase 3.2 — ``MemoryService.recall_hybrid`` semantics.

The service-level fusion layer above ``bm25.BM25Index``. Tests pin:

  - Empty / whitespace query returns [].
  - When BM25 is unavailable, behaviour matches pure ``recall``.
  - Vector candidates ALWAYS survive into the result (so a
    paraphrase the user types still hits the canonical fact even
    when no keyword overlaps).
  - BM25 surfaces facts the vector path missed (so a rare
    identifier lookup still finds its target).
  - Filters (kinds / scopes / buckets / superseded) compose into
    both legs of the recall.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
    bm25,
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ─── Trivial guards ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_hybrid_empty_query_returns_empty():
    svc = _make_service()
    await svc.remember("any fact", kind="fact", scope="user")
    assert await svc.recall_hybrid("") == []
    assert await svc.recall_hybrid("   ") == []


@pytest.mark.asyncio
async def test_recall_hybrid_caps_at_k():
    svc = _make_service()
    for i in range(15):
        await svc.remember(
            f"fact number {i} alembic", kind="fact", scope="user",
        )
    out = await svc.recall_hybrid("alembic", k=4)
    assert len(out) <= 4


# ─── Filter pass-through ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_hybrid_respects_scope_filter():
    svc = _make_service()
    await svc.remember(
        "alembic project fact",
        kind="fact", scope="project", bucket="project_fact",
    )
    await svc.remember(
        "alembic user fact",
        kind="fact", scope="user", bucket="misc",
    )
    out = await svc.recall_hybrid("alembic", scopes=["project"], k=10)
    texts = {h.fact.text for h in out}
    assert "alembic project fact" in texts
    # User-scope fact filtered out.
    assert "alembic user fact" not in texts


@pytest.mark.asyncio
async def test_recall_hybrid_respects_bucket_filter():
    svc = _make_service()
    await svc.remember(
        "alembic A", kind="fact", scope="user", bucket="project_fact",
    )
    await svc.remember(
        "alembic B", kind="fact", scope="user", bucket="misc",
    )
    out = await svc.recall_hybrid(
        "alembic", buckets=["project_fact"], k=10,
    )
    texts = {h.fact.text for h in out}
    assert "alembic A" in texts
    assert "alembic B" not in texts


# ─── Vector path always represented ───────────────────────────────


@pytest.mark.asyncio
async def test_recall_hybrid_returns_something_when_facts_exist():
    """Whether or not BM25 is available, a query with at least one
    fact in scope should never return [] — vector hits are always
    surfaced. This is the lower bound that protects
    auto_recall.recall_for_message from regressing into "silent
    blank" mode."""
    svc = _make_service()
    await svc.remember(
        "FastAPI 项目用 pydantic v2", kind="fact", scope="user",
        bucket="project_fact",
    )
    await svc.remember(
        "用户偏好 Edge 浏览器", kind="preference", scope="user",
        bucket="user_preference",
    )
    # A query that vector cosine WILL hit on (semantic neighbour).
    out = await svc.recall_hybrid("项目用什么框架", k=5)
    assert len(out) >= 1


# ─── BM25 path actually fires when available ──────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
async def test_recall_hybrid_surfaces_keyword_match_vector_might_miss():
    """The killer case: user types a rare identifier (``alembic``)
    and expects to find the fact containing that exact token even
    if the embedding cosine ranks an unrelated paraphrase higher.

    With ``StubEmbedder(dim=4)`` the embedding signal is weak —
    BM25 is what actually pulls ``alembic`` to the top.
    """
    svc = _make_service()
    await svc.remember(
        "数据库迁移走 alembic 不要直接改 schema",
        kind="lesson", scope="project", bucket="rules",
    )
    # Distractors that share no Latin tokens with the query.
    await svc.remember(
        "用户喜欢简短回复", kind="preference", scope="user",
        bucket="user_preference",
    )
    await svc.remember(
        "项目部署到生产环境", kind="fact", scope="project",
        bucket="project_fact",
    )
    out = await svc.recall_hybrid("alembic", k=3)
    texts = [h.fact.text for h in out]
    assert any("alembic" in t for t in texts), (
        f"BM25 should have surfaced the alembic fact; got: {texts}"
    )


# ─── Graceful fallback when BM25 unavailable ──────────────────────


@pytest.mark.asyncio
async def test_recall_hybrid_falls_back_to_vector_when_bm25_missing(
    monkeypatch,
):
    """Force ``is_available`` False and verify recall_hybrid still
    returns vector hits (not crash, not empty). This is the
    contract the auto_recall module depends on."""
    svc = _make_service()
    await svc.remember(
        "FastAPI 用 pydantic v2", kind="fact", scope="user",
        bucket="project_fact",
    )

    monkeypatch.setattr(bm25, "is_available", lambda: False)
    out = await svc.recall_hybrid("项目框架", k=5)
    # Vector path still ran.
    assert len(out) >= 1
