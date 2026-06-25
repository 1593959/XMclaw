# XMclaw Remediation Development Plan

Date: 2026-06-24
Source: `docs/audit/XMCLAW_FRONTEND_AND_AGENT_GAP_AUDIT_2026-06-24.md`
External baseline: source-and-paper research over leading implementations including LangGraph, MetaGPT, Letta/MemGPT, SWE-agent, OpenAI Agents, MCP, OpenClaw, and Hermes.

## Goal

Turn the external audit recheck into an implementation roadmap. The external research baseline is strong and should set the target shape. The recheck only corrects XMclaw-specific factual drift: XMclaw already has many primitives in place, so the work now is to make them first-class, safer, observable, and testable.

## Principles

- Keep current AgentLoop, EventBus, memory, tool, and Mission Control contracts stable.
- Prefer incremental runtime primitives over replacing the system with LangGraph/Letta/SWE-agent wholesale.
- Ship each batch with a build or targeted test.
- Treat frontend safety and reachability as P0 because it is the operator surface for every backend capability.

## Kernel Refactor Quality Gates

The core runtime work is allowed to refactor deeply, but not to create a second code mountain.

- One owner per concept: graph state, planning state, tool history, and memory eviction each get one canonical module.
- No copy-paste compatibility tracks. Existing code migrates toward the new primitive; old paths either delegate or stay untouched until replaced.
- No hidden upward imports. `core/`, `cognition/`, `memory/`, and `providers/` keep their dependency contracts.
- Every new primitive must have unit tests before being wired into AgentLoop.
- Every runtime state object must be checkpoint-friendly: JSON-ish snapshot, deterministic reducers, no live provider objects inside state.
- Every policy object must be serializable: names/config only, no captured callables.
- Refactors must preserve current user-facing behavior until the replacement path has front/back tests.

## Batch 0: Documentation And Triage

Status: in progress.

Deliverables:

- Rechecked external audit table with outdated claims corrected.
- This development plan.
- Priority map for frontend, agent runtime, memory, tools, reflection, and observability.

Acceptance:

- Every remediation item has a priority, a first implementation step, and a verification path.

## Batch 1: Frontend P0 Hardening

Status: in progress.

Problems:

- Mobile users cannot reach Memory, Skills, Files, Team, or System because `TaskRail` is hidden below `md` and owns `DomainNav`.
- Artifact previews allow scripts in HTML/SVG `srcDoc` iframes.
- Real React UI lacks browser-level smoke tests.
- API token is passed through query params in several frontend paths.

Implementation steps:

- Export and reuse `DomainNav` outside `TaskRail`, adding a mobile bottom navigation bar.
- Default artifact `iframe` sandbox to no scripts for generated HTML/SVG previews.
- Add a Playwright smoke test script in a later sub-batch once the local dev-server contract is settled.
- Track query-token replacement as a follow-up because it spans frontend, middleware, CLI, and channel clients.

Verification:

- `npm run build` in `webui/`.
- Targeted unit/smoke tests for Mission Control routes.
- Browser screenshot/E2E in the next sub-batch.

Progress:

