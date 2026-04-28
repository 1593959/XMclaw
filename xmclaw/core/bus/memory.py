"""InProcessEventBus — asyncio-native pub/sub for Phase 1.

Real implementation (not stub): small enough to be correct on first write,
provides a working vehicle for the `xmclaw v2 ping` end-to-end demo.

Persistence and replay live in ``sqlite.py`` (``SqliteEventBus`` —
the real durable bus the daemon runs) and ``replay.py`` (the
``replay()`` async iterator the CLI's ``xmclaw replay <session>``
uses). Both are full implementations as of Phase 1; the original
"stubs for now" note is no longer accurate.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeAlias

from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.utils.log import get_logger

EventHandler: TypeAlias = Callable[[BehavioralEvent], Awaitable[None]]
EventPredicate: TypeAlias = Callable[[BehavioralEvent], bool]

_log = get_logger(__name__)


@dataclass
class Subscription:
    """Opaque handle returned by ``subscribe``; cancel to unsubscribe."""

    bus: "InProcessEventBus"
    predicate: EventPredicate
    handler: EventHandler
    _active: bool = field(default=True)

    def cancel(self) -> None:
        self._active = False


class InProcessEventBus:
    """Single-process pub/sub. Subscribers run in asyncio tasks, fan-out is
    parallel. Publish is best-effort — handler exceptions are logged but do
    not propagate back to the publisher (so a bad subscriber cannot halt
    the agent loop).
    """

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(
        self, predicate: EventPredicate, handler: EventHandler
    ) -> Subscription:
        sub = Subscription(bus=self, predicate=predicate, handler=handler)
        self._subs.append(sub)
        return sub

    async def publish(self, event: BehavioralEvent) -> None:
        for sub in list(self._subs):
            if not sub._active:
                continue
            try:
                if not sub.predicate(event):
                    continue
            except Exception:  # noqa: BLE001 — predicates must never crash publish
                continue
            task = asyncio.create_task(self._run_handler(sub, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_handler(self, sub: Subscription, event: BehavioralEvent) -> None:
        try:
            await sub.handler(event)
        except Exception as exc:  # noqa: BLE001 — isolate subscriber failures
            _log.warning(
                "bus.subscriber_failed",
                event_type=str(event.type),
                session_id=event.session_id,
                event_id=event.id,
                error=repr(exc),
            )

    async def drain(self) -> None:
        """Wait for all in-flight handler tasks to complete. Demo-time only."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def accept_all(_event: BehavioralEvent) -> bool:
    """Predicate that accepts every event. Convenience for simple subscribers."""
    return True
