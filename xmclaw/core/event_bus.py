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
    # ── Evolution journal state machine (Phase E0) ──────────────────────────
    # Fine-grained progress events emitted from EvolutionEngine.run_cycle().
    # Each maps 1:1 to a wire name via WS_EVENT_MAP below so the frontend can
    # render the Evolution Live panel deterministically.
    EVOLUTION_CYCLE_STARTED    = "evolution:cycle_started"
    EVOLUTION_REFLECTING       = "evolution:reflecting"
    EVOLUTION_FORGING          = "evolution:forging"
    EVOLUTION_VALIDATING       = "evolution:validating"
    EVOLUTION_ARTIFACT_SHADOW  = "evolution:artifact_shadow"
    EVOLUTION_ARTIFACT_PROMOTED = "evolution:artifact_promoted"
    EVOLUTION_ARTIFACT_RETIRED = "evolution:artifact_retired"
    EVOLUTION_ROLLBACK         = "evolution:rollback"
    EVOLUTION_REJECTED         = "evolution:rejected"
    # Phase E7: high-risk artifact parked in shadow pending human approval.
    # Payload includes artifact_id, kind, risk reasons, and the shadow path
    # so the UI can render a decision prompt without another round trip.
    EVOLUTION_APPROVAL_REQUESTED = "evolution:approval_requested"
    # Follow-up emitted after approve_artifact runs — lets dashboards mark
    # the prompt as resolved without tailing artifact_promoted / retired.
    EVOLUTION_APPROVAL_DECIDED   = "evolution:approval_decided"
    EVOLUTION_CYCLE_ENDED      = "evolution:cycle_ended"
    # Phase E6: message-level human feedback. Published when a user presses
    # 👍/👎 on a turn so dashboards can render live counters and reflection
    # can notice the signal without re-querying SQLite.
    USER_FEEDBACK_RECORDED     = "user:feedback_recorded"


# ── Frontend wire contract (Phase E0, PR-E0-3) ──────────────────────────────
# Explicit map from EventType → wire `type` name. The daemon WS forwarder
# uses this to emit clean, per-type events to the frontend:
#     {"type": "<wire_name>", "payload": {...}, "source": ..., "ts": ...}
# Events NOT in this map keep the legacy `{"type":"event", "event": {...}}`
# envelope so the migration stays additive.
WS_EVENT_MAP: dict[str, str] = {
    # Conversation-facing events the chat panel cares about
    EventType.REFLECTION_COMPLETE.value:        "reflection_complete",
    EventType.AGENT_THINKING.value:             "agent_thinking",
    EventType.AGENT_MESSAGE.value:              "agent_message",
    EventType.USER_MESSAGE.value:               "user_message_event",
    EventType.TOOL_CALLED.value:                "tool_called",
    EventType.TOOL_RESULT.value:                "tool_result_event",
    # Legacy evolution events kept for the current dashboard UI
    EventType.EVOLUTION_TRIGGER.value:          "evolution_trigger",
    EventType.EVOLUTION_NOTIFY.value:           "evolution_notify",
    EventType.EVOLUTION_CYCLE.value:            "evolution_cycle",
    EventType.PATTERN_THRESHOLD.value:          "pattern_threshold",
    EventType.GENE_GENERATED.value:             "gene_generated",
    EventType.SKILL_GENERATED.value:            "skill_generated",
    # Phase E0 journal state-machine events (Evolution Live panel feed)
    EventType.EVOLUTION_CYCLE_STARTED.value:    "evolution_cycle_started",
    EventType.EVOLUTION_REFLECTING.value:       "evolution_reflecting",
    EventType.EVOLUTION_FORGING.value:          "evolution_forging",
    EventType.EVOLUTION_VALIDATING.value:       "evolution_validating",
    EventType.EVOLUTION_ARTIFACT_SHADOW.value:  "evolution_artifact_shadow",
    EventType.EVOLUTION_ARTIFACT_PROMOTED.value: "evolution_artifact_promoted",
    EventType.EVOLUTION_ARTIFACT_RETIRED.value: "evolution_artifact_retired",
    EventType.EVOLUTION_ROLLBACK.value:         "evolution_rollback",
    EventType.EVOLUTION_REJECTED.value:         "evolution_rejected",
    EventType.EVOLUTION_APPROVAL_REQUESTED.value: "evolution_approval_requested",
    EventType.EVOLUTION_APPROVAL_DECIDED.value:   "evolution_approval_decided",
    EventType.EVOLUTION_CYCLE_ENDED.value:      "evolution_cycle_ended",
    # Phase E6 human feedback
    EventType.USER_FEEDBACK_RECORDED.value:     "user_feedback_recorded",
}


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