- 2026-06-24: Exported `DomainNav` for mobile bottom navigation and hardened generated artifact iframes by removing script permission from HTML/SVG `srcDoc` previews.
- 2026-06-24: Migrated React Mission Control REST fetches from `?token=` URL parameters to `X-XMC-Token` headers for `apiGet`, `apiSend`, session hydration, pending question/fanout hydration, and voice TTS. WebSocket/media/raw resource URLs keep query-token compatibility where browsers cannot attach custom headers.
- 2026-06-24: Added Playwright browser smoke coverage for `/ui/` that starts a local FastAPI app and verifies the React root mounts in Chromium; Chromium is installed locally, so the browser smoke now runs instead of skipping.
- 2026-06-24: Added stable `data-domain` hooks to the domain navigation and a mobile Chromium E2E asserting all six Mission Control domains remain visible at phone width.
- 2026-06-24: Added a Cognition-page GraphState summary panel backed by `/api/v2/cognition/tasks/graph-state`, showing final status, confidence, node count, error count, and recent task nodes without replacing the existing task list.
- 2026-06-24: Hardened generated artifact rendering with a shared `artifactSecurity` sanitizer, DOMPurify, locked iframe CSP, sanitized Mermaid SVG insertion, and Markdown URL/image allowlists. React endpoint smoke coverage now has a separate Mission Control inventory extracted from `webui/src`, and the boot JS has a size-budget guard to keep heavy renderers lazy-loaded.
- 2026-06-24: Added abortable fresh GET support for high-churn panels and migrated System, Cognition, and Memory views to cancel stale requests on token/view/query changes, with source-level regression coverage plus browser smoke verification.
- 2026-06-24: Extended abortable/stale-response protection to Skills, ModelConfig, Cron, and Files views. Files now guards file-open responses with request identity so a slow previous file read cannot overwrite the currently selected file.
- 2026-06-24: Added frontend accessibility control primitives (`IconButton`, `ToggleButton`), strengthened segmented tabs with tablist state semantics, and added `aria-expanded` / `aria-pressed` coverage for core collapsible and toggle controls.
- 2026-06-24: Added a Team-page GraphState topology panel backed by `/api/v2/cognition/tasks/graph-state`, surfacing runnable/blocked/failed/cycle counts, node dependencies, graph errors, and abortable fetch behavior beside the existing fanout/subagent rounds.

## Batch 2: Tool Sandbox And History Policy

Status: in progress.

Problems:

- Builtin shell still executes in the host context.
- Docker sandbox exists for skills but is not a first-class policy for high-risk builtin tools.
- Tool result pruning exists, but there is no unified `ToolHistoryProcessor` policy surface.

Implementation steps:

- Add `tools.shell.execution_policy`: `host_guarded`, `docker`, `disabled`.
- Keep `host_guarded` default for compatibility; add clear UI/doctor warnings.
- Introduce `ToolHistoryProcessor` around old tool outputs and repeated error chains.
- Add named error templates for shell, JSON schema, browser action, path-not-found, and permission-denied failures.

Verification:

- Existing tool guard tests.
- New tests proving policy selection, timeout behavior, and no accidental host execution when `docker` is selected.

Progress:

- 2026-06-24: Added `xmclaw.cognition.tool_history.ToolHistoryProcessor` and wired tool-call outcomes in `ActionDispatcher` GraphState traces to compact tool-history entries instead of full raw output blobs.
- 2026-06-24: Added explicit builtin shell execution policy support (`host_guarded`, `docker`, `disabled`) with config parsing from `tools.shell.execution_policy`, config lint validation, and disabled-policy refusal.
- 2026-06-24: Wired a first-class Docker shell sandbox for builtin `bash`: no network, dropped capabilities, no-new-privileges, memory/CPU/PID limits, `/workspace` bind mount, configurable image via `tools.shell.sandbox_image`, and structured failure when Docker is unavailable instead of falling back to host execution.
- 2026-06-24: Extended Docker shell sandbox configuration to cover memory, CPU, PID, and network mode controls (`tools.shell.sandbox_memory`, `sandbox_cpus`, `sandbox_pids_limit`, `sandbox_network`) with strict linting and factory wiring.
- 2026-06-24: Fixed the `web_search` all-engines-failed path to use the shared `_fail_with_hint` helper instead of raising `NameError`, making the error template actionable again.
- 2026-06-24: Added named tool error templates for shell failures, timeouts, sandbox failures, missing paths, and permission-denied cases; wired bash non-zero exits, timeouts, and Docker sandbox unavailable paths to stable template kinds for retry/reflection logic.

## Batch 3: Graph State Runtime

Status: in progress.

Problems:

