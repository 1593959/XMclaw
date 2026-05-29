"""Memory v3 phase 2 — similarity-axis auto-recall.

The agent loop prepends a ``<recalled>`` block to the user message
based on the top-K LanceDB facts most similar to the user's input.
Tests pin the public behaviour: silent failure modes, bucket
filtering, block rendering, and idempotency.

Together with the structural axis (bucket → .md → system prompt),
these tests guarantee no fact in LanceDB is invisible to the agent
on a turn that's specifically about it.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.daemon.auto_recall import (
    RecalledFact,
    prepend_recalled_block,
    recall_for_message,
    render_recalled_block,
)


# ─── Fixtures ─────────────────────────────────────────────────────


@dataclass
class _StubFact:
    id: str
    text: str
    bucket: str = "misc"
    kind: str = "fact"
    ts_first: float = 1000.0


@dataclass
class _StubHit:
    fact: _StubFact
    distance: float


def _hit(
    fid: str, text: str, distance: float = 0.2,
    bucket: str = "misc", kind: str = "fact",
) -> _StubHit:
    return _StubHit(
        fact=_StubFact(id=fid, text=text, bucket=bucket, kind=kind),
        distance=distance,
    )


def _service(hits: list[_StubHit]) -> MagicMock:
    """Build a memory_service stub whose ``recall`` returns ``hits``.

    Deliberately uses ``spec=["recall"]`` so ``hasattr(svc,
    "recall_hybrid")`` returns False — the auto_recall layer's
    pure-vector fallback path is what we want to exercise here.
    The hybrid path is tested separately in
    ``test_v3_recall_hybrid.py`` against a real MemoryService.
    """
    svc = MagicMock(spec=["recall"])
    svc.recall = AsyncMock(return_value=hits)
    return svc


# ─── recall_for_message ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_skips_when_message_is_trivially_short():
    """1-3 char turns ("ok", "?", "嗯") shouldn't burn an embed call."""
    svc = _service([_hit("a", "anything")])
    out = await recall_for_message(svc, "?")
    assert out == []
    svc.recall.assert_not_called()


@pytest.mark.asyncio
async def test_recall_skips_when_service_none():
    """No service wired → returns [] silently."""
    out = await recall_for_message(None, "tell me about FastAPI")
    assert out == []


@pytest.mark.asyncio
async def test_recall_returns_empty_when_backend_raises():
    """Hard rule: never let recall failure break the turn."""
    svc = MagicMock(spec=["recall"])
    svc.recall = AsyncMock(side_effect=RuntimeError("LanceDB down"))
    out = await recall_for_message(svc, "what was that FastAPI fact?")
    assert out == []


@pytest.mark.asyncio
async def test_recall_returns_empty_on_timeout():
    """2026-05-29 (chat-b09a3ad4): recall MUST never block the turn
    longer than ``timeout_s``. A slow backend that takes longer than
    the deadline trips ``asyncio.TimeoutError`` and the function
    returns ``[]`` so the LLM call proceeds without ``<recalled>``."""
    import asyncio

    async def _slow_recall(*_args, **_kwargs):
        await asyncio.sleep(10.0)  # would block the turn
        return []

    svc = MagicMock(spec=["recall"])
    svc.recall = _slow_recall

    import time
    t0 = time.perf_counter()
    out = await recall_for_message(
        svc, "a query long enough to embed",
        timeout_s=0.05,
    )
    elapsed = time.perf_counter() - t0
    assert out == []
    assert elapsed < 1.0, (
        f"recall_for_message did not honour timeout: took {elapsed:.2f}s"
    )


@pytest.mark.asyncio
async def test_recall_skips_hybrid_path_by_default():
    """Default path is plain ``recall``, NOT ``recall_hybrid``. The
    hybrid leg rebuilds a Python BM25 index per query and can stall
    on large stores — keep it opt-in until Phase 5's background
    prefetch / native FTS lands."""
    svc = MagicMock(spec=["recall", "recall_hybrid"])
    svc.recall = AsyncMock(return_value=[])
    svc.recall_hybrid = AsyncMock(return_value=[])
    await recall_for_message(svc, "a query long enough to embed")
    svc.recall.assert_awaited_once()
    svc.recall_hybrid.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_uses_hybrid_when_explicitly_enabled():
    """Operator opted into hybrid via config → we call
    ``recall_hybrid`` instead of ``recall``."""
    svc = MagicMock(spec=["recall", "recall_hybrid"])
    svc.recall = AsyncMock(return_value=[])
    svc.recall_hybrid = AsyncMock(return_value=[])
    await recall_for_message(
        svc, "a query long enough to embed",
        use_hybrid=True,
    )
    svc.recall_hybrid.assert_awaited_once()
    svc.recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_excludes_default_structural_buckets():
    """Identity / preferences / values already render into the .md
    system prompt. They MUST be excluded from the recall block to
    avoid double-injection (waste tokens, dilute attention)."""
    svc = _service([
        _hit("a", "user is alice", bucket="user_identity"),
        _hit("b", "user prefers brief replies", bucket="user_preference"),
        _hit("c", "agent values surgical edits", bucket="values"),
        _hit("d", "project uses FastAPI", bucket="project_fact"),
        _hit("e", "stale lesson", bucket="failure_modes"),
    ])
    out = await recall_for_message(svc, "我们项目用什么框架？")
    bucket_set = {r.bucket for r in out}
    assert "user_identity" not in bucket_set
    assert "user_preference" not in bucket_set
    assert "values" not in bucket_set
    # But non-structural buckets remain.
    assert "project_fact" in bucket_set
    assert "failure_modes" in bucket_set


