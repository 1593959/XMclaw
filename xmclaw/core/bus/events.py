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
    # B-109: daemon detected an external write to daemon/config.json
    # and refreshed the in-memory cfg dict. Some fields take effect
    # live (prompt-injection policy, tools.allowed_dirs, retention),
    # others (LLM, memory providers) require a daemon restart — the
    # payload reports which keys changed so the UI can advise.
    CONFIG_RELOADED = "config_reloaded"
    TOOL_CALL_EMITTED = "tool_call_emitted"
    TOOL_INVOCATION_STARTED = "tool_invocation_started"
    TOOL_INVOCATION_FINISHED = "tool_invocation_finished"
    # B-341 (audit pass-2 #12): SKILL_EXEC_STARTED / SKILL_EXEC_FINISHED
    # were removed in this commit. They were carry-over from a removed
    # sandboxed-runtime path with zero remaining publishers (no Python
    # code emitted them, no frontend reducer case looked for them, no
    # test asserted on them — confirmed via grep). The previous comment
    # said "kept to avoid breaking the frontend reducer's case map" but
    # the frontend never listened. Skill execution today is observed
    # via TOOL_INVOCATION_STARTED / TOOL_INVOCATION_FINISHED with
    # name="skill_<id>".
    GRADER_VERDICT = "grader_verdict"
    COST_TICK = "cost_tick"
    SESSION_LIFECYCLE = "session_lifecycle"
    # SKILL_CANDIDATE_PROPOSED carries 3 different semantics distinguished
    # by ``payload.decision``:
    #   "propose"  → a NEW skill draft (from SkillDreamCycle / SkillProposer)
    #   "promote"  → upgrade an existing skill version (from EvolutionAgent
    #                 + MutationOrchestrator after evidence threshold cleared)
    #   "rollback" → revert to an older version (from EvolutionAgent on
    #                 controller verdict)
    # The 3 semantics are operationally different but historically share
    # one event type. B-318 added 3 explicit aliases (DRAFTED /
    # PROMOTION_RECOMMENDED / ROLLBACK_RECOMMENDED) for code that wants
    # to subscribe to one specific path without payload-discriminating.
    # The old name keeps emitting for backwards compatibility with the
    # frontend reducer + ProposalMaterializer + EvolutionOrchestrator;
    # new emitters can ALSO publish the specific alias.
    SKILL_CANDIDATE_PROPOSED = "skill_candidate_proposed"
    SKILL_DRAFTED = "skill_drafted"  # B-318: alias for decision="propose"
    SKILL_PROMOTION_RECOMMENDED = "skill_promotion_recommended"  # B-318
    SKILL_ROLLBACK_RECOMMENDED = "skill_rollback_recommended"  # B-318
    SKILL_PROMOTED = "skill_promoted"
    SKILL_ROLLED_BACK = "skill_rolled_back"
    # Sprint 3 Iron Rule #1: emitted when EvolutionController would
    # have promoted on legacy thresholds but the multi-signal gate
    # (``GraderVerdict.promote_eligible``) refused. ``xmclaw evolve
    # review`` listens so reviewers can see WHY promotion didn't fire
    # (no independent signal, judge disagreement, deterministic-score
    # floor, etc.). Payload:
    #   {"skill_id": str | None,
    #    "candidate_id": str | None,
    #    "candidate_version": int | None,
    #    "head_version": int | None,
    #    "reason": str,                # "single_signal_only" | "deterministic_floor" | "independent_floor"
    #    "deterministic_score": float | None,
    #    "independent_score": float | None,
    #    "evidence": list[str]}
    SKILL_PROMOTION_BLOCKED = "skill_promotion_blocked"
    # B-333 (audit #19): emitted when SkillsWatcher detects that a
    # Python ``skill.py`` file changed but ``SkillRegistry.update_body``
    # can't apply the change (importlib caches the module — only a
    # daemon restart picks up the new code). Payload:
    # ``{"skill_id": str, "version": int, "path": str}``. UI listens
    # so the Skills page can show "restart needed" banner; pre-B-333
    # the watcher silently no-op'd and users had no signal that their
    # SKILL.py edit wouldn't take effect until restart.
    SKILL_UPDATE_REQUIRES_RESTART = "skill_update_requires_restart"
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

    # NOTE: SKILL_INVOKED / SKILL_OUTCOME have no current publishers.
    # The original B-29/B-35 heuristic detection (matching agent text
    # against skill titles/triggers) was removed when SkillToolProvider
    # made skill execution deterministic — every skill_<id> tool call
    # now produces TOOL_INVOCATION_STARTED + TOOL_INVOCATION_FINISHED +
    # GRADER_VERDICT events with the proper skill_id stamp. Kept in
    # the enum because the frontend reducer (chat_reducer.js) and
    # Trace.js still register handlers; clean up when the frontend
    # stops listening.
    SKILL_INVOKED = "skill_invoked"
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

    # Epic #24 Phase 2.2: emitted when ProfileExtractor flushes new
    # delta lines to ``<persona>/USER.md`` so the agent's frozen
    # system-prompt cache can invalidate (next turn re-reads USER.md
    # via persona assembler). Payload:
    # {"file_path": str,             # absolute path of file written
    #  "delta_count": int,           # how many delta lines appended
    #  "session_id": str,            # source session whose buffer triggered the flush
    #  "deltas": [{kind, text, confidence}, ...]}
    USER_PROFILE_UPDATED = "user_profile_updated"

    # Sprint 3 #3: Letta-pattern sleep-time agent + OS idle scheduler.
    # See docs/SLEEP_AGENT.md and docs/EVOLUTION_HONEST_STATE.md
    # ("Iron Rules"). Foreground vs. sleep-time split is the actual
    # value-add: only sleep agent writes memory, only foreground reads,
    # so heavy compaction never collides with active turns. The cron-
    # based dream cycle remains a fallback trigger; idle-detection wins
    # whichever crosses first.
    #
    # SLEEP_IDLE_DETECTED — emitted exactly once per threshold crossing.
    # Payload: {"level": "short" | "long",  # which threshold tripped
    #           "idle_seconds": float}      # observed idle at trip time
    SLEEP_IDLE_DETECTED = "sleep_idle_detected"
    # SLEEP_TASK_STARTED — emitted right before the registered fn runs.
    # Payload: {"task_name": str, "level": "short" | "long"}
    SLEEP_TASK_STARTED = "sleep_task_started"
    # SLEEP_TASK_FINISHED — emitted after the fn returns (or raises).
    # Payload: {"task_name": str,
    #           "ok": bool,                  # False when fn raised
    #           "duration_ms": float,
    #           "result": dict[str, Any]}    # whatever fn returned (or
    #                                         # {"error": "<repr>"}}
    SLEEP_TASK_FINISHED = "sleep_task_finished"
    # SLEEP_INTERRUPTED — emitted when the user resumes mid-task and the
    # SleepWorker cancels-with-rollback (any SleepWorkspace buffered
    # writes are discarded; foreground sees pre-task state). Payload:
    # {"task_name": str,
    #  "partial_progress": dict[str, Any]}   # whatever the task
    #                                         # checkpoint last set
    SLEEP_INTERRUPTED = "sleep_interrupted"


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
