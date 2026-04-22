"""Streaming Observer Bus — the v2 event backbone.

See ``docs/V2_DEVELOPMENT.md`` §1.2 for the design rationale.
"""
from xmclaw.core.bus.events import (
    BehavioralEvent,
    EventType,
    make_event,
)
from xmclaw.core.bus.memory import InProcessEventBus
from xmclaw.core.bus.sqlite import (
    SqliteEventBus,
    default_events_db_path,
    event_as_jsonable,
)

__all__ = [
    "BehavioralEvent",
    "EventType",
    "InProcessEventBus",
    "SqliteEventBus",
    "default_events_db_path",
    "event_as_jsonable",
    "make_event",
]
