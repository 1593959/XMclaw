"""Prompt cache hit-rate tracking — production observability.

Reference: Anthropic Prompt Caching docs (2024/2025)
           Claude Code 5-stage compaction (2026)

Tracks per-session and aggregate cache metrics:
  - cache_read_input_tokens (90% cost savings when hitting)
  - cache_creation_input_tokens (1.25× cost on first write)
  - uncached_input_tokens (tokens after the last breakpoint)

Exposes a global singleton that agent_loop can read for
INNER_MONOLOGUE events and the observability dashboard.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CacheMetrics:
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    model: str = ""
    provider: str = ""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_tokens: int = 0
    total_input_tokens: int = 0
    # Derived
    hit_rate: float = 0.0       # cache_read / total_input
    savings_usd: float = 0.0    # estimated cost saved

    def compute(self) -> "CacheMetrics":
        self.total_input_tokens = (
            self.cache_read_tokens + self.cache_write_tokens + self.uncached_tokens
        )
        if self.total_input_tokens > 0:
            self.hit_rate = self.cache_read_tokens / self.total_input_tokens
        return self


@dataclass
class CacheStatsSnapshot:
    """Rolling aggregate across all sessions."""
    total_requests: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_uncached: int = 0
    last_minute_requests: int = 0
    last_minute_cache_read: int = 0

    @property
    def overall_hit_rate(self) -> float:
        total = self.total_cache_read + self.total_cache_write + self.total_uncached
        return self.total_cache_read / total if total > 0 else 0.0

    @property
    def estimated_savings(self) -> float:
        # Saved = 0.90 * cache_read (90% discount on cached tokens)
        # vs would-have-cost = cache_read * 1.0 (full price without cache)
        # Assumes ~$3/Mtok input price (Sonnet 4.6)
        return self.total_cache_read * 0.90 * 3.0 / 1_000_000


class CacheStatsTracker:
    """Thread-safe rolling cache hit-rate tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = CacheStatsSnapshot()
        self._recent: list[tuple[float, int, int]] = []  # (ts, read, write)

    def record(self, metrics: CacheMetrics) -> None:
        metrics.compute()
        with self._lock:
            self._snapshot.total_requests += 1
            self._snapshot.total_cache_read += metrics.cache_read_tokens
            self._snapshot.total_cache_write += metrics.cache_write_tokens
            self._snapshot.total_uncached += metrics.uncached_tokens
            now = time.time()
            self._recent.append((now, metrics.cache_read_tokens, metrics.cache_write_tokens))
            # Prune entries older than 60 seconds
            cutoff = now - 60
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.pop(0)
            self._snapshot.last_minute_requests = len(self._recent)
            self._snapshot.last_minute_cache_read = sum(r[1] for r in self._recent)

    def snapshot(self) -> CacheStatsSnapshot:
        with self._lock:
            from dataclasses import replace
            return replace(self._snapshot)

    def fingerprint(self, text: str, max_len: int = 120) -> str:
        """Stable hash of cacheable prefix content for drift detection."""
        import hashlib
        trimmed = text[:max_len].encode("utf-8")
        return hashlib.sha256(trimmed).hexdigest()[:16]


# Global singleton
_cache_tracker = CacheStatsTracker()


def get_cache_tracker() -> CacheStatsTracker:
    return _cache_tracker
