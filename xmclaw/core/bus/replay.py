"""Replay events from the persistent log.

Phase 1: stub. Required for the `xmclaw replay <session_id>` CLI in §8.2.4
of V2_DEVELOPMENT.md and for offline grader/scheduler re-evaluation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from xmclaw.core.bus.events import BehavioralEvent, EventType


@dataclass
class EventFilter:
    session_id: str | None = None
    types: tuple[EventType, ...] | None = None


async def replay(from_id: str, filter_: EventFilter) -> AsyncIterator[BehavioralEvent]:
    """Yield events in the sqlite log matching ``filter_`` starting at ``from_id``.

    Phase 1.5 deliverable. Until then, raises NotImplementedError.
    """
    raise NotImplementedError("replay lands in Phase 1.5; see V2_DEVELOPMENT.md §8.2.4")
    # unreachable — make this a generator so callers can `async for`
    if False:  # pragma: no cover
        yield  # type: ignore[unreachable]
