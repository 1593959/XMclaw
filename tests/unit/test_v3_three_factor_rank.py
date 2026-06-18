"""Phase 8 ⑦+⑪ — three-factor recall ranking + reinforcement.

Pins the Generative-Agents-style ranking (relevance + recency +
importance) and the MemoryBank recall-strengthens-recency effect.
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
from xmclaw.memory.v2.service import (
    RANK_RECENCY_HALFLIFE_S,
    REINFORCE_MIN_INTERVAL_S,
    _three_factor_score,
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


def _fact(text: str, *, conf: float, age_s: float) -> Fact:
    now = time.time()
    return Fact(
        id=Fact.compute_id(kind=FactKind.PROJECT, scope=FactScope.PROJECT, text=text),
        kind="project", scope="project", text=text,
        confidence=conf, ts_last=now - age_s,
    )


# ─── three-factor score (pure function) ───────────────────────────


def test_recency_decays_to_half_at_one_halflife():
    now = time.time()
    fresh = _fact("a", conf=0.0, age_s=0.0)
    old = _fact("b", conf=0.0, age_s=RANK_RECENCY_HALFLIFE_S)
    s_fresh = _three_factor_score(fresh, query_vec=None, query_norm=0.0, now=now)
    s_old = _three_factor_score(old, query_vec=None, query_norm=0.0, now=now)
    # conf=0 so score is pure recency: fresh≈1.0, one half-life≈0.5.
    assert s_fresh == pytest.approx(1.0, abs=0.02)
    assert s_old == pytest.approx(0.5, abs=0.02)


def test_importance_contributes_when_no_query():
    now = time.time()
    important = _fact("a", conf=0.9, age_s=0.0)
    trivial = _fact("b", conf=0.1, age_s=0.0)
    s_imp = _three_factor_score(important, query_vec=None, query_norm=0.0, now=now)
    s_triv = _three_factor_score(trivial, query_vec=None, query_norm=0.0, now=now)
    # Same recency, higher confidence ⇒ higher score.
    assert s_imp > s_triv


def test_fresh_strong_breaks_tie_over_stale_weak_at_equal_relevance():
    """⑦ + 乱召回 fix (RANK_W_RELEVANCE=3.0): relevance dominates — a
    perfectly-on-topic fact SHOULD beat a totally-irrelevant one even if
    the latter is fresher (that's the whole point of the 3× relevance
    weight). What recency + importance buy us is a TIE-BREAK *among
    comparably-relevant* facts: given two equally on-topic facts, the
    fresh high-confidence one wins. Pin that contract."""
    now = time.time()
    qvec = [1.0, 0.0, 0.0, 0.0]
    qnorm = 1.0
    # Both equally on-topic (same embedding aligned with the query).
    stale_weak = _fact("stale", conf=0.2, age_s=RANK_RECENCY_HALFLIFE_S * 4)
    stale_weak.embedding = (1.0, 0.0, 0.0, 0.0)
    fresh_strong = _fact("fresh", conf=0.95, age_s=0.0)
    fresh_strong.embedding = (1.0, 0.0, 0.0, 0.0)
    s_stale = _three_factor_score(stale_weak, query_vec=qvec, query_norm=qnorm, now=now)
    s_fresh = _three_factor_score(fresh_strong, query_vec=qvec, query_norm=qnorm, now=now)
    # Equal relevance (3.0 each); fresh wins on recency (≈1.0 vs ≈0.06)
    # + importance (0.95 vs 0.2).
    assert s_fresh > s_stale


def test_negative_cosine_clamped_to_zero():
    now = time.time()
    f = _fact("x", conf=0.0, age_s=RANK_RECENCY_HALFLIFE_S * 10)  # recency≈0
    f.embedding = (-1.0, 0.0, 0.0, 0.0)
    s = _three_factor_score(
        f, query_vec=[1.0, 0.0, 0.0, 0.0], query_norm=1.0, now=now,
    )
    # cosine = -1 but clamped to 0; recency≈0; conf 0 ⇒ score≈0, not negative.
    assert s >= 0.0
    assert s < 0.01


# ─── reinforcement (⑪) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reinforce_bumps_stale_fact_ts_last():
    svc = _make_service()
    f = await svc.remember(
        "重要事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    # Age it past the reinforce interval.
    f.ts_last = time.time() - (REINFORCE_MIN_INTERVAL_S + 100)
    await svc._vec.upsert([f])
    old_ts = (await svc.get_fact(f.id)).ts_last

    svc._reinforce_facts([await svc.get_fact(f.id)])
    # Fire-and-forget → let the scheduled task run.
    import asyncio
    await asyncio.sleep(0.05)

    new_ts = (await svc.get_fact(f.id)).ts_last
    assert new_ts > old_ts


@pytest.mark.asyncio
async def test_reinforce_skips_recently_touched():
    svc = _make_service()
    f = await svc.remember(
        "刚写的事实", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )  # ts_last = now → within interval
    before = (await svc.get_fact(f.id)).ts_last
    svc._reinforce_facts([await svc.get_fact(f.id)])
    import asyncio
    await asyncio.sleep(0.05)
    after = (await svc.get_fact(f.id)).ts_last
    # Too fresh to reinforce — no write amplification.
    assert after == before


@pytest.mark.asyncio
async def test_reinforce_empty_is_noop():
    svc = _make_service()
    svc._reinforce_facts([])  # must not raise


# ─── end-to-end: render_for_prompt uses the ranker ────────────────


@pytest.mark.asyncio
async def test_render_ranks_fresh_strong_first():
    svc = _make_service()
    # Two user-preference facts; one stale+weak, one fresh+strong.
    weak = await svc.remember(
        "弱偏好", kind=FactKind.PREFERENCE, scope=FactScope.USER,
        confidence=0.3,
    )
    weak.ts_last = time.time() - (RANK_RECENCY_HALFLIFE_S * 5)
    await svc._vec.upsert([weak])
    await svc.remember(
        "强偏好", kind=FactKind.PREFERENCE, scope=FactScope.USER,
        confidence=0.95,
    )
    block = await svc.render_for_prompt("", k=8)
    # Both present; strong appears before weak in the rendered text.
    assert "强偏好" in block and "弱偏好" in block
    assert block.index("强偏好") < block.index("弱偏好")
