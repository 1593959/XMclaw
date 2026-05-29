"""Memory v3 phase 3.2 — keyword-side recall via BM25.

Pure-vector cosine recall is excellent for paraphrase / semantic
matches but mediocre on **rare named entities, code identifiers,
brand names, and Chinese** — exactly the cases users hit hardest.
OpenClaw's LanceDB Pro plugin documents the same finding (60/40
vector / BM25 fusion).

This module is the keyword-side path:

  - ``BM25Index`` maintains a per-process token index over all
    facts. The full LanceDB store is rescanned on demand (cheap
    when the store is <10K facts — typical XMclaw deployment).
  - ``tokenize_for_bm25`` does Chinese-friendly tokenization:
    bigrams over Chinese chars + lowercased latin words.
  - ``search`` returns the top-K facts by BM25 score plus a stable
    score in ``[0, 1]`` for downstream fusion with vector cosine.

The ``rank_bm25`` package is **optional** — if it's not installed,
``is_available`` returns False, ``search`` returns ``[]``, and the
hybrid layer in ``MemoryService.recall`` silently falls back to
pure-vector behaviour. This keeps Phase 3.2 a zero-cost addition
for users who haven't installed the extra.

Fusion lives in ``MemoryService.recall``, not here — this module
is intentionally just a scorer.
"""
from __future__ import annotations

import re
from typing import Any


# ─── Tokenization (Chinese + Latin) ───────────────────────────────


_LATIN_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-]*")
_DIGIT_RE = re.compile(r"\d+")


def _is_chinese_char(ch: str) -> bool:
    """Match CJK Unified Ideographs (covers Simplified + Traditional
    Chinese, Japanese kanji, Korean hanja)."""
    if not ch:
        return False
    return "一" <= ch <= "鿿"


def tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize a fact for BM25.

    Strategy:
      - Lowercased latin words (regex ``[a-zA-Z][\\w-]*``) → 1 token each
      - Standalone digit runs → 1 token each
      - Each Chinese char → 1 token AND each adjacent-pair bigram
        → 1 token (bigrams matter because single-char Chinese
        tokens dilute IDF — \"项\", \"目\" alone are too common, but
        \"项目\" is informative)

    Returns the bag-of-tokens list (order doesn't matter for BM25).
    """
    if not text:
        return []
    tokens: list[str] = []

    # 1) Latin words.
    for m in _LATIN_WORD_RE.finditer(text):
        tokens.append(m.group(0).lower())

    # 2) Digit runs (years, version numbers, etc.).
    for m in _DIGIT_RE.finditer(text):
        tokens.append(m.group(0))

    # 3) Chinese chars + bigrams.
    chinese_chars = [c for c in text if _is_chinese_char(c)]
    tokens.extend(chinese_chars)
    for i in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[i] + chinese_chars[i + 1])

    return tokens


# ─── Availability probe ───────────────────────────────────────────


_RANK_BM25_MISSING_LOGGED = False


def is_available() -> bool:
    """True iff ``rank_bm25`` is installed.

    Cached: the import attempt happens at most once per process —
    we don't want every recall to pay the ImportError overhead
    when the package is genuinely missing.
    """
    global _RANK_BM25_MISSING_LOGGED
    try:
        import rank_bm25  # noqa: F401
        return True
    except ImportError:
        if not _RANK_BM25_MISSING_LOGGED:
            _RANK_BM25_MISSING_LOGGED = True
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).info(
                    "bm25.unavailable rank_bm25 not installed; "
                    "hybrid recall falls back to pure vector. "
                    "Install with `pip install rank_bm25`.",
                )
            except Exception:  # noqa: BLE001
                pass
        return False


# ─── BM25 index ───────────────────────────────────────────────────


class BM25Index:
    """Build-on-demand BM25 index over a list of facts.

    The index is **stateless across MemoryService.recall calls** —
    we rebuild fresh on every call. Cost: O(N) where N = fact count
    in the store, dominated by tokenization (a few µs per fact).
    For < 10K facts the whole rebuild + score is < 50 ms; for
    bigger stores callers should swap to an LRU-cached incremental
    index.

    This trade-off is deliberate: zero invalidation logic, zero
    sync issues with LanceDB writes, simple code. If recall
    latency becomes a problem the swap is purely internal.
    """

    def __init__(self, facts: list[Any]):
        """``facts`` is an iterable of objects with ``id`` + ``text``.
        Stores parallel lists for fast positional lookup later."""
        self.fact_ids: list[str] = []
        self._corpus: list[list[str]] = []
        for f in facts:
            fid = getattr(f, "id", None)
            text = getattr(f, "text", "")
            if not fid or not text:
                continue
            self.fact_ids.append(str(fid))
            self._corpus.append(tokenize_for_bm25(text))
        self._bm25 = None  # built lazily

    def _ensure_built(self) -> bool:
        if self._bm25 is not None:
            return True
        if not self._corpus:
            return False
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return False
        try:
            self._bm25 = BM25Okapi(self._corpus)
        except Exception:  # noqa: BLE001 — defensive; empty corpus etc
            return False
        return True

    def search(
        self,
        query: str,
        k: int = 40,
    ) -> list[tuple[str, float]]:
        """Score every doc against ``query``, return top-K
        ``(fact_id, normalised_score)``.

        Normalisation: divide raw BM25 scores by the maximum
        observed in this query so the top hit is 1.0 and the rest
        are in ``[0, 1)``. Makes the fusion step's coefficient
        meaningful without re-normalising every time.

        Returns ``[]`` when:
          - rank_bm25 isn't installed
          - corpus is empty
          - query tokenization yields nothing
        """
        if not self._ensure_built():
            return []
        q_tokens = tokenize_for_bm25(query)
        if not q_tokens:
            return []
        try:
            scores = self._bm25.get_scores(q_tokens)
        except Exception:  # noqa: BLE001
            return []
        if len(scores) == 0:
            return []
        max_score = float(max(scores))
        if max_score <= 0:
            return []
        # Rank descending; cap at k.
        ranked: list[tuple[str, float]] = []
        for i, raw in enumerate(scores):
            if raw <= 0:
                continue
            ranked.append((self.fact_ids[i], float(raw) / max_score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:k]


__all__ = [
    "BM25Index",
    "tokenize_for_bm25",
    "is_available",
]
