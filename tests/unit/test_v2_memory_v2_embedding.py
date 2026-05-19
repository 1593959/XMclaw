"""Phase 1b — EmbeddingService unit tests.

Covers: LRU cache hit/miss accounting, retry-with-backoff on
transient failures, deterministic stub embedder for downstream
fixtures, and the "all or none" batch contract.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2.embedding import (
    EmbeddingFailure,
    EmbeddingService,
    StubEmbedder,
)


# ── Happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_single_text() -> None:
    svc = EmbeddingService(StubEmbedder(dim=8))
    vec = await svc.embed("hello")
    assert isinstance(vec, tuple)
    assert len(vec) == 8
    assert svc.cache_misses == 1
    assert svc.cache_hits == 0


@pytest.mark.asyncio
async def test_embed_same_text_twice_hits_cache() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4))
    v1 = await svc.embed("X")
    v2 = await svc.embed("X")
    assert v1 == v2
    assert svc.cache_misses == 1
    assert svc.cache_hits == 1


@pytest.mark.asyncio
async def test_embed_batch_returns_correct_order() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4))
    vs = await svc.embed_batch(["a", "b", "c"])
    assert len(vs) == 3
    # Each must equal the single-embed result.
    va = await svc.embed("a")
    assert vs[0] == va


@pytest.mark.asyncio
async def test_batch_mixed_cache_miss_and_hit() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4))
    # Pre-warm cache for 'a'.
    await svc.embed("a")
    misses_before = svc.cache_misses
    hits_before = svc.cache_hits
    # Batch: a (hit) + b (miss) + c (miss)
    vs = await svc.embed_batch(["a", "b", "c"])
    assert len(vs) == 3
    assert svc.cache_hits == hits_before + 1
    assert svc.cache_misses == misses_before + 2


# ── Cache eviction ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_lru_eviction() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4), cache_capacity=2)
    await svc.embed("a")
    await svc.embed("b")
    await svc.embed("c")  # should evict 'a' (oldest)
    # Re-embedding 'a' → miss again.
    misses_before = svc.cache_misses
    await svc.embed("a")
    assert svc.cache_misses == misses_before + 1


@pytest.mark.asyncio
async def test_disabled_cache() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4), cache_capacity=0)
    await svc.embed("X")
    await svc.embed("X")
    # Both misses — cache disabled.
    assert svc.cache_misses == 2
    assert svc.cache_hits == 0


# ── Whitespace normalisation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_whitespace_collapses_to_same_cache_entry() -> None:
    """Same content with different whitespace ⇒ one cache slot."""
    svc = EmbeddingService(StubEmbedder(dim=4))
    v1 = await svc.embed("hello world")
    v2 = await svc.embed("hello   world")
    assert v1 == v2
    assert svc.cache_misses == 1
    assert svc.cache_hits == 1


# ── Empty input ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_text_raises() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4))
    with pytest.raises(EmbeddingFailure):
        await svc.embed("")
    with pytest.raises(EmbeddingFailure):
        await svc.embed("   ")


# ── Retry ─────────────────────────────────────────────────────────


class _FlakyEmbedder:
    """Fails N times, then succeeds. Tests retry semantics."""

    name = "flaky"
    dim = 4

    def __init__(self, fail_first_n: int) -> None:
        self.fail_first_n = fail_first_n
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.fail_first_n:
            raise RuntimeError(f"flaky fail #{self.calls}")
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def is_available(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_failure() -> None:
    backend = _FlakyEmbedder(fail_first_n=2)
    svc = EmbeddingService(
        backend, retry_attempts=3, retry_backoff_s=0.0,
    )
    vec = await svc.embed("hello")
    assert vec == (0.1, 0.2, 0.3, 0.4)
    assert backend.calls == 3  # 2 failures + 1 success
    assert svc.failures == 0


@pytest.mark.asyncio
async def test_retry_gives_up_after_exhaustion() -> None:
    backend = _FlakyEmbedder(fail_first_n=999)  # never succeeds
    svc = EmbeddingService(
        backend, retry_attempts=2, retry_backoff_s=0.0,
    )
    with pytest.raises(EmbeddingFailure):
        await svc.embed("hello")
    assert backend.calls == 2
    assert svc.failures == 1


# ── Empty-row contract ────────────────────────────────────────────


class _PartialEmptyEmbedder:
    """Returns empty vec for one of N texts. Should trigger retry
    (we treat 'all or none' as the contract)."""

    name = "partial"
    dim = 4

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            # First call: second text empty.
            return [
                [1.0, 0, 0, 0] if i != 1 else []
                for i in range(len(texts))
            ]
        # Subsequent: all valid.
        return [[1.0, 0, 0, 0] for _ in texts]


@pytest.mark.asyncio
async def test_batch_with_empty_row_retries() -> None:
    backend = _PartialEmptyEmbedder()
    svc = EmbeddingService(
        backend, retry_attempts=3, retry_backoff_s=0.0,
    )
    vs = await svc.embed_batch(["a", "b"])
    assert len(vs) == 2
    assert backend.calls == 2  # first batch had empty row → retried


# ── Stats ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_reports_hit_rate() -> None:
    svc = EmbeddingService(StubEmbedder(dim=4))
    await svc.embed("a")
    await svc.embed("a")
    await svc.embed("b")
    s = svc.stats()
    assert s["cache_hits"] == 1
    assert s["cache_misses"] == 2
    assert s["cache_hit_rate"] == pytest.approx(1 / 3)
    assert s["provider"] == "stub"
    assert s["dim"] == 4


# ── Epic #27 sweep #8 (2026-05-19): circuit breaker ────────────────


class _AlwaysFailingEmbedder:
    """Embedder that raises on every call — simulates a 502-storm
    upstream provider. The breaker should clamp down."""

    def __init__(self) -> None:
        self.name = "always_fail"
        self.dim = 4
        self.calls = 0

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise RuntimeError("upstream 502")


@pytest.mark.asyncio
async def test_cb_opens_after_threshold_consecutive_failures() -> None:
    """After ``circuit_breaker_threshold`` consecutive failed
    ``_embed_with_retry`` calls, the breaker opens — subsequent
    calls raise immediately without hitting the provider."""
    from xmclaw.memory.v2.embedding import EmbeddingFailure

    backend = _AlwaysFailingEmbedder()
    svc = EmbeddingService(
        backend,
        retry_attempts=2,
        retry_backoff_s=0.0,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown_s=300.0,
    )
    for _ in range(3):
        with pytest.raises(EmbeddingFailure):
            await svc.embed("text")
    expected_calls_before_open = 3 * 2  # 3 cycles × 2 attempts each
    assert backend.calls == expected_calls_before_open

    # Next call should NOT hit the provider — breaker is open.
    with pytest.raises(EmbeddingFailure, match="circuit breaker OPEN"):
        await svc.embed("more text")
    assert backend.calls == expected_calls_before_open  # no new call


@pytest.mark.asyncio
async def test_cb_resets_on_successful_call() -> None:
    """A success between failures resets the counter — the breaker
    should NOT open if failures are interleaved with successes."""
    from xmclaw.memory.v2.embedding import EmbeddingFailure

    class _FlakyEmbedder:
        def __init__(self) -> None:
            self.name = "flaky"
            self.dim = 4
            self.call = 0
        def is_available(self) -> bool:
            return True
        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.call += 1
            if self.call % 3 == 0:
                return [[1.0, 0, 0, 0] for _ in texts]
            raise RuntimeError("flake")

    backend = _FlakyEmbedder()
    svc = EmbeddingService(
        backend,
        retry_attempts=1,
        retry_backoff_s=0.0,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown_s=300.0,
    )
    for i in range(6):
        try:
            await svc.embed(f"text-{i}")
        except EmbeddingFailure:
            pass
    s = svc.stats()
    assert s["circuit_breaker_open"] is False


@pytest.mark.asyncio
async def test_cb_state_in_stats() -> None:
    """``stats()`` exposes the breaker so observability + UI can
    surface "embedder unhealthy" without grepping daemon.log."""
    from xmclaw.memory.v2.embedding import EmbeddingFailure

    backend = _AlwaysFailingEmbedder()
    svc = EmbeddingService(
        backend,
        retry_attempts=1,
        retry_backoff_s=0.0,
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_s=300.0,
    )
    for _ in range(2):
        with pytest.raises(EmbeddingFailure):
            await svc.embed("x")
    s = svc.stats()
    assert s["circuit_breaker_open"] is True
    assert s["circuit_breaker_cooldown_remaining_s"] > 0
    assert s["circuit_breaker_consecutive_failures"] == 2
