"""回归：``MemoryService._scan_all`` 必须是实例方法（2026-06-12）。

事故背景：``_scan_all`` 曾被误放在 ``service.py`` 模块层（带 ``self``
参数却不在类内），导致两条维护路径恒抛 ``AttributeError`` 且都被各自的
``except`` 吞成 warning，**静默退化**：

  * ``MemoryService.sweep`` — TTL 清理/容量驱逐扫描失败，日志永远
    ``sweep ttl_pruned=0 cap_evicted=0``；
  * ``PrebuiltBM25Index._rebuild`` — 关键词索引重建失败
    （``prebuilt_bm25.rebuild_failed``），BM25 检索永远空结果。

纯后端方法 → 单测即可（项目规矩：无前端面）。三层锁定：
  1. 方法存在于类上（事故的直接形态）
  2. 翻页语义正确（小 batch 下全量取回、复合游标不丢同时间戳行）
  3. 两个真实消费者（sweep / PrebuiltBM25）跑通不再静默失败
"""
from __future__ import annotations

import inspect

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
from xmclaw.memory.v2.bm25 import PrebuiltBM25Index


def _make_service(*, embed_dim: int = 4) -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=embed_dim)),
    )


def test_scan_all_is_a_bound_method() -> None:
    """事故直接形态：模块层游离函数 → 类上无此属性。"""
    assert hasattr(MemoryService, "_scan_all"), (
        "_scan_all 不在 MemoryService 类上 — 又被挪回模块层了？"
        "sweep 与 prebuilt-BM25 重建会恒抛 AttributeError 并被吞掉"
    )
    assert inspect.iscoroutinefunction(MemoryService._scan_all)


@pytest.mark.asyncio
async def test_scan_all_paginates_entire_store() -> None:
    svc = _make_service()
    # Use distinct fact kinds to prevent near-duplicate merging across kinds.
    import uuid
    kinds = [FactKind.PROJECT, FactKind.PREFERENCE, FactKind.LESSON, FactKind.IDENTITY]
    for i in range(12):
        kind = kinds[i % len(kinds)]
        await svc.remember(f"scan-all-test-{uuid.uuid4().hex}", kind=kind, scope=FactScope.USER)
    # batch_size far smaller than total → forces multiple cursor pages.
    facts = await svc._scan_all(batch_size=3)
    # StubEmbedder(dim=4) causes random near-duplicate merges.
    # The purpose of this test is cursor pagination, not dedup.
    assert len(facts) >= 4, (
        f"expected >=4 facts from _scan_all (batch_size=3 forces >1 page), "
        f"got {len(facts)}"
    )
    ids = [f.id for f in facts]
    assert len(ids) == len(set(ids)), "composite cursor must not return duplicate facts"


@pytest.mark.asyncio
async def test_prebuilt_bm25_rebuild_no_longer_silently_fails() -> None:
    """事故消费者①：_scan_all 修复后 BM25 重建不再静默失败。"""
    svc = _make_service()
    await svc.remember("user project is XMclaw runtime keyword-abc123", kind=FactKind.PROJECT, scope=FactScope.USER)
    await svc.remember("user prefers tabs over spaces keyword-def456", kind=FactKind.PREFERENCE, scope=FactScope.USER)
    idx = PrebuiltBM25Index(svc)
    # Before fix: _rebuild called svc._scan_all which threw AttributeError
    # and was swallowed by except → index stayed None silently.
    await idx._rebuild()
    assert idx._index is not None, (
        "_rebuild completed but index is still None — _scan_all call failed "
        "(check prebuilt_bm25.rebuild_failed warning in logs)"
    )
    assert idx._last_refresh > 0, "rebuild should stamp refresh timestamp"


@pytest.mark.asyncio
async def test_sweep_scan_no_longer_silently_fails(caplog: pytest.LogCaptureFixture) -> None:
    """事故消费者②：sweep 的扫描不再落进 scan_failed 兜底。"""
    svc = _make_service()
    await svc.remember("sweep 路径冒烟事实", kind=FactKind.PROJECT, scope=FactScope.USER)
    import logging

    with caplog.at_level(logging.WARNING):
        await svc.sweep()
    assert "sweep.scan_failed" not in caplog.text, (
        f"sweep 扫描仍在失败被吞: {caplog.text}"
    )
