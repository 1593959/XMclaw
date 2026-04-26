"""MultiAgentManager — convention #3: lazy-locked agent dict + dedup.

Direct port of ``qwenpaw/src/qwenpaw/app/multi_agent_manager.py:22-130``.
The dedup pattern (``_pending_starts: dict[str, asyncio.Event]``) is
the part that makes QwenPaw's multi-agent actually robust under
concurrent load — two simultaneous requests for the same agent_id
share one start, the second waits on the first's Event rather than
trying to construct a duplicate.

This is intentionally a thin port that mirrors the QwenPaw shape line-
for-line (modulo Python idioms vs Python-from-TS). The XMclaw-specific
cleverness goes elsewhere (in factory.build_agent_from_config and the
inter-agent tools); this class just owns the dict.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class AgentNotFound(KeyError):
    """Raised when ``get(agent_id)`` is called for an unknown agent and
    no factory was supplied to mint one. Mirrors QwenPaw's behaviour at
    ``multi_agent_manager.py:62``."""


class MultiAgentManager(Generic[T]):
    """Lazy registry of agent-id → agent-runtime objects.

    Args:
        factory: optional async factory ``(agent_id) -> T`` called when
            an unknown id is requested. ``None`` makes ``get`` raise
            :class:`AgentNotFound` instead. The factory is awaited, so
            it's safe to do filesystem reads / LLM client construction
            inside.

    The manager is generic over T so XMclaw can use it for
    ``AgentLoop`` (the primary case), but unit tests can use it for
    any ``T`` to verify the dedup semantics in isolation.
    """

    def __init__(
        self,
        factory: Callable[[str], Awaitable[T]] | None = None,
    ) -> None:
        self._factory = factory
        self._agents: dict[str, T] = {}
        self._pending: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    # ── read API ──────────────────────────────────────────────────────

    def list_ids(self) -> list[str]:
        return list(self._agents.keys())

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def peek(self, agent_id: str) -> T | None:
        """Return the agent runtime if already loaded; ``None`` if not.

        Unlike :meth:`get`, this does NOT trigger the factory. Use it
        from sync code that only wants to see if an agent is alive.
        """
        return self._agents.get(agent_id)

    # ── write API ─────────────────────────────────────────────────────

    def register(self, agent_id: str, runtime: T) -> None:
        """Insert a pre-constructed agent runtime under ``agent_id``.

        Used by the factory when the daemon wires its primary agent at
        boot. Concurrent ``get`` calls during this can race with
        ``register``; the lock handed out via :meth:`get` serializes.
        """
        self._agents[agent_id] = runtime

    async def remove(self, agent_id: str) -> bool:
        async with self._lock:
            return self._agents.pop(agent_id, None) is not None

    async def get(self, agent_id: str) -> T:
        """Lazy-fetch ``agent_id``; if not loaded, run the factory.

        Concurrent calls for the same ``agent_id`` share one factory
        execution (this is convention #3's dedup behaviour). The lock
        is released during the slow ``_factory`` call so other agent
        ids can be loaded in parallel.
        """
        existing = self._agents.get(agent_id)
        if existing is not None:
            return existing

        # Acquire the manager-level lock just to register/wait on the
        # per-id event. The actual factory call runs OUTSIDE the lock
        # to allow parallel start-up of distinct agent ids.
        async with self._lock:
            existing = self._agents.get(agent_id)
            if existing is not None:
                return existing
            event = self._pending.get(agent_id)
            if event is None:
                event = asyncio.Event()
                self._pending[agent_id] = event
                creator = True
            else:
                creator = False

        if not creator:
            # Wait for the active starter to finish.
            await event.wait()
            existing = self._agents.get(agent_id)
            if existing is None:
                # The other starter failed; let this caller try again
                # rather than silently return None.
                raise AgentNotFound(agent_id)
            return existing

        # We are the creator — call factory outside the lock.
        try:
            if self._factory is None:
                raise AgentNotFound(agent_id)
            runtime = await self._factory(agent_id)
            async with self._lock:
                self._agents[agent_id] = runtime
                event.set()
                self._pending.pop(agent_id, None)
            return runtime
        except BaseException:
            async with self._lock:
                event.set()
                self._pending.pop(agent_id, None)
            raise
