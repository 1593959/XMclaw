"""Cache-hit-rate aggregator — P1-1 Phase 1.

Subscribes to ``COST_TICK`` events and maintains per-session running
totals so the dashboard / UI can show cache efficiency without
recomputing from the full event log.

Background
==========

Pre-fix: ``COST_TICK`` emits per-hop raw numbers
(``cache_creation_input_tokens``, ``cache_read_input_tokens``) but
there is no aggregation. The dashboard can only show "this hop used
N tokens" — not "this session saved M tokens via caching".

This module adds a lightweight in-memory aggregator. It is NOT
persistent (daemon restart resets counters) because the primary use
case is live-session feedback, not long-term billing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.core.bus import BehavioralEvent, EventType, InProcessEventBus


@dataclass
class _SessionMetrics:
    """Running totals for one session."""

    total_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    completion_tokens: int = 0
    hop_count: int = 0
    first_ts: float = field(default_factory=time.time)
    last_ts: float = field(default_factory=time.time)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input tokens that were cache reads."""
        if self.total_input_tokens <= 0:
            return 0.0
        return self.cache_read_tokens / self.total_input_tokens

    @property
    def tokens_saved_vs_nocache(self) -> int:
        """Estimated tokens saved compared to no caching at all.

        Uses a 90%% discount heuristic: cache-read tokens cost ~10%%
        of regular input tokens on Anthropic / Kimi / GLM.
        """
        return int(self.cache_read_tokens * 0.9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input_tokens": self.total_input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "completion_tokens": self.completion_tokens,
            "hop_count": self.hop_count,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "tokens_saved": self.tokens_saved_vs_nocache,
            "duration_s": round(self.last_ts - self.first_ts, 1),
        }


class CacheMetricsAggregator:
    """In-memory aggregator for prompt-cache efficiency metrics.

    Usage::

        aggregator = CacheMetricsAggregator(bus)
        # aggregator auto-subscribes to COST_TICK on construction
        ...
        metrics = aggregator.get_session_metrics(session_id)
    """

    # Sessions idle longer than this are purged on the next tick.
    _SESSION_TTL_S: float = 3600.0  # 1 hour

    def __init__(self, bus: InProcessEventBus | None = None) -> None:
        self._sessions: dict[str, _SessionMetrics] = {}
        self._bus = bus
        if bus is not None:
            bus.subscribe(EventType.COST_TICK, self._on_cost_tick)

    def _on_cost_tick(self, event: BehavioralEvent) -> None:
        """Event handler — wired to the bus."""
        payload = event.payload or {}
        session_id = event.session_id
        if not session_id:
            return

        m = self._sessions.setdefault(session_id, _SessionMetrics())
        m.hop_count += 1
        m.last_ts = time.time()

        prompt = int(payload.get("prompt_tokens", 0) or 0)
        completion = int(payload.get("completion_tokens", 0) or 0)
        cache_creation = int(payload.get("cache_creation_input_tokens", 0) or 0)
        cache_read = int(payload.get("cache_read_input_tokens", 0) or 0)

        m.total_input_tokens += prompt
        m.completion_tokens += completion
        m.cache_creation_tokens += cache_creation
        m.cache_read_tokens += cache_read

    def get_session_metrics(self, session_id: str) -> dict[str, Any] | None:
        """Return running totals for ``session_id``, or None if unknown."""
        m = self._sessions.get(session_id)
        return m.to_dict() if m is not None else None

    def get_all_sessions(self) -> dict[str, dict[str, Any]]:
        """Return metrics for every known session."""
        return {sid: m.to_dict() for sid, m in self._sessions.items()}

    def clear_session(self, session_id: str) -> bool:
        """Drop a session's metrics (e.g. on session destroy)."""
        return self._sessions.pop(session_id, None) is not None

    def purge_stale(self) -> int:
        """Remove sessions idle longer than ``_SESSION_TTL_S``.

        Returns number of sessions removed.
        """
        cutoff = time.time() - self._SESSION_TTL_S
        stale = [sid for sid, m in self._sessions.items() if m.last_ts < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
        return len(stale)

    def build_summary_payload(
        self,
        session_id: str,
        provider: str = "unknown",
    ) -> dict[str, Any] | None:
        """Build the payload for a ``CACHE_METRICS_SUMMARY`` event."""
        m = self._sessions.get(session_id)
        if m is None:
            return None
        return {
            "session_id": session_id,
            "provider": provider,
            "hop_count": m.hop_count,
            "cache_hit_rate": round(m.cache_hit_rate, 4),
            "tokens_saved": m.tokens_saved_vs_nocache,
            "total_input_tokens": m.total_input_tokens,
            "cache_read_tokens": m.cache_read_tokens,
            "cache_creation_tokens": m.cache_creation_tokens,
            "duration_s": round(m.last_ts - m.first_ts, 1),
        }


__all__ = ["CacheMetricsAggregator"]
