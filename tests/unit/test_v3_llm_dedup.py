"""Semantic (LLM-driven) dedup that catches paraphrases cosine misses.

The classic gap: "空消息超过3轮停止分析" / "连续3次空消息后中止" /
"若3轮都是空消息则停" all mean the same thing but sit below the 0.86
cosine threshold ``dedup_scope`` uses, so they survive as duplicates.
``llm_dedup_scope`` asks an LLM to cluster by meaning.
"""
from __future__ import annotations

import json

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
    """Returns a pre-baked groups response. ``response`` is the JSON
    dict the LLM 'returns'."""

    def __init__(self, response: dict):
        self._response = response
        self.calls = 0

    async def complete(self, *, messages, **kwargs):
        self.calls += 1

        class _R:
            content = json.dumps(self._response, ensure_ascii=False)
        return _R()


# ─── no-LLM guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_dedup_without_llm_returns_clean_error():
    svc = _make_service()
    await svc.remember("x", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    r = await svc.llm_dedup_scope(scope="project")
    assert r["merged"] == 0
    assert "no llm" in r["error"]


@pytest.mark.asyncio
async def test_llm_dedup_single_fact_no_op():
    svc = _make_service()
    svc.set_llm(_FakeLLM({"groups": []}))
    await svc.remember("only one", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    r = await svc.llm_dedup_scope(scope="project")
    assert r["scanned"] == 1
    assert r["merged"] == 0


# ─── dry-run preview ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_dedup_dry_run_previews_without_writing():
    svc = _make_service()
    # Three paraphrases of the same rule.
    f1 = await svc.remember(
        "空消息超过3轮停止分析", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    f2 = await svc.remember(
        "连续3次空消息后中止分析", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    f3 = await svc.remember(
        "若3轮都是空消息则停止", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    # LLM says all 3 are the same, canonical = #1.
    svc.set_llm(_FakeLLM({
        "groups": [
            {"members": [1, 2, 3], "canonical": 1, "reason": "同一停止规则"},
        ],
    }))

    r = await svc.llm_dedup_scope(scope="project", dry_run=True)
    assert r["scanned"] == 3
    assert r["merge_groups"] == 1
    assert r["merged"] == 2  # 3 members, 1 survivor, 2 superseded
    assert r["dry_run"] is True
    assert r["method"] == "llm_semantic"

    # Nothing actually superseded in dry-run — all 3 still recallable.
    hits = await svc.recall(None, scopes=["project"], k=10)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_llm_dedup_commits_merges_when_not_dry_run():
    svc = _make_service()
    f1 = await svc.remember(
        "空消息超过3轮停止分析", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    f2 = await svc.remember(
        "连续3次空消息后中止分析", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    f3 = await svc.remember(
        "若3轮都是空消息则停止", kind=FactKind.LESSON,
        scope=FactScope.PROJECT, bucket="rules",
    )
    # The LLM numbers facts by their RECALL order (newest ts_last first),
    # NOT creation order — so pin distinct timestamps to make "canonical:1"
    # deterministically map to f1. Without this the back-to-back writes get
    # near-equal ts_last and the survivor is whichever the recall sort
    # happened to put first.
    for fid, ts in ((f1.id, 3000.0), (f2.id, 2000.0), (f3.id, 1000.0)):
        _f = await svc.get_fact(fid)
        _f.ts_last = ts
        await svc._vec.upsert([_f])
    svc.set_llm(_FakeLLM({
        "groups": [
            {"members": [1, 2, 3], "canonical": 1, "reason": "同一规则"},
        ],
    }))

    r = await svc.llm_dedup_scope(scope="project", dry_run=False)
    assert r["merged"] == 2

    # Only the canonical survives default recall.
    hits = await svc.recall(None, scopes=["project"], k=10)
    surviving_ids = {h.fact.id for h in hits}
    assert f1.id in surviving_ids
    assert f2.id not in surviving_ids
    assert f3.id not in surviving_ids


# ─── safety: malformed / conservative LLM output ──────────────────


@pytest.mark.asyncio
async def test_llm_dedup_ignores_singleton_group():
    """A 'group' with <2 members is meaningless — skip it."""
    svc = _make_service()
    await svc.remember("a", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    svc.set_llm(_FakeLLM({"groups": [{"members": [1], "canonical": 1}]}))
    r = await svc.llm_dedup_scope(scope="project", dry_run=True)
    assert r["merge_groups"] == 0
    assert r["merged"] == 0


@pytest.mark.asyncio
async def test_llm_dedup_ignores_out_of_range_indices():
    """LLM hallucinating an index past the batch must not crash or
    merge the wrong fact."""
    svc = _make_service()
    await svc.remember("a", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    # canonical=99 doesn't exist.
    svc.set_llm(_FakeLLM({
        "groups": [{"members": [1, 99], "canonical": 99}],
    }))
    r = await svc.llm_dedup_scope(scope="project", dry_run=True)
    # canonical out of range → group skipped.
    assert r["merge_groups"] == 0


@pytest.mark.asyncio
async def test_llm_dedup_survives_bad_json():
    """A model that returns non-JSON must degrade to 'no merges',
    never crash the dedup."""
    svc = _make_service()
    await svc.remember("a", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.LESSON, scope=FactScope.PROJECT)

    class _BadLLM:
        async def complete(self, *, messages, **kwargs):
            class _R:
                content = "I think these are different, sorry not JSON"
            return _R()

    svc.set_llm(_BadLLM())
    r = await svc.llm_dedup_scope(scope="project", dry_run=True)
    assert r["merged"] == 0
    assert r["merge_groups"] == 0


@pytest.mark.asyncio
async def test_llm_dedup_llm_arg_overrides_late_bound():
    """An explicit llm= arg takes priority over set_llm."""
    svc = _make_service()
    await svc.remember("a", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    bound = _FakeLLM({"groups": []})
    passed = _FakeLLM({
        "groups": [{"members": [1, 2], "canonical": 1, "reason": "x"}],
    })
    svc.set_llm(bound)
    r = await svc.llm_dedup_scope(scope="project", llm=passed, dry_run=True)
    # The passed LLM (which merges) was used, not the bound one.
    assert passed.calls == 1
    assert bound.calls == 0
    assert r["merge_groups"] == 1