- `parallel_subagents` has DAG scheduling, but shared state and reducers are implicit.
- Planner/action dispatcher retry policy exists, but graph-node policy is not a uniform runtime contract.

Implementation steps:

- Add a small `GraphState` dataclass/protocol with reducers for subtasks, artifacts, errors, confidence, and final synthesis.
- Add node policy fields: timeout, retry count, cache key, and error handler.
- Bridge current `parallel_subagents` and `TaskScheduler` into the new state shape without replacing them.

Verification:

- Unit tests for reducer determinism.
- DAG/fanout tests prove previous behavior remains unchanged.

Progress:

- 2026-06-24: Added `xmclaw.cognition.graph_runtime` with `GraphState`, default reducers, `NodePolicy`, snapshot roundtrip, custom reducer override, and immutable identity checks.
- 2026-06-24: Wired `ActionDispatcher.execute_plan` to return a `GraphState` trace on `PlanExecutionResult`, mapping planned subtasks, tool results, messages, errors, confidence, and final status through reducers without changing existing execution behavior.
- 2026-06-24: Standardized per-step retry/timeout/cache/error-handler data into `GraphState.node_policies`, preserving existing planner behavior while creating one policy surface for future node execution.
- 2026-06-24: Added `agent.max_react_loop` as an explicit ReAct-loop alias for the existing `max_hops` guard, with factory precedence and config lint coverage.
- 2026-06-24: Reconnected the B-397 stuck-loop guard into the real hop loop so three consecutive identical `(tool, error_signature)` failures publish `anti_req_violation(kind="stuck_loop")` and exit before the generic no-progress guard or max-hop budget.
- 2026-06-24: Fixed instant-mode LLM retry backoff to interpret classifier schedules as milliseconds, matching the normal hop loop and preventing accidental 1000-second sleeps on transient/unknown failures.
- 2026-06-24: Added `TaskScheduler.snapshot_graph_state()` so persisted multi-agent task DAGs can produce the same GraphState shape as plan execution, and fixed SQLite dependency deserialization from JSON arrays.
- 2026-06-24: Added `GraphInspection` and `inspect_graph_state()` to detect runnable/blocked/failed nodes, missing dependencies, cycles, and missing node policies; TaskScheduler graph snapshots now include this inspection metadata.
- 2026-06-24: Added `GraphExecutor`, a lightweight policy-aware GraphState runtime that executes dependency waves with per-node timeout/retry/backoff/cache handling, records reducer updates, and converts node failures into structured graph errors.
- 2026-06-24: Wired `WorkerSwarm.execute_plan()` to return a GraphState trace for real multi-agent DAG execution, including worker result artifacts/messages, failure errors, inspection metadata, and deadlock/cycle failure semantics instead of empty-result success.
- 2026-06-24: Added `/api/v2/cognition/tasks/graph-state` so the operator UI and tests can fetch the canonical task GraphState snapshot directly, while preserving the older `/tasks/graph` nodes/edges endpoint.
- 2026-06-24: Migrated `WorkerSwarm.execute_plan()` from its bespoke ready/pending loop onto `GraphExecutor`, preserving the public `SwarmResult` contract while making dependency waves and `max_workers` concurrency flow through the shared graph runtime.
- 2026-06-24: Wired `swarm.max_subagents` and `swarm.task_timeout_s` into daemon `WorkerSwarm` construction and GraphState node policies, closing the gap between typed config validation and actual multi-agent runtime behavior.
- 2026-06-24: Hardened `GraphExecutor` suspension semantics: node handlers that return their own subtask as `pending`, `blocked`, or `waiting` now pause the graph with `final=pending` instead of being treated as completed and re-run in a loop.

## Batch 4: Summarizer Agent And Eviction

Status: in progress.

Problems:

- Memory has hybrid recall, reflection, and compression, but not Letta-style summarizer-owned partial eviction.

Implementation steps:

