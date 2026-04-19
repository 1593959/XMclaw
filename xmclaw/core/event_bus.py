"""Event bus for inter-agent communication and async pub/sub."""
import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable
import uuid


class EventType(str, Enum):
    """Standard event types in XMclaw multi-agent system."""
    AGENT_START = "agent:start"
    AGENT_STOP = "agent:stop"
    AGENT_ERROR = "agent:error"
    AGENT_THINKING = "agent:thinking"
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
    EVOLUTION_CYCLE = "evolution:cycle"
    EVOLUTION_NOTIFY = "evolution:notify"
    GENE_GENERATED = "gene:generated"
    SKILL_GENERATED = "skill:generated"
    REFLECTION_COMPLETE = "reflection:complete"
    PATTERN_THRESHOLD = "pattern:threshold_reached"
    EVOLUTION_TRIGGER = "evolution:trigger"


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
    Async pub/sub event bus for inter-agent communication with:
    - Thread-safe publishing
    - In-memory history (up to 500 events)
    - Filterable history queries
    - Subscriber count tracking
    - Wildcard subscriptions
    - Rate limiting protection

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
        self._lock = asyncio.Lock()
        # Rate limiting: track publish counts per event_type
        self._rate_limit: dict[str, list[float]] = defaultdict(list)
        self._rate_limit_window = 60.0  # seconds
        self._rate_limit_max = 200      # max events per type per window

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
        # Rate limiting check
        now = time.time()
        window = self._rate_limit[event.event_type]
        self._rate_limit[event.event_type] = [t for t in window if now - t < self._rate_limit_window]
        if len(self._rate_limit[event.event_type]) >= self._rate_limit_max:
            return 0  # Silently drop if over rate limit
        self._rate_limit[event.event_type].append(now)

        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        count = 0
        handlers: list[tuple[str, Callable]] = []
        async with self._lock:
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

    def subscribe_wildcard(
        self,
        handler: Callable[[Event], Awaitable[None] | None],
    ) -> str:
        """Subscribe to all events. Returns subscription ID."""
        return self.subscribe("*", handler)

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

    def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics."""
        type_counts: dict[str, int] = defaultdict(int)
        for e in self._history:
            type_counts[e.event_type] += 1
        return {
            "total_events": len(self._history),
            "subscriber_count": sum(len(v) for v in self._subscribers.values()),
            "events_by_type": dict(type_counts),
            "history_capacity": self._max_history,
        }

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


# ── Event handlers that wire into the system ────────────────────────────────

async def _audit_log_handler(event: Event) -> None:
    """Log all events to audit file for debugging and analysis."""
    try:
        from xmclaw.utils.paths import BASE_DIR
        import structlog
        logger = structlog.get_logger()
        logger.info("event",
                    type=event.event_type,
                    source=event.source,
                    target=event.target,
                    payload_keys=list(event.payload.keys()))
    except Exception:
        pass


async def _tool_analytics_handler(event: Event) -> None:
    """Track tool usage for analytics and evolution insight generation."""
    if event.event_type != EventType.TOOL_CALLED:
        return
    try:
        tool_name = event.payload.get("tool", "unknown")
        agent_id = event.source
        # Could be written to SQLite for later analysis
    except Exception:
        pass


def install_event_handlers() -> None:
    """Install built-in event handlers into the global event bus.

    Call this once during daemon startup (after orchestrator init).
    """
    bus = get_event_bus()
    # Audit log: log all events
    bus.subscribe("*", _audit_log_handler)
    # Tool analytics: track usage
    bus.subscribe(EventType.TOOL_CALLED, _tool_analytics_handler)
