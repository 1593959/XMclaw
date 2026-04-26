"""Replay events from the persistent log.

Backs the ``xmclaw replay <session_id>`` CLI in §8.2.4 of
V2_DEVELOPMENT.md and offline grader/scheduler re-evaluation.

The implementation is a thin paginating wrapper over
:meth:`xmclaw.core.bus.sqlite.SqliteEventBus.query`. We lazily open
the bus against the default events.db (or an explicit path) so callers
don't have to manage its lifecycle themselves — the bus we open here
is closed when the async generator is exhausted.

``from_id`` is interpreted as the event id to **resume after** — it
seeks the wall-clock timestamp of that event in the log and yields
everything strictly after it. ``""`` (the conventional sentinel) means
"from the beginning". Unknown ids raise ``LookupError`` so callers
can distinguish "nothing yet" from "you mistyped".
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.bus.sqlite import SqliteEventBus, default_events_db_path


@dataclass
class EventFilter:
    session_id: str | None = None
    types: tuple[EventType, ...] | None = None


_DEFAULT_PAGE = 500


async def replay(
    from_id: str,
    filter_: EventFilter,
    *,
    db_path: Path | str | None = None,
    page_size: int = _DEFAULT_PAGE,
) -> AsyncIterator[BehavioralEvent]:
    """Yield events in the sqlite log matching ``filter_`` after ``from_id``.

    Args:
        from_id: Resume sentinel. ``""`` starts from the head of the log.
            Otherwise must be the id of an event present in the log;
            yielding starts strictly after it (chronologically).
        filter_: Optional ``session_id`` / ``types`` narrowing.
        db_path: Path to the events sqlite. ``None`` → the canonical
            ``~/.xmclaw/v2/events.db``.
        page_size: How many rows to fetch per round-trip. Tests pass
            small values to exercise the pagination path.

    Raises:
        LookupError: ``from_id`` is non-empty but absent from the log.
    """
    bus = SqliteEventBus(Path(db_path) if db_path is not None else default_events_db_path())
    try:
        since: float | None = None
        skip_first: str | None = None
        if from_id:
            # Find the event's timestamp so we can seek to it; we cannot
            # use rowid because BehavioralEvent doesn't expose it. Two
            # events can share a timestamp (microsecond ties on Windows),
            # so we filter the anchor id out of the first page below.
            matches = [e for e in bus.query(limit=10_000) if e.id == from_id]
            if not matches:
                raise LookupError(f"event {from_id!r} not found in log")
            since = matches[0].ts
            skip_first = from_id

        offset = 0
        while True:
            page = bus.query(
                session_id=filter_.session_id,
                since=since,
                types=filter_.types,
                limit=page_size,
                offset=offset,
            )
            if not page:
                return
            for event in page:
                if skip_first is not None and event.id == skip_first:
                    skip_first = None
                    continue
                yield event
            if len(page) < page_size:
                return
            offset += page_size
    finally:
        bus.close()
