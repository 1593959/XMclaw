"""Token-bucket rate limiter for channel adapters (P1-8, audit 2026-06-11).

Prevents a single channel (飞书/钉钉/Telegram/Slack) from overwhelming
the agent loop with inbound messages or exhausting API rate budgets on
outbound calls.

Usage per adapter::

    from xmclaw.providers.channel.rate_limiter import RateLimiter
    _limiter = RateLimiter(max_per_second=5, burst=10)
    if not _limiter.acquire(channel="feishu", user_id="ou_xxx"):
        return  # drop or queue
"""
from __future__ import annotations

import time
from collections import defaultdict

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class RateLimiter:
    """Simple token-bucket limiter. Thread-safe for single-threaded asyncio."""

    def __init__(
        self,
        max_per_second: float = 5.0,
        burst: int = 10,
    ) -> None:
        self._rate = max_per_second
        self._burst = burst
        self._buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(burst, max_per_second))

    def acquire(self, *, channel: str, user_id: str = "", key: str = "") -> bool:
        """Try to acquire one token. Returns True if within limit."""
        k = f"{channel}:{user_id or key}" if (user_id or key) else channel
        return self._buckets[k].try_consume()

    def remaining(self, channel: str) -> int:
        return max(0, int(self._buckets[channel].tokens))

    def reset(self, channel: str) -> None:
        self._buckets[channel] = _Bucket(self._burst, self._rate)


class _Bucket:
    __slots__ = ("tokens", "last_fill", "burst", "rate")

    def __init__(self, burst: int, rate: float) -> None:
        self.tokens = float(burst)
        self.burst = burst
        self.rate = rate
        self.last_fill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_fill
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
        self.last_fill = now

    def try_consume(self) -> bool:
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# Global singleton for channel adapters to share
_channel_limiter: RateLimiter | None = None


def get_channel_limiter() -> RateLimiter:
    global _channel_limiter
    if _channel_limiter is None:
        _channel_limiter = RateLimiter()
    return _channel_limiter
