"""Streaming Observer Bus — the v2 event backbone.

See ``docs/V2_DEVELOPMENT.md`` §1.2 for the design rationale.
"""
from xmclaw.core.bus.events import (
    BehavioralEvent,
    EventType,
    make_event,
)
from xmclaw.core.bus.memory import InProcessEventBus

__all__ = [
    "BehavioralEvent",
    "EventType",
    "InProcessEventBus",
    "make_event",
]