- Add `SummarizerAgent` for long sessions and tool-heavy trajectories.
- Store summary provenance: session id, source message range, eviction ratio, model/profile, and timestamp.
- Protect user/assistant/tool-call boundaries during partial eviction.

Verification:

- Tests for replay integrity, summary provenance, and memory recall after eviction.

Progress:

- 2026-06-24: Added `xmclaw.context.summarizer_eviction` with a pure `SummarizerEvictionPlanner`, serializable `SummarizerEvictionPlan`, provenance, protected indices, eviction ranges, latest-user protection, and assistant/tool-call boundary alignment.
- 2026-06-24: Wired `ContextCompressor` to produce `last_eviction_plan` and to consume the plan for context-aware preservation while keeping the existing compression behavior stable.
- 2026-06-24: Added `SummarizerAgent`, an LLM-backed compressor callable with a dedicated system role, timeout boundary, redaction, and failure-to-`None` fallback; `HistoryCompressionMixin` now lazy-wires this agent instead of calling the LLM directly.
- 2026-06-24: Rechecked the memory retrieval audit finding: BM25 + RRF hybrid recall already existed, but `/api/v2/memory/unified_query` still used plain vector recall. The unified API now defaults to `recall_hybrid`, exposes `recall_mode=vector` for debugging, and passes layer/time filters into the hybrid path.
- 2026-06-24: Added first-class `summary_provenance` to summarizer eviction plans and `summarizer_eviction_planned` events, carrying session id, source message range, source/summarized/preserved indices, evict ratio, model profile, timestamp, and summary kind for checkpoint/replay audits.

## Batch 5: Reflexion-Style Failure Critique

Status: in progress.

Problems:

- Reflection exists but is cadence/session oriented; failed trajectories need immediate structured self-critique.

Implementation steps:

- Add a self-critique prompt template with dimensions: plan quality, tool choice, evidence, safety, user fit, retry decision.
- Trigger on failed turn, max-hop exit, stuck-loop exit, and low grader score.
- Materialize accepted critiques into long-term memory/persona with rate limits.

Verification:

- Tests for trigger conditions and materialization caps.

Progress:

- 2026-06-24: Added `xmclaw.cognition.self_critique` with Reflexion-style dimensions, strict JSON prompt schema, parser normalization, and a bounded memory-candidate policy.
- 2026-06-24: Wired failed `ActionDispatcher.execute_plan` results to produce `SelfCritiqueRequest` from GraphState snapshots and compact trajectory events, without automatically calling an LLM or writing memory.
- 2026-06-24: Added `SelfCritiqueMaterializer` and `SelfCritiqueEngine` so approved Reflexion critiques can flow from request -> critic JSON -> bounded long-term `lesson` memory writes through an injected memory service, without adding daemon or memory-layer reverse dependencies.
- 2026-06-24: Connected `ActionDispatcher` failure paths to an optional injected `SelfCritiqueEngine`, passing the configured critic callable and memory service, while swallowing critique failures so the original plan result remains stable.
- 2026-06-24: Added daemon runtime wiring for self-critique: fast/balanced auxiliary LLMs are wrapped as critic callables, memory is resolved lazily after `memory_v2` startup, and `cognition.self_critique.enabled` is linted as a boolean.
- 2026-06-24: Extended Reflexion triggers from autonomous plan failures into foreground AgentLoop exits: `max_hops_exit`, `stuck_loop_exit`, and generic failed turns now build compact `SelfCritiqueRequest` objects from the user message, hop count, tool trajectory, and failure summary, then run the same injected `SelfCritiqueEngine` best-effort without changing the user-visible turn result.
- 2026-06-24: Emitted `self_critique_requested` for foreground AgentLoop Reflexion triggers as well as ActionDispatcher plan failures, with payloads carrying trigger, session, goal, failure summary, trajectory count, graph-state summary, and `source=agent_loop`.
- 2026-06-24: Added a per-session materialization cap to `SelfCritiqueMemoryPolicy` so repeated failures cannot flood long-term memory with Reflexion lessons; accepted candidates now carry write-count and write-limit metadata.

