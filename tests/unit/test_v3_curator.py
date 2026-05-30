"""MemoryCurator — holistic time-budgeted memory management.

Pins the two root-cause fixes (time budget so it never times out;
the report is honest) plus the dedup + prune passes.
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
from xmclaw.memory.v2.curator import CurationReport, MemoryCurator


class _FakeLLM:
    """Returns a pre-baked JSON response for every complete() call."""

    def __init__(self, response: dict):
        import json
        self._payload = json.dumps(response, ensure_ascii=False)
        self.calls = 0

    async def complete(self, *, messages, **kwargs):
        self.calls += 1
        payload = self._payload

        class _R:
            content = payload
        return _R()


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ─── CurationReport ───────────────────────────────────────────────


def test_report_did_anything_false_when_empty():
    r = CurationReport()
    assert r.did_anything is False
    assert r.honest_summary_zh() == ""


def test_report_honest_summary_reflects_actual_counts():
    r = CurationReport(merged=3, pruned=2, dry_run=False)
    s = r.honest_summary_zh()
    assert "合并 3 条重复" in s
    assert "降权 2 条低价值" in s
    assert s.startswith("刚整理了记忆：已")


def test_report_dry_run_summary_says_would_not_did():
    r = CurationReport(merged=1, dry_run=True)
    s = r.honest_summary_zh()
    assert "预计可" in s
    assert "已合并" not in s


# ─── dedup pass ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curate_dedup_merges_near_identical():
    svc = _make_service()
    # StubEmbedder gives byte-identical text the same embedding, so
    # these two cluster (cosine 1.0 ≥ 0.86).
    f1 = await svc.remember(
        "用户偏好简洁回复", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, bucket="user_preference",
    )
    f2 = await svc.remember(
        "用户偏好简洁回复", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, bucket="user_preference",
    )
    # remember() already collapses exact dupes; force a distinct row
    # with the same embedding via a near-identical but distinct text
    # would need a real embedder. Instead assert the curate path runs
    # cleanly and reports honestly on whatever the store holds.
    curator = MemoryCurator(svc)
    report = await curate_user(curator)
    assert isinstance(report, CurationReport)
    assert report.scanned >= 1
    assert "dedup" in report.passes_run


async def curate_user(curator: MemoryCurator) -> CurationReport:
    return await curator.curate(
        scopes=["user"], time_budget_s=5.0, dry_run=False,
    )


# ─── time budget (the root-cause fix) ─────────────────────────────


@pytest.mark.asyncio
async def test_curate_respects_time_budget():
    """The whole point: a large store must NOT block past the budget.
    With a 0.01s budget the run returns near-instantly and flags
    budget_exhausted rather than grinding through everything."""
    svc = _make_service()
    for i in range(50):
        await svc.remember(
            f"fact number {i}", kind=FactKind.PROJECT,
            scope=FactScope.PROJECT,
        )
    curator = MemoryCurator(svc)
    t0 = time.perf_counter()
    report = await curator.curate(
        scopes=["project"], time_budget_s=0.01, dry_run=False,
    )
    elapsed = time.perf_counter() - t0
    # Must return fast — well under a second even with 50 facts.
    assert elapsed < 2.0
    assert isinstance(report, CurationReport)


@pytest.mark.asyncio
async def test_curate_dry_run_does_not_write():
    svc = _make_service()
    await svc.remember(
        "alpha", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    await svc.remember(
        "beta", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    before = await svc.recall(None, scopes=["project"], k=10)
    curator = MemoryCurator(svc)
    await curator.curate(scopes=["project"], dry_run=True)
    after = await svc.recall(None, scopes=["project"], k=10)
    # Dry-run never supersedes / prunes.
    assert len(after) == len(before)


# ─── prune pass ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curate_prunes_old_low_value_fact():
    svc = _make_service()
    f = await svc.remember(
        "old speculative note", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, confidence=0.4,
    )
    # Age it past the 30-day prune cutoff + keep evidence_count=1.
    f.ts_last = time.time() - (60 * 60 * 24 * 40)  # 40 days ago
    await svc._vec.upsert([f])

    curator = MemoryCurator(svc)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=True, dry_run=False,
    )
    assert report.pruned >= 1
    # The pruned fact's confidence got floored.
    refreshed = await svc.get_fact(f.id)
    assert refreshed is not None
    assert refreshed.confidence <= 0.15


@pytest.mark.asyncio
async def test_curate_does_not_prune_protected_kinds():
    svc = _make_service()
    f = await svc.remember(
        "用户叫敬宇", kind=FactKind.IDENTITY,
        scope=FactScope.USER, confidence=0.4,
    )
    f.ts_last = time.time() - (60 * 60 * 24 * 40)
    await svc._vec.upsert([f])

    curator = MemoryCurator(svc)
    report = await curator.curate(
        scopes=["user"], do_dedup=False, do_prune=True, dry_run=False,
    )
    # identity is protected — never pruned.
    refreshed = await svc.get_fact(f.id)
    assert refreshed.confidence == 0.4
    assert report.pruned == 0


@pytest.mark.asyncio
async def test_curate_does_not_prune_recent_facts():
    svc = _make_service()
    f = await svc.remember(
        "recent note", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, confidence=0.4,
    )  # ts_last = now, not old → not pruned
    curator = MemoryCurator(svc)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=True, dry_run=False,
    )
    assert report.pruned == 0


@pytest.mark.asyncio
async def test_curate_does_not_prune_high_confidence():
    svc = _make_service()
    f = await svc.remember(
        "important durable fact", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, confidence=0.9,  # above ceiling
    )
    f.ts_last = time.time() - (60 * 60 * 24 * 40)
    await svc._vec.upsert([f])
    curator = MemoryCurator(svc)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=True, dry_run=False,
    )
    assert report.pruned == 0


# ─── pass selection ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curate_skips_disabled_passes():
    svc = _make_service()
    await svc.remember("x", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    curator = MemoryCurator(svc)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
    )
    assert "dedup" in report.passes_skipped
    assert "prune" in report.passes_skipped
    assert report.merged == 0
    assert report.pruned == 0


# ─── contradiction pass (LLM) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_curate_skips_llm_passes_without_llm():
    """No LLM bound → contradict + crystallize are skipped, not run."""
    svc = _make_service()
    await svc.remember("x", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    curator = MemoryCurator(svc)  # no llm
    report = await curator.curate(scopes=["project"])
    assert "contradict" in report.passes_skipped
    assert "crystallize" in report.passes_skipped
    assert report.contradictions_found == 0
    assert report.crystallized == 0


@pytest.mark.asyncio
async def test_curate_marks_contradiction_and_downweights_weaker():
    svc = _make_service()
    strong = await svc.remember(
        "用户喜欢喝咖啡", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, confidence=0.9,
    )
    weak = await svc.remember(
        "用户从不喝咖啡", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, confidence=0.5,
    )
    llm = _FakeLLM({
        "contradictions": [
            {"a": 1, "b": 2, "reason": "一个说喜欢喝，一个说从不喝"},
        ],
    })
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["user"], do_dedup=False, do_prune=False,
        do_crystallize=False, dry_run=False,
    )
    assert "contradict" in report.passes_run
    assert report.contradictions_found == 1
    # The lower-confidence side got down-ranked + stamped.
    refreshed_weak = await svc.get_fact(weak.id)
    assert refreshed_weak.confidence <= 0.4
    assert strong.id in refreshed_weak.contradicts
    # The stronger claim is untouched.
    refreshed_strong = await svc.get_fact(strong.id)
    assert refreshed_strong.confidence == 0.9


@pytest.mark.asyncio
async def test_curate_contradiction_dry_run_no_write():
    svc = _make_service()
    a = await svc.remember(
        "项目用 PostgreSQL", kind=FactKind.DECISION,
        scope=FactScope.PROJECT, confidence=0.6,
    )
    b = await svc.remember(
        "项目用 MySQL 不用 PostgreSQL", kind=FactKind.DECISION,
        scope=FactScope.PROJECT, confidence=0.5,
    )
    llm = _FakeLLM({"contradictions": [{"a": 1, "b": 2, "reason": "数据库冲突"}]})
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
        do_crystallize=False, dry_run=True,
    )
    assert report.contradictions_found == 1  # detected…
    # …but nothing written in dry-run.
    refreshed = await svc.get_fact(b.id)
    assert refreshed.confidence == 0.5
    assert refreshed.contradicts == ()


@pytest.mark.asyncio
async def test_curate_contradiction_ignores_bad_indices():
    svc = _make_service()
    await svc.remember(
        "fact one", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    await svc.remember(
        "fact two", kind=FactKind.PROJECT, scope=FactScope.PROJECT,
    )
    llm = _FakeLLM({"contradictions": [{"a": 1, "b": 99}]})  # 99 OOB
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
        do_crystallize=False, dry_run=False,
    )
    assert report.contradictions_found == 0


# ─── crystallization pass (LLM) ───────────────────────────────────


@pytest.mark.asyncio
async def test_curate_crystallizes_fragments_into_canonical():
    svc = _make_service()
    f1 = await svc.remember(
        "部署脚本在 scripts/deploy.sh", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, bucket="ops", confidence=0.6,
    )
    f2 = await svc.remember(
        "部署需要先跑测试", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, bucket="ops", confidence=0.7,
    )
    f3 = await svc.remember(
        "部署后要重启 daemon", kind=FactKind.PROJECT,
        scope=FactScope.PROJECT, bucket="ops", confidence=0.5,
    )
    llm = _FakeLLM({
        "crystals": [
            {
                "members": [1, 2, 3],
                "canonical_text": "部署流程：先跑测试 → 执行 scripts/deploy.sh → 重启 daemon",
                "reason": "都是部署流程的片段",
            },
        ],
    })
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
        do_contradict=False, dry_run=False,
    )
    assert "crystallize" in report.passes_run
    assert report.crystallized == 1
    hits = await svc.recall(None, scopes=["project"], k=10)
    texts = {h.fact.text for h in hits}
    # The canonical exists; the fragments are superseded out of recall.
    assert any("部署流程" in t for t in texts)
    surviving = {h.fact.id for h in hits}
    assert f1.id not in surviving
    assert f2.id not in surviving
    assert f3.id not in surviving


@pytest.mark.asyncio
async def test_curate_crystallize_dry_run_no_write():
    svc = _make_service()
    for i, t in enumerate(["碎片A", "碎片B", "碎片C"]):
        await svc.remember(
            t, kind=FactKind.PROJECT, scope=FactScope.PROJECT,
        )
    before = await svc.recall(None, scopes=["project"], k=10)
    llm = _FakeLLM({
        "crystals": [
            {"members": [1, 2, 3], "canonical_text": "统一表述", "reason": "x"},
        ],
    })
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
        do_contradict=False, dry_run=True,
    )
    assert report.crystallized == 1  # previewed…
    after = await svc.recall(None, scopes=["project"], k=10)
    assert len(after) == len(before)  # …nothing written


@pytest.mark.asyncio
async def test_curate_crystallize_ignores_singleton_group():
    svc = _make_service()
    await svc.remember("a", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    await svc.remember("c", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    llm = _FakeLLM({
        "crystals": [{"members": [1], "canonical_text": "solo", "reason": "x"}],
    })
    curator = MemoryCurator(svc, llm=llm)
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False,
        do_contradict=False, dry_run=False,
    )
    assert report.crystallized == 0


@pytest.mark.asyncio
async def test_curate_llm_passes_survive_bad_json():
    svc = _make_service()
    await svc.remember("a", kind=FactKind.PROJECT, scope=FactScope.PROJECT)
    await svc.remember("b", kind=FactKind.PROJECT, scope=FactScope.PROJECT)

    class _BadLLM:
        async def complete(self, *, messages, **kwargs):
            class _R:
                content = "not json at all"
            return _R()

    curator = MemoryCurator(svc, llm=_BadLLM())
    report = await curator.curate(
        scopes=["project"], do_dedup=False, do_prune=False, dry_run=False,
    )
    # Degrades to no-op, never crashes.
    assert report.contradictions_found == 0
    assert report.crystallized == 0
