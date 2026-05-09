"""Tests for Sprint 3 #6 — ReasoningBank-style strategy distillation.

Covers:

* ``make_strategy_id`` stability + collision resistance
* ``cap_confidence`` boundary handling (Iron Rule #2)
* :class:`StrategyDistiller` parsing, evidence floor, confidence cap,
  malformed JSON tolerance, empty-window short-circuit
* :class:`StrategyBank` put idempotency, retrieve top-K + last-touch,
  prune_unused age policy, evidence-floor rejection at the bank layer

All tests use a dict-backed fake vec store and a deterministic fake
embedder so no real ``sqlite_vec`` / network calls happen.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from xmclaw.core.journal.strategy_bank import StrategyBank
from xmclaw.core.journal.strategy_distiller import StrategyDistiller
from xmclaw.core.journal.strategy_types import (
    CONFIDENCE_CAP,
    MIN_EVIDENCE_COUNT,
    Strategy,
    cap_confidence,
    make_strategy_id,
)


# ── id + cap helpers ────────────────────────────────────────────────


def test_make_strategy_id_stable_across_calls() -> None:
    a = make_strategy_id("when X", "then Y")
    b = make_strategy_id("when X", "then Y")
    assert a == b
    # SHA256 hex digest is exactly 64 chars.
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_make_strategy_id_separator_prevents_ambiguity() -> None:
    """``when="ab" then="c"`` must not hash the same as ``when="a"
    then="bc"`` — guards the join-with-separator design choice."""
    a = make_strategy_id("ab", "c")
    b = make_strategy_id("a", "bc")
    assert a != b


def test_cap_confidence_above_cap() -> None:
    assert cap_confidence(0.95) == CONFIDENCE_CAP


def test_cap_confidence_within_range() -> None:
    assert cap_confidence(0.4) == 0.4


def test_cap_confidence_below_zero() -> None:
    assert cap_confidence(-0.1) == 0.0


def test_cap_confidence_far_above_cap() -> None:
    assert cap_confidence(1.5) == CONFIDENCE_CAP


# ── Distiller ───────────────────────────────────────────────────────


class _FakeLLM:
    """Records the prompt and replays a canned response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None
        self.calls: int = 0

    async def complete(self, prompt: str, *, session_id: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self.response


@pytest.mark.asyncio
async def test_distiller_drops_low_evidence_entries() -> None:
    """Iron Rule #1: an entry with evidence_count=1 must be filtered
    out even when the LLM emits it."""
    response = """[
      {"when_pattern": "good pattern",
       "then_action": "do A",
       "evidence_count": 2,
       "evidence_session_ids": ["s1", "s2"],
       "confidence": 0.5},
      {"when_pattern": "weak pattern",
       "then_action": "do B",
       "evidence_count": 1,
       "evidence_session_ids": ["s3"],
       "confidence": 0.5}
    ]"""
    distiller = StrategyDistiller(llm=_FakeLLM(response))
    strategies = await distiller.distill_from_journal(
        [{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}]
    )
    assert len(strategies) == 1
    assert strategies[0].when_pattern == "good pattern"


@pytest.mark.asyncio
async def test_distiller_handles_malformed_json_cleanly() -> None:
    """Bad JSON returns empty list, never raises."""
    distiller = StrategyDistiller(llm=_FakeLLM("not json at all"))
    strategies = await distiller.distill_from_journal([{"x": 1}])
    assert strategies == []


@pytest.mark.asyncio
async def test_distiller_caps_confidence_when_llm_returns_high() -> None:
    """Iron Rule #2: confidence is capped post-parse."""
    response = """[
      {"when_pattern": "p", "then_action": "a",
       "evidence_count": 2, "evidence_session_ids": ["s1", "s2"],
       "confidence": 0.95}
    ]"""
    distiller = StrategyDistiller(llm=_FakeLLM(response))
    strategies = await distiller.distill_from_journal([{"x": 1}])
    assert len(strategies) == 1
    assert strategies[0].confidence == CONFIDENCE_CAP


@pytest.mark.asyncio
async def test_distiller_empty_window_skips_llm_call() -> None:
    """Empty input short-circuits — no LLM round trip is fired."""
    fake = _FakeLLM("[]")
    distiller = StrategyDistiller(llm=fake)
    strategies = await distiller.distill_from_journal([])
    assert strategies == []
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_distiller_strips_markdown_fences() -> None:
    """Tolerant parser: strips ```json fences if the LLM wraps output."""
    response = """```json
[
  {"when_pattern": "p", "then_action": "a",
   "evidence_count": 2, "evidence_session_ids": ["s1", "s2"],
   "confidence": 0.5}
]
```"""
    distiller = StrategyDistiller(llm=_FakeLLM(response))
    strategies = await distiller.distill_from_journal([{"x": 1}])
    assert len(strategies) == 1


@pytest.mark.asyncio
async def test_distiller_respects_max_strategies_cap() -> None:
    """Even a flood of valid entries trims to ``max_strategies``."""
    items = [
        {
            "when_pattern": f"p{i}",
            "then_action": f"a{i}",
            "evidence_count": 2,
            "evidence_session_ids": [f"s{i}a", f"s{i}b"],
            "confidence": 0.3,
        }
        for i in range(12)
    ]
    import json as _json
    distiller = StrategyDistiller(
        llm=_FakeLLM(_json.dumps(items)), max_strategies=5,
    )
    strategies = await distiller.distill_from_journal([{"x": 1}])
    assert len(strategies) == 5


# ── StrategyBank: in-memory fakes ──────────────────────────────────


class _FakeEmbedder:
    """Maps each text to a deterministic 4-d vector based on hash."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hash(t)
            out.append([
                ((h >> 0) & 0xFF) / 255.0,
                ((h >> 8) & 0xFF) / 255.0,
                ((h >> 16) & 0xFF) / 255.0,
                ((h >> 24) & 0xFF) / 255.0,
            ])
        return out


class _FakeVecStore:
    """Dict-backed structural stand-in for SqliteVecMemory.

    Keys on ``MemoryItem.id``. ``query`` ignores the embedding (returns
    items in insertion order, filtered by ``filters['tag']`` if
    provided). That's enough to exercise StrategyBank's contract — the
    real cosine ranking is sqlite_vec's job and is tested elsewhere.
    """

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self.put_calls: int = 0

    async def put(self, layer: str, item: Any) -> str:
        self.put_calls += 1
        self.items[item.id] = item
        return item.id

    async def query(
        self,
        layer: str,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        wanted_tag = (filters or {}).get("tag")
        for item in self.items.values():
            if wanted_tag is not None and item.metadata.get("tag") != wanted_tag:
                continue
            results.append(item)
            if len(results) >= k:
                break
        return results

    async def forget(self, item_id: str) -> None:
        self.items.pop(item_id, None)


def _make_strategy(
    when: str = "w",
    then: str = "t",
    evidence_count: int = 2,
    evidence_ids: tuple[str, ...] = ("s1", "s2"),
    confidence: float = 0.5,
    distilled_at: float | None = None,
    last_retrieved_at: float | None = None,
) -> Strategy:
    return Strategy(
        id=make_strategy_id(when, then),
        when_pattern=when,
        then_action=then,
        evidence_count=evidence_count,
        evidence_session_ids=evidence_ids,
        confidence=confidence,
        distilled_at=distilled_at if distilled_at is not None else time.time(),
        last_retrieved_at=last_retrieved_at,
    )


@pytest.mark.asyncio
async def test_bank_put_is_idempotent_on_id() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    s = _make_strategy(when="when X", then="then Y")
    await bank.put(s)
    await bank.put(s)
    # Same id => same dict slot, only one entry.
    assert len(store.items) == 1
    # Both calls hit the underlying put — idempotency is at the *id*
    # level, not the call level.
    assert store.put_calls == 2


@pytest.mark.asyncio
async def test_bank_put_rejects_low_evidence() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    s = _make_strategy(
        evidence_count=1, evidence_ids=("s1",),
    )
    await bank.put(s)
    assert len(store.items) == 0


@pytest.mark.asyncio
async def test_bank_retrieve_returns_top_k() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    for i in range(5):
        await bank.put(
            _make_strategy(
                when=f"when {i}", then=f"then {i}",
                evidence_ids=(f"s{i}a", f"s{i}b"),
            )
        )
    out = await bank.retrieve("query", limit=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_bank_retrieve_zero_limit_returns_empty() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    await bank.put(_make_strategy())
    assert await bank.retrieve("q", limit=0) == []


@pytest.mark.asyncio
async def test_bank_retrieve_touches_last_retrieved_at() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    s = _make_strategy(last_retrieved_at=None)
    await bank.put(s)
    before = time.time()
    out = await bank.retrieve("query", limit=1)
    after = time.time()

    assert len(out) == 1
    touched = out[0]
    assert touched.last_retrieved_at is not None
    assert before <= touched.last_retrieved_at <= after

    # The store row was updated, not duplicated.
    assert len(store.items) == 1
    persisted = store.items[s.id].metadata
    assert persisted["last_retrieved_at"] is not None


@pytest.mark.asyncio
async def test_bank_prune_drops_old_never_retrieved() -> None:
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    old_distilled = time.time() - 10_000
    s = _make_strategy(
        when="old", then="strategy",
        distilled_at=old_distilled, last_retrieved_at=None,
    )
    await bank.put(s)
    removed = await bank.prune_unused(max_age_s=1_000)
    assert removed == 1
    assert s.id not in store.items


@pytest.mark.asyncio
async def test_bank_prune_keeps_recently_retrieved_regardless_of_age() -> None:
    """A strategy that was distilled long ago but retrieved recently
    must NOT be pruned — the whole point of last_retrieved_at."""
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    old_distilled = time.time() - 10_000
    s = _make_strategy(
        when="old", then="recently used",
        distilled_at=old_distilled,
        last_retrieved_at=time.time(),  # touched now
    )
    await bank.put(s)
    removed = await bank.prune_unused(max_age_s=1_000)
    assert removed == 0
    assert s.id in store.items


@pytest.mark.asyncio
async def test_bank_prune_keeps_young_unretrieved() -> None:
    """Young + never-retrieved is also kept (only old + never-retrieved
    is dropped)."""
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    s = _make_strategy(
        when="young", then="strategy",
        distilled_at=time.time(),  # just made
        last_retrieved_at=None,
    )
    await bank.put(s)
    removed = await bank.prune_unused(max_age_s=1_000)
    assert removed == 0
    assert s.id in store.items


@pytest.mark.asyncio
async def test_bank_put_only_persists_strategy_tagged_rows() -> None:
    """Sanity: the metadata written to the store carries the strategy
    tag so retrieve()'s filter can find it."""
    store = _FakeVecStore()
    bank = StrategyBank(store, _FakeEmbedder())
    s = _make_strategy()
    await bank.put(s)
    assert len(store.items) == 1
    persisted = next(iter(store.items.values()))
    assert persisted.metadata["tag"] == "strategy"


@pytest.mark.asyncio
async def test_min_evidence_count_constant_is_two() -> None:
    """Sanity-check the published constant — downstream callers (the
    distiller / bank) rely on its exact value (Iron Rule #1)."""
    assert MIN_EVIDENCE_COUNT == 2


@pytest.mark.asyncio
async def test_confidence_cap_constant_is_point_six() -> None:
    """Sanity-check the published constant — Iron Rule #2."""
    assert CONFIDENCE_CAP == 0.60
