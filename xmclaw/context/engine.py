"""ContextEngine — 6-stage lifecycle abstraction for context management.

Adapted from ``xmclaw_port/context/engine.py`` (which itself was adapted
from openclaw's ``context-engine-maintenance.ts``). The abstraction
captures the full context lifecycle around a single user turn:

  1. **bootstrap**  — initialise per-session state (load history from
                      events.db / session_store, import file refs, etc.)
  2. **ingest**     — record a single message into context tracking
  3. **assemble**   — build the message list for an LLM call within a
                      token budget
  4. **compact**    — proactive or reactive context shrinkage (delegates
                      to ``ContextCompressor``)
  5. **after_turn** — post-turn maintenance (prune temp refs, update
                      file state tracking)
  6. **dispose**    — release resources (close DB conns, flush writes)

Status: SHIPPED AS ABC ONLY. The current ``AgentLoop`` implements all
six stages inline; this ABC captures the contract so a future refactor
can lift them out into a pluggable engine. The included
``SimpleContextEngine`` is an in-memory reference impl used by tests
and small-session callers (``xmclaw chat`` smoke probe etc).

Why ship the ABC now if nothing uses it yet? Because the AgentLoop
internals already conform to this shape — adding the formal contract
makes it possible to swap in a richer engine (file-state tracking,
SQLite-backed conversation index, retrieval-augmented assembly) without
touching the run_turn body. The day someone needs that, the seam exists.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BootstrapResult:
    """What ``bootstrap`` returns — surfaces import counts to the caller."""

    success: bool
    imported_messages: int = 0
    imported_files: list[str] = field(default_factory=list)
    imported_summaries: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class IngestResult:
    """Per-message ingest outcome."""

    success: bool
    message_id: str = ""
    tokens_added: int = 0
    error: Optional[str] = None


@dataclass
class AssembleResult:
    """Result of building messages for an LLM call.

    ``compressed`` and ``summary_included`` flags let the caller emit
    a CONTEXT_COMPRESSED bus event without re-checking.
    """

    messages: list[Any] = field(default_factory=list)
    total_tokens: int = 0
    compressed: bool = False
    summary_included: bool = False


@dataclass
class CompactResult:
    """Telemetry from a compaction pass."""

    success: bool
    messages_before: int = 0
    messages_after: int = 0
    summary: Optional[str] = None
    error: Optional[str] = None


class ContextEngine(ABC):
    """Abstract base class for context management engines.

    Implementations handle:
      * Message persistence (in-memory / SQLite / events.db)
      * Token counting + budget enforcement
      * Compression integration (``ContextCompressor``)
      * File-reference tracking (which files this session has touched)
      * Summary management (previous summaries persisted across turns)
    """

    @abstractmethod
    async def bootstrap(self, session_id: str) -> BootstrapResult:
        """Initialise context engine for a session.

        Imports historical context: previous messages from events.db,
        file refs from workspace, existing summaries.
        """
        ...

    @abstractmethod
    async def ingest(self, session_id: str, message: Any) -> IngestResult:
        """Ingest a single message into context.

        Args:
            session_id: session identifier
            message: ``Message`` dataclass or compatible dict
        """
        ...

    @abstractmethod
    async def assemble(
        self,
        session_id: str,
        token_budget: int,
        include_system: bool = True,
    ) -> AssembleResult:
        """Build the message list for the next LLM call.

        Args:
            session_id: session identifier
            token_budget: maximum tokens for the assembled context
            include_system: whether to include the system prompt
        """
        ...

    @abstractmethod
    async def compact(self, session_id: str, force: bool = False) -> CompactResult:
        """Compact context to free token budget.

        Args:
            session_id: session identifier
            force: bypass the threshold gate (e.g. when the LLM call
                already failed with context_overflow and we KNOW the
                payload is too big)
        """
        ...

    @abstractmethod
    async def after_turn(self, session_id: str) -> None:
        """Post-turn maintenance — update message metadata, prune
        temporary references, update file-state tracking."""
        ...

    @abstractmethod
    async def dispose(self) -> None:
        """Release resources — close DB connections, flush pending writes."""
        ...

    # ── Optional hooks (no-op by default) ────────────────────────────

    async def on_file_modified(self, session_id: str, file_path: str) -> None:
        """Hook: a file was modified during this turn."""

    async def on_tool_invoked(
        self, session_id: str, tool_name: str, result: Any,
    ) -> None:
        """Hook: a tool was invoked during this turn."""

    async def on_error(self, session_id: str, error: Exception) -> None:
        """Hook: an error occurred during this turn."""


class SimpleContextEngine(ContextEngine):
    """In-memory reference implementation.

    Useful for:
      * Tests that need a working ContextEngine without DB setup
      * Probe scripts that don't care about cross-restart persistence
      * Documentation — the bootstrap → assemble flow is easier to read
        when the impl is 80 LOC instead of 800

    NOT used by production AgentLoop (which manages context inline);
    kept here so the ABC has at least one concrete shipped impl.
    """

    def __init__(self, max_tokens: int = 128_000) -> None:
        self.max_tokens = max_tokens
        self._sessions: dict[str, list[Any]] = {}
        self._summaries: dict[str, Optional[str]] = {}

    async def bootstrap(self, session_id: str) -> BootstrapResult:
        self._sessions.setdefault(session_id, [])
        self._summaries.setdefault(session_id, None)
        return BootstrapResult(success=True)

    async def ingest(self, session_id: str, message: Any) -> IngestResult:
        if session_id not in self._sessions:
            await self.bootstrap(session_id)
        self._sessions[session_id].append(message)
        # Rough token estimate: the actual estimator lives in
        # context.compressor; keeping this impl dependency-free.
        return IngestResult(
            success=True,
            tokens_added=len(str(message)) // 4,
        )

    async def assemble(
        self,
        session_id: str,
        token_budget: int,
        include_system: bool = True,
    ) -> AssembleResult:
        messages = list(self._sessions.get(session_id, []))
        if not include_system and messages:
            first = messages[0]
            first_role = (
                getattr(first, "role", None)
                or (first.get("role") if isinstance(first, dict) else None)
            )
            if first_role == "system":
                messages = messages[1:]
        return AssembleResult(
            messages=messages,
            total_tokens=len(str(messages)) // 4,
        )

    async def compact(self, session_id: str, force: bool = False) -> CompactResult:
        # No-op compaction in the simple impl. Real implementations
        # delegate to ``ContextCompressor.compress``.
        messages = self._sessions.get(session_id, [])
        return CompactResult(
            success=True,
            messages_before=len(messages),
            messages_after=len(messages),
        )

    async def after_turn(self, session_id: str) -> None:
        return

    async def dispose(self) -> None:
        self._sessions.clear()
        self._summaries.clear()


__all__ = [
    "BootstrapResult",
    "IngestResult",
    "AssembleResult",
    "CompactResult",
    "ContextEngine",
    "SimpleContextEngine",
]