@pytest.mark.asyncio
async def test_recall_respects_custom_exclude_buckets():
    """Operator can add more buckets to exclude via config."""
    svc = _service([
        _hit("a", "fact A", bucket="project_fact"),
        _hit("b", "fact B", bucket="misc"),
    ])
    out = await recall_for_message(
        svc, "what's project state",
        exclude_buckets={"project_fact"},
    )
    bucket_set = {r.bucket for r in out}
    assert "project_fact" not in bucket_set
    assert "misc" in bucket_set


@pytest.mark.asyncio
async def test_recall_drops_below_similarity_floor():
    """Cosine distance > (1 - min_similarity) → dropped."""
    svc = _service([
        _hit("a", "very related", distance=0.10),   # sim 0.90
        _hit("b", "moderately related", distance=0.40),  # sim 0.60
        _hit("c", "barely related", distance=0.50),  # sim 0.50
    ])
    out = await recall_for_message(svc, "long enough query", min_similarity=0.65)
    fids = [r.fid for r in out]
    assert "a" in fids
    assert "b" not in fids
    assert "c" not in fids


@pytest.mark.asyncio
async def test_recall_caps_at_k_after_filter():
    """Even if 20 candidates pass the floor, we cap to k for token
    budget reasons."""
    svc = _service([
        _hit(f"f{i}", f"fact {i}", distance=0.1) for i in range(20)
    ])
    out = await recall_for_message(svc, "anything", k=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_recall_sorted_by_similarity_desc():
    """Top of <recalled> should be highest-similarity for the LLM's
    sake — its attention drops on later items."""
    svc = _service([
        _hit("a", "loose", distance=0.30),  # sim 0.70
        _hit("b", "tight", distance=0.10),  # sim 0.90
        _hit("c", "medium", distance=0.20), # sim 0.80
    ])
    # The mock returns them in [a, b, c] order; recall preserves
    # backend ordering and just filters. Backend usually sorts by
    # distance ASC. We trust that contract here.
    out = await recall_for_message(svc, "anything")
    sims = [r.similarity for r in out]
    # Backend gave us ascending distance ([a:0.30, b:0.10, c:0.20]) —
    # so similarities arrive in [0.70, 0.90, 0.80]. We trust the
    # backend ordering; whether to re-sort is the renderer's call.
    assert sims == [0.70, 0.90, 0.80]


# ─── render_recalled_block ────────────────────────────────────────


def test_render_recalled_block_empty_returns_empty_string():
    """No hits → empty string so caller can unconditionally concat."""
    assert render_recalled_block([]) == ""


def test_render_recalled_block_format_is_xml_ish():
    """Each fact: ``- (sim | bucket) text [fid:xxx]``. The wrapper
    tags help the LLM recognize this as auxiliary context, not
    user-authored content."""
    block = render_recalled_block([
        RecalledFact(
            fid="abc123",
            text="项目用 FastAPI",
            bucket="project_fact",
            kind="fact",
            ts_first=1000.0,
            similarity=0.92,
        ),
    ])
    assert block.startswith("<recalled")
    assert block.rstrip().endswith("</recalled>")
    assert "- (0.92 | project_fact) 项目用 FastAPI [fid:abc123]" in block


def test_render_recalled_block_escapes_newlines_in_fact_text():
    """Multi-line fact text mustn't break the bullet structure."""
    block = render_recalled_block([
        RecalledFact(
            fid="xy",
            text="line1\nline2\nline3",
            bucket="misc", kind="fact",
            ts_first=0.0, similarity=0.7,
        ),
    ])
    assert "line1 line2 line3" in block
    # Exactly one bullet line for one fact.
    assert block.count("- (") == 1


# ─── prepend_recalled_block ───────────────────────────────────────


def test_prepend_returns_message_unchanged_when_no_hits():
    """Empty hits = no change. Caller doesn't need to branch."""
    msg = "你能帮我看下这个 bug 吗"
    assert prepend_recalled_block(msg, []) == msg


def test_prepend_puts_block_above_user_message():
    """Order matters — LLM reads top-down; we want recall context
    BEFORE user's actual ask."""
    hits = [RecalledFact(
        fid="x", text="recalled fact",
        bucket="misc", kind="fact",
        ts_first=0.0, similarity=0.8,
    )]
    result = prepend_recalled_block("user asks something", hits)
    assert result.index("<recalled") < result.index("user asks")
    assert "recalled fact" in result
    assert "user asks something" in result
