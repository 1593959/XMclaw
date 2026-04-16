"""Step 5: Event Bus + Multi-Agent Framework."""
import asyncio
from pathlib import Path
from typing import Any, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
import uuid

# ── Create event_bus.py ───────────────────────────────────────────────────
event_bus_content = '''"""Event bus for inter-agent communication and async pub/sub."""
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
    target: str = ""  # empty = broadcast
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

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
        await bus.unsubscribe_all()
    """

    def __init__(self):
        self._subscribers: dict[str, list[tuple[str, Callable]]] = {}  # event_type -> [(id, handler)]
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
        """Unsubscribe by subscription ID. Returns True if found."""
        for etype, subs in self._subscribers.items():
            before = len(subs)
            self._subscribers[etype] = [(s, h) for s, h in subs if s != sid]
            if len(self._subscribers[etype]) < before:
                return True
        return False

    def unsubscribe_all(self, source: str | None = None) -> None:
        """Unsubscribe all handlers, optionally only those from a specific source."""
        if source is None:
            self._subscribers.clear()
        else:
            for etype in list(self._subscribers.keys()):
                self._subscribers[etype] = [
                    (s, h) for s, h in self._subscribers[etype]
                    if s.split("_")[1] != source  # rough filter by prefix
                ]
                if not self._subscribers[etype]:
                    del self._subscribers[etype]

    async def publish(self, event: Event) -> int:
        """
        Publish an event to all matching subscribers.
        Returns the number of subscribers that received the event.
        """
        # Store in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        count = 0
        handlers: list[tuple[str, Callable]] = []

        # Direct match
        handlers.extend(self._subscribers.get(event.event_type, []))
        # Wildcard subscribers
        handlers.extend(self._subscribers.get("*", []))

        for sid, handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
                count += 1
            except Exception:
                pass  # Don't let handler errors break the bus

        return count

    def get_history(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Get recent events from history, optionally filtered."""
        events = self._history[-limit:]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if source:
            events = [e for e in events if e.source == source]
        return events

    def subscriber_count(self, event_type: str | None = None) -> int:
        """Count total subscribers, or for a specific event type."""
        if event_type:
            return len(self._subscribers.get(event_type, []))
        return sum(len(v) for v in self._subscribers.values())


# ── Global event bus singleton ─────────────────────────────────────────────
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
'''

event_bus_path = Path(r"C:\Users\15978\Desktop\XMclaw\xmclaw\core\event_bus.py")
event_bus_path.write_text(event_bus_content, encoding="utf-8")
print("Created event_bus.py")

# ── Update orchestrator.py ──────────────────────────────────────────────────
orch_content = '''"""Agent orchestrator: manages agent instances and routes requests."""
from xmclaw.core.agent_loop import AgentLoop
from xmclaw.core.event_bus import EventBus, Event, EventType, get_event_bus
from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.genes.manager import GeneManager
from xmclaw.utils.log import logger


class MultiAgentSystem:
    """
    Complete multi-agent orchestration system.

    Supports:
    - create_team(): create multiple independent agents with isolated memory/sessions
    - delegate(): parent agent generates sub-agents for sub-tasks (like OpenClaw agent routing)
    - coordinate(): multiple agents collaborate on complex tasks via event bus
    """

    def __init__(self):
        self.llm = LLMRouter()
        self.tools = ToolRegistry(llm_router=self.llm)
        self.memory = MemoryManager(llm_router=self.llm)
        self.gene_manager = GeneManager()
        self.agents: dict[str, AgentLoop] = {}
        self._event_bus = get_event_bus()
        self._team: dict[str, list[str]] = {}  # team_name -> [agent_ids]

    async def initialize(self) -> None:
        logger.info("orchestrator_initializing")
        await self.tools.load_all()
        await self.memory.initialize()
        # Share memory manager with tools that need it
        AgentOrchestrator._tool_memory = self.memory  # type: ignore
        logger.info("orchestrator_ready")

    async def shutdown(self) -> None:
        logger.info("orchestrator_shutting_down")
        # Publish stop events for all agents
        for agent_id in list(self.agents.keys()):
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_STOP,
                source="orchestrator",
                target=agent_id,
                payload={"agent_id": agent_id},
            ))
        await self.memory.close()

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    async def run_agent(self, agent_id: str, user_input: str):
        """Run a single agent and yield response chunks."""
        # Publish agent start event
        await self._event_bus.publish(Event(
            event_type=EventType.AGENT_START,
            source="orchestrator",
            target=agent_id,
            payload={"agent_id": agent_id, "input_preview": user_input[:100]},
        ))

        if agent_id not in self.agents:
            self.agents[agent_id] = AgentLoop(
                agent_id=agent_id,
                llm_router=self.llm,
                tools=self.tools,
                memory=self.memory,
            )

        try:
           