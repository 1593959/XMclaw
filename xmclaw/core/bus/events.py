"""BehavioralEvent — v2 data contract.

Every tool call, LLM response, skill exec, grader verdict, etc. becomes a
``BehavioralEvent`` and flows through the ``InProcessEventBus``. Subscribers
(grader, scheduler, memory, cost tracker, web UI) consume events — they do
not call each other directly.

Schema versioning: additive field changes bump ``schema_version`` minor;
removal/rename bumps major and requires a migration note in the CHANGELOG.
See ``docs/V2_DEVELOPMENT.md`` §4.3.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Phase 1 minimal event set. See V2_DEVELOPMENT.md §4.2."""

    USER_MESSAGE = "user_message"
    LLM_REQUEST = "llm_request"
    LLM_CHUNK = "llm_chunk"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL_EMITTED = "tool_call_emitted"
    TOOL_INVOCATION_STARTED = "tool_invocation_started"
    TOOL_INVOCATION_FINISHED = "tool_invocation_finished"
    SKILL_EXEC_STARTED = "skill_exec_started"
    SKILL_EXEC_FINISHED = "skill_exec_finished"
    GRADER_VERDICT = "grader_verdict"
    COST_TICK = "cost_tick"
    SESSION_LIFECYCLE = "session_lifecycle"
    SKILL_CANDIDATE_PROPOSED = "skill_candidate_proposed"
    SKILL_PROMOTED = "skill_promoted"
    SKILL_ROLLED_BACK = "skill_rolled_back"
    ANTI_REQ_VIOLATION = "anti_req_violation"
    # Emitted when the agent calls `todo_write` so the UI can live-render
    # the todo panel without polling. Payload: {"items": [...], "sid": ...}.
    TODO_UPDATED = "todo_updated"
    # Epic #14: emitted when the prompt scanner detects injection attempts
    # in untrusted context (tool output, web-fetched text, file loads).
    # Payload: {
    #     "source": "tool_result" | "web_fetch" | "file_read",
    #     "policy": "detect_only" | "redact" | "block",
    #     "findings": [{"pattern_id", "severity", "category", "match"}],
    #     "invisible_chars": int,
    #     "scanned_length": int,
    #     "acted": bool,              # True when policy mutated / blocked content
    #     "tool_call_id": str | None,  # set when source=="tool_result"
    # }
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    # Epic #5: emitted when the memory provider evicts items, either from
    # periodic daemon maintenance or an explicit admin prune/evict call.
    # Emitted with session_id="_system" / agent_id="daemon".
    # Payload: {
    #     "layer": "short" | "working" | "long",
    #     "count": int,              # rows deleted
    #     "reason": "age" | "cap_items" | "cap_bytes" | "cap",
    #     "bytes_removed": int | None,  # only present for byte-cap evictions
    # }
    MEMORY_EVICTED = "memory_evicted"

    # B-27: emitted when a memory provider stores or recalls. Payload:
    # {"provider": "builtin" | "sqlite_vec" | ...,
    #  "op": "put" | "query" | "prefetch" | "sync_turn",
    #  "session_id": str | None,
    #  "k": int | None, "hits": int | None,
    #  "elapsed_ms": float}
    # Surfaced in the Trace page so users can see memory-layer activity.
    MEMORY_OP = "memory_op"

    # B-29: emitted heuristically when the agent appears to be acting
    # on a learned SKILL.md (i.e. its reply mentions the skill's
    # title or trigger keywords). Payload:
    # {"skill_id": str, "trigger_match": str | None,
    #  "session_id": str, "evidence": "title" | "trigger"}
    # Used by the Evolution UI to show per-skill usage counts so
    # auto_repair_v9 can be compared with v8 by real invocation rate.
    SKILL_INVOKED = "skill_invoked"


@dataclass(frozen=True, slots=True)
class BehavioralEvent:
    """A single observation in the streaming bus.

    Immutable by construction (``frozen=True``) so subscribers cannot mutate
    the event in-flight and accidentally change what later subscribers see.
    """

    id: str
    ts: float
    session_id: str
    agent_id: str
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    parent_id: str | None = None
    schema_version: int = 1


def make_event(
    *,
    session_id: str,
    agent_id: str,
    type: EventType,  # noqa: A002 — matches dataclass field
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    parent_id: str | None = None,
) -> BehavioralEvent:
    """Factory that fills ``id`` (UUIDv4 — UUIDv7 once stdlib has it) and ``ts``.

    Callers should use this rather than constructing ``BehavioralEvent`` directly
    so that we have one place to plug in UUIDv7 when Python stdlib ships it.
    """
    return BehavioralEvent(
        id=uuid.uuid4().hex,
        ts=time.time(),
        session_id=session_id,
        agent_id=agent_id,
        type=type,
        payload=dict(payload or {}),
        correlation_id=correlation_id,
        parent_id=parent_id,
    )
