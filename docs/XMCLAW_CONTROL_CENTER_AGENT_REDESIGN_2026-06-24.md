# XMclaw Control Center and Agent Runtime Redesign

Date: 2026-06-24

## Why This Exists

The backend already has capable subsystems: pairing auth, feature flags,
Memory v2, GraphState, task scheduling, prompt sections, voice providers,
tool guards, and self-critique. The product problem is that many of these
are configured through `daemon/config.json`, legacy static panels, or
implicit defaults. Users need a first-class frontend control center.

This document turns the current complaints into one roadmap:

- Security and guardrails need visible user controls, not hidden defaults.
- TTS, STT, embedding/vector backend, memory switches, and default feature
  toggles should be configurable from the frontend wherever safe.
- Memory, planning, tool use, reasoning, and prompt engineering should move
  toward one graph-state runtime instead of scattered local loops.

## 2026-06-25 Runtime Re-Anchor

Do not treat a single incident such as "WeChat was on another drive" as the
main design target. That incident only exposed the real kernel failures:
task state is not durable enough, produced artifacts are not tracked, failed
strategies repeat, and automatic memory writes can solidify unfinished or
wrong trajectories.

The Agent Runtime redesign has six layers:

1. **Task Runtime**: owns goal, plan, current step, done condition, artifacts,
   verification status, failures, retries, and user-facing progress.
2. **Planner / Graph Runtime**: LangGraph-style state graph with nodes,
   reducers, conditional edges, retry policy, timeout, cache, and error
   handlers.
3. **Tool Strategy Layer**: tools are selected through history, constraints,
   prior failures, alternative routes, and artifact tracking instead of direct
   one-shot LLM guesses.
4. **Memory Governance**: raw events, episodes, pending candidates, verified
   facts, and core memory are separate. Unfinished tasks, failed probes, and
   assistant speculation cannot directly become long-term memory.
5. **Skill Runtime**: skills are discovered, scored, selected, skipped with
   structured reasons, invoked, and reviewed. Installation alone is not enough.
6. **Control Center / UI**: users can inspect and configure guardrails, model
   profiles, voice, vector models, memory extraction, skill autonomy, retry
   policy, and runtime switches without editing files.

Correct implementation order:

1. **Artifact Ledger**: every download/generation/install/move/extract/write
   records `path`, `source_url`, `expected_version`, `actual_version`,
   `checksum`, `target_drive`, `verified`, and owning task/step.
2. **Task State**: every non-trivial user request gets explicit task state and
   completion criteria; follow-up checks read from state instead of rediscovering
   where outputs landed.
3. **Failure Strategy Switch**: repeated failures with the same tool/command/
   search scope trigger `change_plan`, `alternative_tool`, `ask_user`, or
   `stop`, not another identical retry.
4. **Pending Memory Candidates**: automatic extraction writes candidates with
   evidence and skip reasons; durable facts require verified outcomes, repeated
   evidence, or user approval.
5. **Skill Selection Engine**: output structured candidates, selected skill,
   skip reasons, confidence, missing inputs, and invocation result.
6. **StateGraph Migration**: once state, artifacts, memory governance, and
   skill selection are explicit, migrate the main loop to a graph runtime.

## External Design Anchors

Primary references checked on 2026-06-24:

- LangGraph `StateGraph`: nodes read/write shared state, and state keys can
  define reducers to aggregate updates from multiple nodes.
  https://langchain-ai.github.io/langgraphjs/reference/classes/langgraph.StateGraph.html
- LangGraph `Send`: conditional edges can dynamically fan out map-reduce
  style node work with different state packets.
  https://langchain-ai.github.io/langgraphjs/reference/classes/langgraph.Send.html
- LangChain v1 agents are built on LangGraph and inherit persistence,
  streaming, human-in-the-loop, and time-travel style runtime properties.
  https://docs.langchain.com/oss/python/releases/langchain-v1
- LangChain middleware is the customization primitive for prompt
  transformation, tool selection, retries, fallbacks, early termination,
  rate limits, guardrails, and PII detection.
  https://docs.langchain.com/oss/python/langchain/middleware/overview
- LangChain HITL intercepts sensitive tool calls with configurable policy
  and pauses execution through durable interrupts/checkpoints.
  https://docs.langchain.com/oss/python/langchain/human-in-the-loop
- LangChain short-term memory is part of graph state and persisted with a
  checkpointer per thread.
  https://docs.langchain.com/oss/python/langchain/short-term-memory
