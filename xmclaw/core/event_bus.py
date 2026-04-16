"""Event bus for inter-agent communication and async pub/sub."""
import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable
import uuid


class EventType(str, Enum):
    """Standard event types in XMclaw multi-agent system."""
    AGENT_START = "agent:start"
    AGENT_STOP = "agent:stop"
    AGENT_ERROR = "agent:error"
    TASK_ASSIGNED = "task:assigned"
    TASK_COMPLETED = "task:completed"
    TASK_FAILED = "task:failed"
    TOOL_CALLED = "tool:called"
    TOOL_RESULT = "tool:result"
    MEMORY_UPDATED = "memory:updated"
    GENE_ACTIVATED = "gene:activated"
    SKILL_EXECUTED = "skill:executed"
    USER_MESSAGE = "user:message"
    AGENT_MESSAGE = "agent:message"
    THINKING = "agent:thinking"


@dataclass
class Event:
    """A single event in the bus."""
    event_type: str
    source: str = "system"
    target: str = ""
    payload: dict = None
    event_id: str = None
    timestamp: str = None

    def __post_init__(self):
        if self.payload is None:
            self.payload = {}
        if self.event_id is None:
            self.event_id = str(uuid.uuid4())[:8]
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class EventBus:
    """
    Async pub/sub event bus for inter-agent communication.

    Usage:
        bus = EventBus()
        await bus.subscribe("agent:start", my_handler)
        await bus.publish(Event(EventType.AGENT_START, source="agent_1"))
    """

    def __init__(self):
        self._subscribers: dict[str, list[tuple[str, Callable]]] = {}
        self._counter = 0
        self._history: list[Event] = []
        self._max_history = 500

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[Event], Awaitable[None] | None],
    ) -> str:
        """Subscribe a handler to an event type. Returns subscription ID."""
        self._counter += 1
        sid = f"sub_{self._counter}"
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append((sid, handler))
        return sid

    def subscribe_wildcard(
        self,
        handler: Callable[[Event], Awaitable[None] | None],
    ) -> str:
        """Subscribe to all events. Returns subscription ID."""
        return self.subscribe("*", handler)

    def unsubscribe(self, sid: str) -> bool:
        """Unsubscribe by subscription ID."""
        for etype, subs in self._subscribers.items():
            before = len(subs)
            self._subscribers[etype] = [(s, h) for s, h in subs if s != sid]
            if len(self._subscribers[etype]) < before:
                return True
        return False

    def unsubscribe_all(self) -> None:
        """Unsubscribe all handlers."""
        self._subscribers.clear()

    async def publish(self, event: Event) -> int:
        """Publish an event. Returns number of handlers that received it."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        count = 0
        handlers: list[tuple[str, Callable]] = []
        handlers.extend(self._subscribers.get(event.event_type, []))
        handlers.extend(self._subscribers.get("*", []))

        for sid, handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
                count += 1
            except Exception:
                pass

        return count

    def get_history(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Get recent events, optionally filtered."""
        events = self._history[-limit:]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if source:
            events = [e for e in events if e.source == source]
        return events

    def subscriber_count(self, event_type: str | None = None) -> int:
        """Count subscribers."""
        if event_type:
            return len(self._subscribers.get(event_type, []))
        return sum(len(v) for v in self._subscribers.values())


# Global singleton
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
