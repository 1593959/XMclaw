"""Shared turn-level types and utilities.

Extracted from agent_loop.py to break circular imports between
mixins and the main orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xmclaw.core.bus import BehavioralEvent


def _log_memory_failure(exc: BaseException) -> None:
    """Log a memory prefetch / write failure without killing the turn.

    Memory is best-effort — a vector-DB hiccup must never break the live
    user turn. Mirrors the same posture as session_store persistence
    (best-effort, swallow OS errors, surface via logs only).
    """
    try:
        from xmclaw.utils.log import get_logger
        get_logger(__name__).debug("memory.failure %s: %s", type(exc).__name__, exc)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class AgentTurnResult:
    """What ``run_turn`` returns after a single user turn completes."""

    ok: bool
    text: str                              # final assistant text (if any)
    hops: int                              # LLM calls made
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[BehavioralEvent] = field(default_factory=list)
    error: str | None = None