- Letta memory blocks are persistent prompt-visible sections, always in
  context without retrieval.
  https://docs.letta.com/guides/core-concepts/memory/memory-blocks/
- Letta archival memory is semantically searchable long-term storage queried
  on demand via tools/API.
  https://docs.letta.com/guides/core-concepts/memory/archival-memory/
- MemGPT proposes OS-style virtual context management with memory tiers and
  control-flow interrupts to extend effective context.
  https://arxiv.org/abs/2310.08560
- SWE-agent's strongest lesson is not "more tools"; it is a tight
  Agent-Computer Interface: constrained edit/navigation/test affordances,
  repeatable YAML configuration, and error/history shaping around the
  software-engineering loop.
  https://swe-agent.com/latest/
  https://arxiv.org/abs/2405.15793
- OpenAI Agents SDK makes the runtime nouns explicit: agent instructions,
  tools, handoffs, guardrails, structured output, tracing, and run hooks.
  https://openai.github.io/openai-agents-python/agents/
  https://openai.github.io/openai-agents-python/guardrails/
- Microsoft Agent Framework/AutoGen emphasizes production orchestration:
  session state, type safety, filters, telemetry, deterministic workflows,
  dynamic multi-agent collaboration, and distributed/event-driven agents.
  https://learn.microsoft.com/en-us/agent-framework/overview/
  https://microsoft.github.io/autogen/stable/
