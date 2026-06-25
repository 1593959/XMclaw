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
    # 2026-05-30: stream() degraded to non-streaming complete() for this
    # hop — UI shows a transient banner so the long no-token gap doesn't
    # read as a hang. Payload: ``{reason, hop, provider}`` where reason
    # is ``"risk_reject"`` (Anthropic refused the prompt) or
    # ``"shim_no_stream"`` (compat shim lacking /stream).
    LLM_STREAM_FALLBACK = "llm_stream_fallback"
    # 2026-05-30 (F1): a file was created / modified / deleted inside the
    # current session's workspace dir. Backs the chat-page right-side
    # WorkspacePanel — UI subscribes, refreshes the file tree, opens the
    # drawer on first event. Payload: ``{path, rel_path, action, tool,
    # commit_sha, summary, bytes}`` where ``path`` is absolute,
    # ``rel_path`` is workspace-rooted, ``action`` ∈ {created, modified,
    # deleted}, ``commit_sha`` references the auto-commit in the
    # workspace's git repo (empty when git unavailable).
    WORKSPACE_FILE_CHANGED = "workspace_file_changed"
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
    TOOL_INVOCATION_PROGRESS = "tool_invocation_progress"
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
    # Jarvis Phase 1-2: per-session cache-efficiency summary. Emitted
    # periodically (every N hops or on session end) so the dashboard /
    # UI can show cache hit rates without recomputing from the full
    # event log. Payload: see CacheMetricsAggregator.build_summary_payload.
    CACHE_METRICS_SUMMARY = "cache_metrics_summary"
    # Plan #3: memory extraction latency — emitted after LLM-based fact
    # extraction completes (or fails/times out). Payload:
    # {session_id, latency_ms, facts_count, status, layer}.
    MEMORY_EXTRACTION_LATENCY = "memory_extraction_latency"
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
    # Phase 6: emitted by CognitiveDaemon after each heartbeat tick.
    # Payload mirrors tick_once() return dict:
    #   {"tick": int, "n_percepts": int, "n_goals_spawned": int,
    #    "n_plans_executed": int, "ran_experiment": bool,
    #    "n_reflections": int, "n_skill_proposals": int,
    #    "errors": list[str], "timestamp": float}
    COGNITIVE_DAEMON_TICK = "cognitive_daemon_tick"

    # B-27: emitted when a memory provider stores or recalls. Payload:
    # {"provider": "builtin" | "sqlite_vec" | ...,
    #  "op": "put" | "query" | "prefetch" | "sync_turn",
    #  "session_id": str | None,
    #  "k": int | None, "hits": int | None,
    #  "elapsed_ms": float}
    # Surfaced in the Trace page so users can see memory-layer activity.
    MEMORY_OP = "memory_op"

    # 2026-05-10 (Phase A of "agent 自己用记忆"): emitted by AgentLoop
    # when it auto-queries the UnifiedMemorySystem at the start of a
    # turn. Distinct from MEMORY_OP (which the legacy MemoryManager
    # emits for the older recall path) — the unified path crosses
    # semantic + relation + temporal axes and the UI surfaces it
    # differently (each hit shows which axes contributed).
    # Payload: {
    #   "session_id": str,
    #   "query": str,                   # user message used as semantic query
    #   "hits": [{
    #     "id": str, "text": str, "score": float,
    #     "matched_axes": list[str],   # subset of ["semantic","relation","temporal"]
    #     "layer": str,
    #   }],
    #   "elapsed_ms": float,
    #   "limit": int,
    # }
    MEMORY_RECALL = "memory_recall"
    # 2026-05-10 (Phase B): emitted by AgentLoop after a turn when the
    # MemoryExtractor decides a fact / decision / preference is worth
    # persisting via UnifiedMemorySystem.put(). Payload:
    # {
    #   "session_id": str,
    #   "id": str,                      # the unified id minted by put()
    #   "text": str,                    # the stored entry's text
    #   "layer": str,                   # "long_term" / "short_term" / etc
    #   "node_type": str,               # "event" / "entity" / "state" / "intent"
    #   "reason": str,                  # human-readable why the extractor stored it
    # }
    MEMORY_PUT_AUTO = "memory_put_auto"

    # 2026-05-10 R1 (真持续认知 Loop): emitted by CognitiveDaemon's
    # InnerMonologue channel — the agent's own running self-talk.
    # Visible to the user via the Mind page so they can see the
    # agent's "thought process" outside of user-facing turns.
    # Payload: {
    #   "kind": "reflection" | "wonder" | "concern" | "plan" | "observation",
    #   "text": str,                    # one short paragraph of self-talk
    #   "tick": int,                    # cognitive_daemon tick number
    #   "trigger": str,                 # what prompted this thought
    # }
    INNER_MONOLOGUE = "inner_monologue"
    # Sprint 1: ProactiveAgent fires this when a trigger surfaces a
    # message the agent wants to say WITHOUT user prompt. Payload: {
    #   "trigger": str,        # trigger name ("idle_check_in", "system_health", ...)
    #   "message": str,        # what the user sees as the assistant bubble
    #   "urgency": "low" | "normal" | "high",
    #   "ts": float,
    #   ... + per-trigger payload (idle_minutes, warning text, ...)
    # }
    # Frontend renders this as an agent-initiated bubble; click can
    # open a new conversation thread with this as the first turn.
    PROACTIVE_PROPOSAL = "proactive_proposal"
    # R1: emitted when the 5-min ReflectionCycle.reflect_recent runs
    # — surfaces patterns / quality scores / suggestions found by
    # looking at the last N turns. Payload: {
    #   "scope": "recent" | "consolidate" | "groom",
    #   "lookback_n": int,
    #   "patterns_found": list[str],
    #   "actions_taken": list[str],     # propose_skill / curriculum_edit / archive_goal
    #   "elapsed_ms": float,
    # }
    REFLECTION_CYCLE_RAN = "reflection_cycle_ran"
    # R1: emitted when the 1h ConsolidationCycle compresses short-term
    # memory into long-term. Payload: {
    #   "promoted": int,                # short → long entries
    #   "merged": int,                  # near-duplicates merged
    #   "archived": int,                # stale entries removed
    #   "elapsed_ms": float,
    # }
    MEMORY_CONSOLIDATED = "memory_consolidated"
    # R1: emitted when the 1d GoalGroomingCycle prunes / advances
    # the goal queue. Payload: {
    #   "before": int,
    #   "after": int,
    #   "completed_archived": int,
    #   "stale_dropped": int,
    #   "stuck_replanned": int,
    # }
    GOALS_GROOMED = "goals_groomed"

    # R3 (2026-05-10) — emitted by Reformer when a Pattern from
    # MetaCognitionPass earns a concrete proposal. The UI renders
    # these as a "建议" panel where the operator can approve / reject;
    # approved proposals route into the existing Evolution / Persona
    # pipelines (curriculum_edit / skill_propose / preference_update).
    # Payload: {
    #   "kind": "curriculum_edit" | "skill_propose" | "preference_update"
    #            | "no_op",
    #   "pattern_summary": str,
    #   "payload": dict,           # kind-specific (e.g. {addendum, tag, ...})
    #   "confidence": float,        # ≤ 0.6 (Iron Rule #2 cap)
    #   "why": str,                 # one-liner rationale
    # }
    METACOGNITION_PROPOSAL = "metacognition_proposal"

    # Jarvisification: cognitive architecture events.
    # Emitted by FileWatcher when filesystem changes are detected.
    FILE_SYSTEM_EVENT = "file_system_event"
    # Emitted when CognitiveState attention focus shifts.
    ATTENTION_SHIFT = "attention_shift"
    # Emitted when TaskScheduler task state changes.
    TASK_STATE_CHANGED = "task_state_changed"

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

    # Wave 26 fix-4: fires BEFORE the compressor discards content so
    # memory subscribers can extract facts from the doomed messages.
    # Closes the "compression eats my memory" pain — autobio + vector
    # stores listen on this and run fact extraction on the payload.
    # Payload:
    # {"session_id": str,
    #  "dropped_messages": [           # list of message dicts about to be summarised
    #    {"role": str, "content": str, "ts": float | None}, ...
    #  ],
    #  "trigger": "proactive" | "reactive",  # how compression got invoked
    #  "estimated_tokens": int}         # CJK-aware estimate of dropped slice
    CONTEXT_COMPRESSION_PENDING = "context_compression_pending"

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

    # Epic #26 Phase B (2026-05-19) — HTN plan lifecycle events. Emitted
    # by ActionDispatcher.execute_plan and Planner.execute so the UI
    # ("Autonomous Tasks" panel) + observability layer can see what
    # autonomous work is in flight, completed, or wedged. Pre-Phase-B
    # the only signal a plan was running was indirect (the dispatcher
    # used the step_id as session_id → reflection events fanned out
    # under that id, but no event NAMED a plan ever appeared).
    #
    # Common payload fields (all six events):
    #   "plan_id": str         # Plan.id (post-Phase-A: plan_<uuid12>)
    #   "goal_id": str         # plan.goal_id
    #   "n_steps": int         # total steps in the plan
    #
    # PLAN_STARTED additional:
    #   "step_ids": list[str]   # in topological order
    #   "confidence": float
    # PLAN_STEP_STARTED / PLAN_STEP_COMPLETED / PLAN_STEP_FAILED:
    #   "step_id": str
    #   "step_index": int       # 0-based ordinal in execution order
    #   "action_kind": str
    # PLAN_STEP_COMPLETED additional:
    #   "latency_ms": float
    #   "output_keys": list[str]   # top-level keys of step output dict
    # PLAN_STEP_FAILED additional:
    #   "error": str
    #   "latency_ms": float
    # PLAN_COMPLETED / PLAN_FAILED:
    #   "status": "completed" | "repaired" | "failed"
    #   "duration_ms": float
    #   "n_step_results": int
    # PLAN_FAILED additional:
    #   "error": str
    PLAN_STARTED = "plan_started"
    PLAN_STEP_STARTED = "plan_step_started"
    PLAN_STEP_COMPLETED = "plan_step_completed"
    PLAN_STEP_FAILED = "plan_step_failed"
    PLAN_COMPLETED = "plan_completed"
    PLAN_FAILED = "plan_failed"

    # Agent-kernel observability (2026-06-24): additive runtime signals
    # for the LangGraph-style GraphState / reducer path and Reflexion
    # failure critique path. Payloads are JSON-ish and intentionally
    # compact so events.db does not become a second checkpoint store.
    #
    # GRAPH_STATE_UPDATED:
    #   {"plan_id": str, "goal_id": str, "step_id": str | None,
    #    "step_index": int | None, "final": str | None,
    #    "subtasks": int, "tool_results": int, "errors": int}
    # SELF_CRITIQUE_REQUESTED:
    #   {"plan_id": str, "goal_id": str, "trigger": str,
    #    "failure_summary": str, "trajectory_events": int}
    # SUMMARIZER_EVICTION_PLANNED:
    #   {"session_id": str, "messages_before": int,
    #    "summarize_start": int, "summarize_end": int,
    #    "evict_ratio": float, "ranges": list[dict]}
    # TOOL_SANDBOX_POLICY_DECIDED:
    #   {"tool_name": str, "policy": str, "decision": "allow" | "deny",
    #    "reason": str, "sandbox_runtime": "host" | "docker" | "none",
    #    "image": str}
    GRAPH_STATE_UPDATED = "graph_state_updated"
    SELF_CRITIQUE_REQUESTED = "self_critique_requested"
    SUMMARIZER_EVICTION_PLANNED = "summarizer_eviction_planned"
    TOOL_SANDBOX_POLICY_DECIDED = "tool_sandbox_policy_decided"

    # Epic #27 sweep follow-up (2026-05-19): emitted by SkillsWatcher
    # when it SUCCESSFULLY hot-reloads a Python ``skill.py`` after an
    # mtime change. Pre-this-event Python skills always needed a
    # daemon restart; now most edits go live within one tick.
    # Payload:
    #   "skill_id": str
    #   "version": int
    #   "path": str           # absolute path to the reloaded file
    #   "kind": "python"      # for symmetry with future SKILL.md hot-reload
    SKILL_HOT_RELOADED = "skill_hot_reloaded"

    # Epic #26 Phase C (2026-05-19): emitted by ActionDispatcher.
    # execute_plan when a per-plan cost budget is exceeded mid-run.
    # The plan halts at this step (no further dispatches) and the
    # PlanResult records ``status="failed"`` with the budget reason
    # so the UI / cognitive_daemon doesn't keep re-trying the same
    # over-budget work. Payload:
    #   "plan_id": str
    #   "goal_id": str
    #   "n_step_results": int    # how many steps got run before halt
    #   "spent_usd": float       # actual cost incurred during this plan
    #   "budget_usd": float      # configured cap
    #   "would_exceed_at_step": int  # the step index that triggered halt
    PLAN_BUDGET_EXCEEDED = "plan_budget_exceeded"

    # Android companion device events (M1)
    DEVICE_EVENT = "device_event"

    # Live Canvas / A2UI (Phase 6+): agent-generated visual artifacts.
    # Emitted when the agent calls canvas_create / canvas_update / canvas_close
    # so the frontend can render diagrams, charts, tables, and rich HTML
    # inline in the chat transcript.
    #
    # CANVAS_ARTIFACT_CREATED payload:
    #   {"artifact_id": str, "kind": "mermaid"|"html"|"svg"|"chart"|"table",
    #    "title": str, "content": str, "turn_id": str}
    # CANVAS_ARTIFACT_UPDATED payload:
    #   {"artifact_id": str, "content": str}
    # CANVAS_ARTIFACT_CLOSED payload:
    #   {"artifact_id": str}
    CANVAS_ARTIFACT_CREATED = "canvas_artifact_created"
    CANVAS_ARTIFACT_UPDATED = "canvas_artifact_updated"
    CANVAS_ARTIFACT_CLOSED = "canvas_artifact_closed"
    # WorkerSwarm lifecycle (2026-05-23): emitted when a WorkerAgent
    # starts / completes / fails a subtask. The frontend renders these
    # inline in the parent session's chat transcript so users see
    # parallel worker progress without the worker sessions cluttering
    # the sidebar.
    WORKER_STARTED = "worker_started"
    WORKER_COMPLETED = "worker_completed"
    WORKER_FAILED = "worker_failed"
    # parallel_subagents fanout lifecycle (2026-05-25): emitted per
    # ephemeral sub-agent so the chat UI can render a live, expanded
    # output card for each fanout leaf — replaces the silent-then-
    # synthesised flow that hid intermediate progress.
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_COMPLETED = "subagent_completed"
    # 2026-06-17: Expert Team (P0). Emitted once per top-level fanout so the
    # Mission Control "TeamView" can render a leader card with the goal,
    # plan, and synthesis strategy before the per-leaf subagent events.
    FANOUT_STARTED = "fanout_started"
    # 派发前编辑拆解（#3）：显式「派专家团」时，组长拆完任务先发这个事件
    # 把方案推给前端编辑，工具侧阻塞等用户确认；用户决定经
    # fanout_review_decision 客户端帧回到守在 Future 上的工具。
    FANOUT_REVIEW_REQUESTED = "fanout_review_requested"
    # 2026-05-26: memory curation events. Pre-fix the new
    # forget / correct / dedup_scope service methods only logged to
    # stdlib — the "记忆活动" UI tab + audit replay had no way to
    # show "agent forgot fact X at time T". Now each curation op
    # emits a structured event so the timeline carries the audit
    # trail. Payload shapes:
    #   MEMORY_FORGOT:    {fact_id, text, reason, source}
    #   MEMORY_CORRECTED: {old_fact_id, new_fact_id, old_text,
    #                      new_text, matched, distance, source}
    #   MEMORY_DEDUPED:   {scope, bucket, scanned, merged, dry_run,
    #                      source}
    # ``source`` is "tool" / "api" / "agent" — discriminates UI vs
    # agent-tool vs HTTP-router triggers.
    MEMORY_FORGOT = "memory_forgot"
    MEMORY_CORRECTED = "memory_corrected"
    MEMORY_DEDUPED = "memory_deduped"


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