## Batch 6: Observability And Config Typing

Status: in progress.

Problems:

- Config validation exists but is hand-rolled and uneven.
- Event observability exists but lacks some runtime-specific exit/reducer/sandbox events.

Implementation steps:

- Add structured events for ReAct iteration exit, graph reducer updates, sandbox policy decisions, and summarizer eviction.
- Decide whether to move config to Pydantic or strengthen current validator with typed models at boundaries.

Verification:

- Config lint tests.
- Event shape tests.
- Mission Control API shape tests.

Progress:

- 2026-06-24: Added runtime event contracts for `graph_state_updated`, `self_critique_requested`, `summarizer_eviction_planned`, and `tool_sandbox_policy_decided`.
- 2026-06-24: Wired `ActionDispatcher` to emit compact `graph_state_updated` events after reducer application and `self_critique_requested` events on failed plan trajectories.
- 2026-06-24: Added config lint coverage for `tools.shell.execution_policy`.
- 2026-06-24: Verified stuck-loop front/back event contract: daemon payload keeps `message`, `tool`, `error_signature`, `hop`, and `kind`; reducer renders the daemon message instead of a generic fallback.
- 2026-06-24: Wired `summarizer_eviction_planned` into the AgentLoop compression path and `tool_sandbox_policy_decided` into the real bash invocation path, with unit coverage for both runtime event contracts.
- 2026-06-24: Extended `tool_sandbox_policy_decided` payloads with `sandbox_runtime` and Docker image metadata so shell audit events distinguish host guarded execution from containerized execution.
- 2026-06-24: Updated AgentLoop curriculum-hint tests to reflect the current context-hygiene architecture: dynamic hint blocks are visible to the LLM in system context, while the user message remains clean.
- 2026-06-24: Added a Pydantic v2 typed-config overlay for high-risk blocks (`tools.shell`, `agent.max_react_loop`, `cognition.self_critique`) and wired its strict validation errors into `lint_config` while preserving legacy dict compatibility.
- 2026-06-24: Expanded the typed-config overlay to `cognition.memory_v2.retention`, `curator`, and `write_decision`, using strict bool/int/float/list fields for memory maintenance controls.
- 2026-06-24: Extended the typed-config overlay to `swarm` runtime controls (`enabled`, `max_subagents`, `max_depth`, `task_timeout_s`, `synthesize`) so multi-agent planning limits fail fast during config validation.
- 2026-06-24: Documented that Skill Auto-Propose already exists through `SkillProposer`, `SkillDreamCycle`, realtime evolution, `skill_propose`, and untrusted proposal/audit flows; extended typed config coverage to `evolution.skill_dream`, `evolution.realtime`, and `cognition.skill_proposer`.
- 2026-06-24: Added typed config coverage for `cognition.auto_recall` (`enabled`, `use_hybrid`, `timeout_s`, `min_similarity`) so per-turn memory recall policy fails fast under Pydantic validation instead of relying only on hand-written lint branches.
- 2026-06-24: Fixed typed-config compatibility aliases for shell sandbox settings: explicit `tools.shell.image/memory/cpus/pids_limit/network` and top-level `tools.shell_execution_policy` now resolve correctly when canonical `sandbox_*` keys are absent, instead of being masked by default canonical values.
- 2026-06-24: Extended typed-config coverage to `cognition.continuous_loop` (`autonomy_level`, `heartbeat_hz`) and `tools.invoke_timeout_s`, moving self-organization heartbeat and tool hang boundaries into the strict Pydantic overlay while preserving existing lint behavior.

## Current Priority Order

1. Batch 1 frontend P0 hardening.
2. Batch 2 shell/tool sandbox policy.
3. Batch 3 graph state runtime.
4. Batch 4 summarizer eviction.
5. Batch 5 failure critique.
6. Batch 6 observability/config polish.