- MetaGPT's useful abstraction is `Role = Actions + Memory + think/act
  strategy`, with an explicit state-machine style `_think` step.
  https://github.com/FoundationAgents/MetaGPT
  https://docs.deepwisdom.ai/v0.7/en/guide/tutorials/agent_101.html
- CrewAI separates collaborative "crews" from deterministic "flows", then
  layers memory, knowledge, guardrails, observability, async execution, and
  A2A-style integration on top.
  https://docs.crewai.com/
- OpenClaw's important runtime idea is provider/runtime separation: the
  runtime owns prepared model loops, prompt assembly, tool calls, finished
  turns, progress, retries, fallback, and channel state durability.
  https://github.com/openclaw/openclaw
  https://docs.openclaw.ai/concepts/agent-runtimes
- Hermes Agent is a direct benchmark for XMclaw's skill system: it treats
  self-improvement, skill creation/improvement, past-conversation search,
  knowledge persistence nudges, and user modeling as built-in loops.
  https://hermes-agent.nousresearch.com/docs/
  https://github.com/NousResearch/Hermes-Agent
- Claude Skills shows the product contract users expect: skills are modular
  packages with metadata/resources, and the agent automatically uses them
  when relevant instead of waiting for a manual tool name.
  https://code.claude.com/docs/en/skills
  https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview

## Top Implementation Lessons

The practical design target is a runtime kernel, not a larger prompt.

| Implementation | What To Copy | XMclaw Landing Zone |
| --- | --- | --- |
| LangGraph | StateGraph, reducers, conditional edges, checkpointable execution | `GraphState` becomes the AgentLoop contract |
| SWE-agent | ACI, error/history templates, constrained tool loop | tool sandbox, history processor, edit/test workflow templates |
| OpenAI Agents SDK | tools, handoffs, guardrails, tracing as first-class runtime objects | local middleware and typed run events |
| Microsoft Agent Framework / AutoGen | typed workflows, session state, telemetry, distributed multi-agent runtime | durable sessions, typed node policies, swarm orchestration |
| MetaGPT | `_think`, state machine, role/action/memory binding | explicit reasoning node and mode router |
| CrewAI | crews for collaboration, flows for deterministic control | separate multi-agent collaboration from deterministic task DAGs |
| OpenClaw | runtime/provider split and channel state surviving retries/fallbacks | `AgentRuntime` owns loop state; providers only generate/stream |
| Hermes | self-improvement and skill learning as built-in loops | autonomous skill discovery, skill proposal, skill usage telemetry |
| Claude Skills | automatic skill activation by metadata/relevance | deterministic `SkillDiscoveryMiddleware` |

## Current Gaps Confirmed In Code

- React `webui/src` exposes model profiles, but not a unified settings or
  control center.
- Voice chat uses browser STT plus `/api/v2/voice/tts`, but provider/model
  and voice selection are not surfaced in React.
- Legacy static `settings_audio.js` and `memory_facts_v2_embedder.js` already
  show the user pain: embedding config is visible but read-only and tells
  users to edit `daemon/config.json`.
- `security.prompt_injection`, `security.guardians`,
  `tools.shell.execution_policy`, and allowed directories exist in config,
  but React has no clear guardrail dashboard.
- `xmclaw.cognition.graph_runtime` and `TaskScheduler.snapshot_graph_state()`
  already provide LangGraph-like primitives, but they are not yet the
  canonical runtime path for every agent turn.
- `prompt_builder.py` has sections and version comments, but the registry is
  still embedded in code, not editable, previewable, testable, or
  token-budgeted from UI.
- Skill tooling exists (`SkillToolProvider`, semantic prefilter, trigger
  engine, active routing), but the current path is still mostly probabilistic:
  skills are made visible to the model and hinted, yet the runtime does not
  enforce a deterministic discovery/load/skip decision for each turn.

## UX Target: Control Center

Entry point: add a top-level React view named `control` / "控制中心".

Primary tabs:

- Security: prompt-injection policy, guardians enable switch, severity policy,
  shell mode, sandbox image/resources, browser/computer-use toggles, allowed
  directories, pending approvals.
- Models: LLM profiles, default routing, embedding/vector model, reranker,
  STT, TTS, provider reachability tests.
- Memory: Memory v2 enable, recall top-k, hybrid recall, BM25/vector/reranker
  switches, write decision, curator, retention, summarizer eviction,
  self-critique memory caps.
- Agent Runtime: `max_react_loop`, `max_hops`, mode (`REACT`, `BY_ORDER`,
  `PLAN_AND_ACT`), task retry/timeout/cache policy, subagent fanout,
  graph-state visualization.
- Skills: disclosure mode, semantic discovery, autonomous invocation mode
  (`suggest`, `prefer`, `force`), max loaded skills per turn, trust state,
  pending proposals, skill usage/skip telemetry.
- Prompts: prompt layer registry, enabled/disabled sections, per-section
  version, rendered preview, diff, token count, replay tests.
- Diagnostics: config validation, restart-required banner, last reload, active
  build status, degraded subsystem warnings.

## Backend Target: Config Control API

New API surface:

- `GET /api/v2/config/control`: return grouped safe config values with secrets
  redacted.
- `PATCH /api/v2/config/control`: accept dotted-path patches, validate against
  an allowlist, write config atomically, update `app.state.config`, and return
  `restart_required`.
- `POST /api/v2/config/control/validate`: validate a hypothetical patch without
  writing.

Rules:

- Secrets are never returned raw.
- Unknown paths are rejected.
- Env-overridden paths are shown as locked where detectable.
- Runtime-hot paths update `app.state.config`; boot-time paths return
  `restart_required=true`.
- Every write produces a small backup and audit event.

## Agent Runtime Redesign

Canonical state envelope:

- `messages`: user/assistant/tool observations.
- `memory_hits`: recalled facts/passages with provenance and score.
- `prompt_layers`: rendered prompt sections and token budgets.
- `subtasks`: plan graph nodes with dependencies and status.
- `tool_results`: normalized tool calls, errors, artifacts, and retry metadata.
- `decisions`: planner decisions and routing rationale summaries.
- `errors`: structured failure records.
- `final`: final response candidate.

Canonical nodes:

- `input_normalize`
- `safety_scan`
- `memory_recall`
- `prompt_compose`
- `planner`
- `tool_select`
- `tool_execute`
- `observe_reduce`
- `self_critique`
- `summarize_evict`
- `final_synthesis`

Conditional edges:

- unsafe action -> approval interrupt
- missing info -> ask user
- tool failure -> retry with policy, then fallback, then escalate
- context pressure -> summarize/evict
- plan complete -> final synthesis

Implementation direction:

- Keep the existing dependency-free `GraphState` and reducers.
- Promote `GraphState` from task visualization to the main AgentLoop internal
  contract.
- Add checkpoint snapshots per session/run so a paused HITL approval can
  resume.
- Treat LangChain-style middleware as local composable hooks: `before_model`,
  `after_model`, `before_tool`, `after_tool`, `on_error`, `on_finish`.

## Skill Autonomy Redesign

Current state:

- `SkillToolProvider` exposes promoted skills as tools.
- `prefilter.py` and `semantic_index.py` reduce the 100+ skill list to
  relevant candidates.
- `trigger_engine.py` can force-inject skills by keyword/event/cron.
- `agent_loop.py` active routing inserts matched skills and a hint.

Gap:

- This is still a model-choice hint, not a runtime decision. If the model
  ignores the hint, the skill is skipped silently.

Target middleware:

1. `skill_discover`: before model call, retrieve candidates from trigger,
   lexical, semantic, path, and recent-success signals.
2. `skill_gate`: apply trust/sandbox policy, config mode, and confidence.
3. `skill_load`: in `prefer/force`, inject an explicit system block requiring
   the model to call the matched skill or record a skip reason.
4. `skill_observe`: emit `skill_considered`, `skill_invoked`, `skill_skipped`,
   `skill_failed`, and `skill_succeeded` events.
5. `skill_learn`: raise/lower future relevance scores from real outcomes and
   feed successful repeated trajectories into skill auto-propose.

Configuration:

- `skills.semantic_discovery.enabled`
- `skills.semantic_discovery.floor`
- `skills.autonomous_invocation.enabled`
- `skills.autonomous_invocation.mode`: `suggest | prefer | force`
- `skills.autonomous_invocation.min_score`
- `skills.autonomous_invocation.max_loaded`

## Memory Redesign

Dedicated memory/event plan:

- `docs/XMCLAW_MEMORY_EVENT_REDESIGN_2026-06-24.md`

Memory layers:

- Core blocks: persona/user/project constraints that are always prompt-visible,
  matching Letta memory blocks.
- Working/session memory: short-lived active context.
- Archival memory: semantic/BM25 searchable facts/passages, matching Letta
  archival memory and MemGPT slow memory.
- Procedural memory: skills, workflows, tool lessons.
- Pinned memory: user-approved never-evict facts.

Read path:

1. Classify query and task intent.
2. Apply metadata filters.
3. Retrieve with vector + BM25.
4. Fuse with RRF.
5. Optionally rerank with cross-encoder/LLM judge.
6. Pack context under a token budget with source provenance.

Write path:

1. Extract candidate observations from turn trajectory.
2. Decide whether to write.
3. Deduplicate/merge with nearest neighbors.
4. Assign kind/scope/layer/bucket/confidence.
5. Persist with provenance.
6. Schedule curator/summarizer for compression and eviction.

## Prompt Engineering Redesign

Move from code-only prompt sections to a `PromptRegistry`:

- each section has `id`, `version`, `scope`, `mode`, `enabled`, `template`,
  `required_context`, `token_budget`, `risk_level`;
- rendered prompts can be previewed in UI;
- changes are versioned and diffable;
- prompt tests replay stored trajectories;
- safety/tool/memory/persona/planning sections are separate.

## Implementation Phases

P0, now:

- Add config control API.
- Add React Control Center shell.
- Surface guardrails, shell policy, voice, embedding, memory, runtime toggles.
- Surface skill discovery and autonomous invocation toggles.
- Inject a `skill-autonomy` system block when active routing finds relevant
  skills, with `suggest/prefer/force` policy.
- Extract skill autonomy policy parsing/rendering into
  `xmclaw.skills.autonomy`, with unit tests.
- Add validation and restart-required feedback.

P0 status:

- Done: config control API, route mounting, React Control Center, skill config
  tab, skill-autonomy system block, `xmclaw.skills.autonomy` helper, targeted
  unit tests, frontend production build.
- Done: `xmclaw.skills.discovery.SkillDiscoveryMiddleware` now emits
  structured candidates, telemetry-ready discovery events, skip-reason
  requirements, and an explicit `skill_browse -> skill_view -> skill_run`
  self-query path when no direct candidate fits.

P1:

- Wire Control Center forms to config control API.
- Add provider test buttons for TTS/STT/embedder.
- Add config-backed UI for feature flags and security policy.
- Add skill usage telemetry and skip reasons to the frontend.
- Move skill discovery into a standalone `SkillDiscoveryMiddleware` with
  deterministic events and tests.
- Done: replace the legacy inline active-routing block in AgentLoop with
  `SkillDiscoveryMiddleware.discover(...)`; skill discovery now has one primary
  runtime path.

P2:

- Promote GraphState to AgentLoop execution contract.
- Add checkpoints and durable HITL interrupts.
- Convert existing retry/cache/error handling into node policies.

P3:

- Rebuild memory read/write pipeline with hybrid retrieval, provenance packing,
  summarizer eviction, and UI tuning.
- Add memory replay/eval tests.

P4:

- Add PromptRegistry, prompt preview, prompt replay, and section-level UI.

## Acceptance Criteria

- A user can configure security posture without editing files.
- A user can switch TTS/STT/embedding/vector options from the frontend.
- A user can see which config changes require daemon restart.
- Memory, planning, tool use, and prompting have one visible runtime graph.
- No new scattered config endpoints for each feature; safe settings go through
  the config control API.
