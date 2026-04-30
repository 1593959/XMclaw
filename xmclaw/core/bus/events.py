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
    # B-91: per-token reasoning / extended-thinking chunks. Distinct
    # from LLM_CHUNK (which carries user-visible text). Anthropic
    # surfaces these as ``thinking_delta`` events; OpenAI / MiniMax /
    # Moonshot / DashScope surface them as ``delta.reasoning_content``
    # or ``delta.reasoning``. Provider layer normalises both shapes
    # into this event so the UI can show "what was the model thinking
    # before it answered" in the PhaseCard body.
    LLM_THINKING_CHUNK = "llm_thinking_chunk"
    LLM_RESPONSE = "llm_response"
    # B-92: agent stops mid-turn to ask the user a question with N
    # options (single- or multi-select). Tool invocation blocks on a
    # future that the WS handler resolves when the user clicks an
    # answer in the UI's QuestionCard. Inspired by free-code's
    # AskUserQuestionTool — closes the "agent guesses when ambiguous"
    # gap. Payload: {question_id, question, options, multi_select}.
    AGENT_ASKED_QUESTION = "agent_asked_question"
    # User's answer comes back via the same WS connection as a
    # client→server frame; the daemon re-broadcasts it as this event
    # so the chat transcript and any audit log replay can see what
    # the user picked. Payload: {question_id, value}.
    USER_ANSWERED_QUESTION = "user_answered_question"
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

    # B-35: emitted alongside SKILL_INVOKED with a verdict for the
    # whole turn the skill rode. Payload:
    # {"skill_id": str, "session_id": str,
    #  "verdict": "success" | "partial" | "error",
    #  "hops": int, "tool_errors": int}
    # Closes the evolution feedback loop: invocation count alone
    # treats every fire as equal; the outcome lets the optimizer
    # weight skills by whether they actually helped vs broke turns.
    SKILL_OUTCOME = "skill_outcome"

    # B-51: emitted when DreamCompactor successfully rewrites
    # MEMORY.md. Payload: {ok, before_chars, after_chars,
    # saved_chars, backup_path, memory_path, ts}.
    MEMORY_DREAMED = "memory_dreamed"

    # B-43: emitted by MemoryFileIndexer at the end of every tick
    # that actually changed something. Payload:
    # {"files_changed": int, "chunks_added": int, "chunks_deleted": int,
    #  "chunks_unchanged": int, "files_removed": int, "elapsed_ms": float}
    # Lets the Trace page show "indexer just embedded 3 chunks from
    # MEMORY.md" so the user knows their edit landed in the vector
    # store. No-op ticks (no changes) don't emit, so the bus stays
    # quiet when the user isn't editing.
    MEMORY_INDEXED = "memory_indexed"

    # B-33: emitted when AgentLoop._persist_history compresses older
    # turns into a synthetic system summary. Payload:
    # {"session_id": str,
    #  "dropped_count": int,         # number of messages summarised
    #  "kept_count": int,            # surviving history length
    #  "dropped_tokens_estimated": int,  # chars/4 estimate
    #  "trigger": "msg_cap" | "token_cap" | "both",
    #  "summary_chars": int}         # length of the inserted summary text
    # Surfaces compression activity on the Trace page so the user knows
    # WHY older content disappeared from the agent's view.
    CONTEXT_COMPRESSED = "context_compressed"


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
