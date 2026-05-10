"""PerceptionBus — Jarvis Phase 6.1 unified percept entry.

A single, bounded, async percept queue with multiple producers and
typically a single consumer (AttentionFilter). Subscribers can also
tap the stream for observation (logging, metrics, debug UI), but they
MUST NOT make blocking decisions off it — the heartbeat / tick loop
drives the actual cognitive work.

When the buffer fills past ``max_buffer``, the **lowest-salience**
buffered percept is dropped (not FIFO), so recency is preserved and
high-importance signals survive. Drops are counted in ``stats()``.

This module is greenfield (Phase 6.1 foundation): nothing in the
existing daemon imports it yet — wiring (WS / file_watcher / cron /
internal-goal producers + the consumer task in AgentLoop) lands in a
follow-up commit. See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.1.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


PerceptSource = Literal[
    "ws", "file", "process", "time", "internal", "network",
    # R4 (2026-05-10) — multi-modal perception sources.
    "screen", "window", "clipboard", "calendar",
]


@dataclass(frozen=True, slots=True)
class Percept:
    """A unit of perception ingested by the cognitive loop.

    Producer-supplied. ``suggested_salience`` is the producer's
    self-rating in [0, 1]; AttentionFilter is free to override based
    on goals / fatigue / novelty. ``correlation_id`` lets downstream
    machinery thread a percept back to a session, plan, or goal.
    """

    id: str
    source: PerceptSource
    kind: str
    timestamp: float
    payload: dict[str, Any]
    suggested_salience: float | None = None
    correlation_id: str | None = None


_Subscriber = Callable[[Percept], Awaitable[None]]


@dataclass
class _SubscriberSlot:
    sub_id: str
    fn: _Subscriber


class PerceptionBus:
    """Single-entry async percept queue (multi-producer, single-consumer).

    The consumer (typically ``AttentionFilter.tick``) calls
    :meth:`drain` on each tick to atomically take the buffer. Producers
    call :meth:`push`. When the buffer is full, the lowest-salience
    percept is evicted to make room for the newcomer; this keeps
    recent + important percepts and prefers shedding stale low-value
    noise over freshly-arrived signals.

    Subscribers (registered via :meth:`subscribe`) are notified for
    every successful :meth:`push`, in registration order, but their
    exceptions are swallowed (one bad subscriber MUST NOT take the bus
    down). They are observers, not gatekeepers.
    """

    def __init__(self, max_buffer: int = 1024) -> None:
        if max_buffer < 1:
            raise ValueError("max_buffer must be >= 1")
        self._max_buffer = max_buffer
        self._buffer: list[Percept] = []
        self._lock = asyncio.Lock()
        self._subscribers: list[_SubscriberSlot] = []
        self._total_pushed = 0
        self._total_drained = 0
        self._total_dropped = 0

    @staticmethod
    def new_id() -> str:
        """Generate a fresh percept id (uuid4 hex). Helper for producers."""
        return uuid.uuid4().hex

    async def push(self, p: Percept) -> None:
        """Push a percept onto the bus.

        If the buffer is full, the lowest-salience buffered percept is
        evicted before the new one is appended. Subscribers are then
        notified out-of-band (their exceptions are logged, not raised).
        """
        async with self._lock:
            if len(self._buffer) >= self._max_buffer:
                self._evict_lowest_salience_locked()
            self._buffer.append(p)
            self._total_pushed += 1
            subs = list(self._subscribers)

        # Fire-and-log subscribers OUTSIDE the lock so a slow
        # subscriber can't stall producers.
        for slot in subs:
            try:
                await slot.fn(p)
            except Exception:
                logger.exception(
                    "PerceptionBus subscriber %s raised; swallowing",
                    slot.sub_id,
                )

    def _evict_lowest_salience_locked(self) -> None:
        """Drop the buffered percept with the lowest suggested_salience.

        Called under ``self._lock``. None is treated as 0.0 for
        ranking — producers that didn't self-rate are eligible for
        eviction first.
        """
        if not self._buffer:
            return

        def _rank(item: tuple[int, Percept]) -> tuple[float, float]:
            _, p = item
            sal = p.suggested_salience if p.suggested_salience is not None else 0.0
            # Tie-break: older percepts (smaller timestamp) evicted first.
            return (sal, p.timestamp)

        idx, victim = min(enumerate(self._buffer), key=_rank)
        del self._buffer[idx]
        self._total_dropped += 1
        logger.debug(
            "PerceptionBus dropped percept %s (kind=%s, suggested=%s) "
            "due to overflow (max_buffer=%d, total_dropped=%d)",
            victim.id,
            victim.kind,
            victim.suggested_salience,
            self._max_buffer,
            self._total_dropped,
        )

    async def drain(self) -> list[Percept]:
        """Atomically take and clear the current buffer.

        Returns percepts in insertion order. A no-op if empty.
        """
        async with self._lock:
            if not self._buffer:
                return []
            taken = self._buffer
            self._buffer = []
            self._total_drained += len(taken)
            return taken

    def subscribe(self, fn: _Subscriber) -> str:
        """Register an async observer for every pushed percept.

        Returns a subscription id usable with :meth:`unsubscribe`.
        Subscribers are NOT awaited under the bus lock; their
        exceptions are caught and logged.
        """
        sub_id = uuid.uuid4().hex
        self._subscribers.append(_SubscriberSlot(sub_id=sub_id, fn=fn))
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a previously-registered subscriber. No-op if unknown."""
        self._subscribers = [s for s in self._subscribers if s.sub_id != sub_id]

    def stats(self) -> dict[str, int]:
        """Return cumulative bus counters + current buffer size."""
        return {
            "buffered": len(self._buffer),
            "total_pushed": self._total_pushed,
            "total_drained": self._total_drained,
            "total_dropped": self._total_dropped,
            "subscribers": len(self._subscribers),
            "max_buffer": self._max_buffer,
        }


__all__ = ["Percept", "PerceptionBus", "PerceptSource"]