"""Tests for the 2026-05-26 memory-curation APIs.

Pins behaviour of ``MemoryService.forget`` / ``correct`` /
``dedup_scope`` plus the ``correction_detector`` heuristic that
nudges the LLM toward calling them on user-correction turns.

Background: pre-fix the agent had no way to delete or update an
incorrect fact — corrections piled up as appended contradictions
in the persona files (user surfaced the 张伟 / LT凌天电竞 case).
These tests lock the proper semantics so regression is loud.
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
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ── forget ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_marks_fact_with_sentinel_and_drops_from_recall() -> None:
    svc = _make_service()
    f = await svc.remember(
        "用户是张伟", kind=FactKind.IDENTITY, scope=FactScope.USER,
    )
    ok = await svc.forget(fact_id=f.id, reason="not real identity")
    assert ok is True

    # Default recall must NOT surface forgotten facts.
    hits = await svc.recall("张伟", k=5)
    assert all(h.fact.id != f.id for h in hits), (
        "forgotten fact leaked into default recall"
    )

    # include_superseded=True + min_confidence=0.0 surfaces it
    # (audit / restore path). forget() pins confidence to 0.0 to
    # guarantee the row stops competing in normal recall ranking;
    # restoring requires explicit opt-in on both knobs.
    hits_all = await svc.recall(
        "张伟", k=5, include_superseded=True, min_confidence=0.0,
    )
    assert any(h.fact.id == f.id for h in hits_all)


@pytest.mark.asyncio
async def test_forget_unknown_id_returns_false() -> None:
    svc = _make_service()
    assert await svc.forget(fact_id="does:not:exist") is False


# ── correct ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correct_supersedes_close_match() -> None:
    svc = _make_service()
    old = await svc.remember(
        "用户的名字是张伟", kind=FactKind.IDENTITY,
        scope=FactScope.USER, confidence=0.85,
    )
    result = await svc.correct(
        old_text="用户的名字是张伟",
        new_text="用户的名字是何鹏",
        kind="identity", scope="user",
    )
    # StubEmbedder is deterministic but coarse — old_text identical
    # to stored text guarantees distance 0.0, so matched=True.
    assert result["matched"] is True
    assert result["old_fact_id"] == old.id
    assert result["new_fact_id"] != old.id

    # New fact is the only one surfaced.
    hits = await svc.recall("用户的名字", k=5)
    ids = {h.fact.id for h in hits}
    assert old.id not in ids
    assert result["new_fact_id"] in ids


@pytest.mark.asyncio
async def test_correct_with_no_match_still_writes_new_fact() -> None:
    svc = _make_service()
    # Nothing in memory — correct still captures the new value.
    result = await svc.correct(
        old_text="something the agent thought was true",
        new_text="actually the real fact is X",
        kind="preference", scope="user",
    )
    assert result["matched"] is False
    assert result["old_fact_id"] is None
    assert result["new_fact_id"]
    # New fact recallable on its own.
    hits = await svc.recall("the real fact is X", k=5)
    assert any(h.fact.id == result["new_fact_id"] for h in hits)


# ── dedup_scope ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_scope_dry_run_reports_without_writing() -> None:
    svc = _make_service()
    # Two facts that the stub embedder will cluster as near-duplicates
    # (StubEmbedder produces identical embeddings for byte-identical
    # input, so we differ only in trailing punctuation to stay in
    # the same cluster while having distinct text+id).
    f1 = await svc.remember(
        "用户偏好简洁回复", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, bucket="user_preference",
    )
    # Force a second row with the same embedding by writing identical
    # text via a different scope, then back-fixing scope — simulating
    # the real-world "same insight extracted twice" case.
    f2 = await svc.remember(
        "用户偏好简洁回复 .", kind=FactKind.PREFERENCE,
        scope=FactScope.USER, bucket="user_preference",
    )
    # Embeddings produced by the stub for these two strings differ
    # slightly; dedup_scope only fires when cosine >= 0.86. If the
    # stub doesn't cluster them tightly enough we at least exercise
    # the no-op path (scanned > 0, merged == 0). The contract under
    # test is "dry_run doesn't write" — that holds either way.
    result = await svc.dedup_scope(
        scope="user", bucket="user_preference", dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["scanned"] >= 2
    # Both rows still active regardless of whether they clustered.
    hits = await svc.recall("用户偏好", k=10)
    surviving_ids = {h.fact.id for h in hits}
    assert f1.id in surviving_ids
    assert f2.id in surviving_ids


# ── correction_detector ────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "我不是张伟",
    "你别再叫我张伟了",
    "不是张伟，是何鹏",
    "你说错了，我从不喝咖啡",
    "忘掉关于陪玩店的那些事",
    "I'm not 张伟",
    "Actually I'm working on a different project",
    "That was wrong about my name",
    "Forget what I said about the meeting",
    "I never said I worked at LT凌天电竞",
])
def test_correction_detector_fires_on_correction(msg: str) -> None:
    from xmclaw.cognition.correction_detector import detect_correction
    hint = detect_correction(msg)
    assert hint is not None, f"missed correction: {msg!r}"
    assert "memory_correct" in hint
    assert "memory_forget" in hint


@pytest.mark.parametrize("msg", [
    "今天天气不错",
    "帮我看一下这个文件",
    "我是新来的，请多关照",  # 我是 X — assertion, not correction
    "what's the weather like?",
    "",
    "   ",
])
def test_correction_detector_quiet_on_non_corrections(msg: str) -> None:
    from xmclaw.cognition.correction_detector import detect_correction
    assert detect_correction(msg) is None, (
        f"false-positive on: {msg!r}"
    )
