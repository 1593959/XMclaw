"""Memory v3 phase 3.2 — BM25 keyword recall + hybrid fusion.

Two layers:

1. ``xmclaw.memory.v2.bm25`` — pure index + tokenizer (this file).
2. ``MemoryService.recall_hybrid`` — the fusion + filter wrapper.
   Lives at the service layer and is tested separately against
   InMemoryVectorBackend.

These tests do NOT require ``rank_bm25`` to be installed; they
skip the BM25-specific assertions when the package is missing and
verify the graceful-fallback contract instead.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from xmclaw.memory.v2 import bm25


# ─── Tokenizer ────────────────────────────────────────────────────


def test_tokenize_empty_returns_empty():
    assert bm25.tokenize_for_bm25("") == []
    assert bm25.tokenize_for_bm25(None) == []  # type: ignore[arg-type]


def test_tokenize_latin_words_lowercased():
    out = bm25.tokenize_for_bm25("Use FastAPI with PyDantic")
    assert "use" in out
    assert "fastapi" in out
    assert "pydantic" in out
    # Original case is dropped (normalisation, helps recall).
    assert "Use" not in out


def test_tokenize_chinese_chars_and_bigrams():
    out = bm25.tokenize_for_bm25("项目用 FastAPI")
    # Single chars present.
    assert "项" in out
    assert "目" in out
    # Bigram bridges the two — informative because "项目" disambiguates
    # from "项链" / "节目".
    assert "项目" in out
    # Latin word still picked up.
    assert "fastapi" in out


def test_tokenize_preserves_digit_runs():
    out = bm25.tokenize_for_bm25("FastAPI 0.115 on Python 3.12")
    assert "0" in out or "115" in out  # the regex captures digit runs
    assert "fastapi" in out
    assert "python" in out


def test_tokenize_mixed_chinese_english():
    out = bm25.tokenize_for_bm25("用户偏好用 PowerShell 不用 bash")
    # Chinese bigrams.
    assert "用户" in out
    assert "偏好" in out
    # English brands.
    assert "powershell" in out
    assert "bash" in out


# ─── Availability ─────────────────────────────────────────────────


def test_is_available_returns_bool_without_raising():
    """The probe must never bubble an ImportError up — callers rely
    on the boolean to branch."""
    out = bm25.is_available()
    assert isinstance(out, bool)


# ─── BM25Index (depends on rank_bm25 — skip when missing) ─────────


@dataclass
class _Fact:
    id: str
    text: str


@pytest.fixture
def sample_facts() -> list[_Fact]:
    return [
        _Fact("f1", "项目 myapp 用 FastAPI + pydantic v2"),
        _Fact("f2", "用户偏好简洁中文回复"),
        _Fact("f3", "Playwright select_option 接 string 时要 try value 再 label"),
        _Fact("f4", "永远不直接 push main 分支"),
        _Fact("f5", "数据库迁移走 alembic 不要直接改 schema"),
        _Fact("f6", "用户日常浏览器是 Edge"),
    ]


def test_index_skips_facts_without_id_or_text(sample_facts):
    """Defensive: ID-less / text-less rows in the LanceDB store
    shouldn't crash index construction."""
    extra = sample_facts + [
        _Fact("", "missing id"),
        _Fact("z", ""),
    ]
    idx = bm25.BM25Index(extra)
    assert len(idx.fact_ids) == len(sample_facts)


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_search_returns_relevant_fids_for_keyword_match(sample_facts):
    """The classic case BM25 fixes: exact keyword match on a rare
    identifier (here ``alembic``) that vector cosine often misses."""
    idx = bm25.BM25Index(sample_facts)
    hits = idx.search("alembic 迁移", k=3)
    fids = [fid for fid, _score in hits]
    assert "f5" in fids
    # Top hit's score is exactly 1.0 (normalised against itself).
    assert 0.99 <= hits[0][1] <= 1.01


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_search_chinese_query_hits_chinese_facts(sample_facts):
    """Bigram tokenization makes Chinese-only queries return Chinese
    facts even when no Latin overlap exists."""
    idx = bm25.BM25Index(sample_facts)
    hits = idx.search("用户偏好", k=5)
    fids = [fid for fid, _ in hits]
    assert "f2" in fids  # contains "用户偏好" directly


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_search_empty_query_returns_empty(sample_facts):
    idx = bm25.BM25Index(sample_facts)
    assert idx.search("", k=5) == []


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_search_no_match_returns_empty(sample_facts):
    """No token overlap → all scores zero → empty list."""
    idx = bm25.BM25Index(sample_facts)
    out = idx.search("xyzzy nonexistent gibberish", k=5)
    assert out == []


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_search_caps_at_k(sample_facts):
    idx = bm25.BM25Index(sample_facts)
    # Every fact mentions some Chinese char that overlaps with this
    # broad query — likely >2 hits.
    out = idx.search("用户 项目 不要 alembic", k=2)
    assert len(out) <= 2


@pytest.mark.skipif(
    not bm25.is_available(),
    reason="rank_bm25 not installed",
)
def test_score_is_normalised_to_unit_interval(sample_facts):
    """Top score should be 1.0; rest in [0, 1). Makes fusion in
    MemoryService.recall_hybrid arithmetically clean."""
    idx = bm25.BM25Index(sample_facts)
    out = idx.search("FastAPI pydantic", k=10)
    if not out:
        pytest.skip("no matches in this corpus for the query")
    assert 0.99 <= out[0][1] <= 1.01
    for _, s in out[1:]:
        assert 0.0 <= s < 1.0


# ─── Graceful-fallback contract ───────────────────────────────────


def test_index_search_returns_empty_when_bm25_missing(monkeypatch, sample_facts):
    """When rank_bm25 isn't installed, search() returns [] instead
    of crashing. MemoryService.recall_hybrid relies on this for
    graceful degradation to pure vector."""
    idx = bm25.BM25Index(sample_facts)
    # Force the "not available" path by stubbing _ensure_built.
    monkeypatch.setattr(idx, "_ensure_built", lambda: False)
    assert idx.search("anything", k=5) == []
