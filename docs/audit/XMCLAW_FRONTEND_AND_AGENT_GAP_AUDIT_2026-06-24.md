# XMclaw Gap Audit: Agent Runtime + Frontend

Date: 2026-06-24
Scope: v2.0/v2.1/v2.3 external gap notes, current XMclaw code, Mission Control frontend.

## Executive Summary

The prior audits correctly identify several backend/runtime gaps: explicit ReAct loop exits, graph-state multi-agent planning, summarizer-driven memory eviction, tool sandbox/history processors, and Reflexion-style self-critique. The biggest missing area is frontend operational quality. Mission Control builds and key smoke tests pass, but current coverage is mostly API-shape and shell-serving coverage. It does not yet prove that the real React UI works across browser states, screen sizes, streaming event sequences, artifact previews, keyboard flows, or mobile navigation.

Follow-up recheck: the external audit is based on leading implementation source/paper research, so its target architecture is still valuable. Several XMclaw-specific statements are directionally useful but outdated. XMclaw already has `max_hops` loop caps, stuck-loop detection, DAG-aware subagent fanout, BM25 hybrid recall, reflection materialization, config linting, an MCP stdio server, and Docker skill runtime support. The remaining gaps are narrower: make those mechanisms first-class product/runtime primitives, harden frontend artifact boundaries, and add browser-level UI verification.

Verification run:

- `npm run build` in `webui/`: passed.
- `python -m pytest tests/unit/test_v2_phase10_mission_control.py tests/integration/test_v2_ui_endpoint_smoke.py -q`: 24 passed.
- Vite warning: several chunks exceed 500 kB, notably Mermaid/Cytoscape/KaTeX-related chunks.

## External Audit Recheck

| External claim | Recheck result | Code evidence | Corrected priority |
|---|---|---|---|
| ReAct has no explicit exit protection. | Outdated. The main loop has `max_hops`, config validation, human-facing max-hop fallback, and a 3x same-error stuck-loop guard. What is still missing is a named ReAct state machine with explicit exit-reason events. | `xmclaw/daemon/agent_loop.py:288`, `xmclaw/daemon/agent_loop.py:3720`, `xmclaw/daemon/agent_loop.py:4286`, `xmclaw/daemon/config_schema.py:622` | P1, not P0 |
| Multi-agent planning is only flat parallel. | Partly outdated. `parallel_subagents` supports DAG dependencies, nested fanout depth, per-subagent hop caps, timeouts, and partial failure aggregation. Still not equivalent to LangGraph `StateGraph`: no typed shared graph state, reducer registry, checkpointed conditional-edge runtime, or durable graph execution model. | `xmclaw/providers/tool/builtin_subagent.py:1`, `xmclaw/providers/tool/builtin_subagent.py:255`, `xmclaw/providers/tool/builtin_subagent.py:515`, `xmclaw/cognition/planner.py:50`, `xmclaw/cognition/action_dispatcher.py:202`, `xmclaw/cognition/task_scheduler.py:1` | P0/P1 hybrid |
| Memory retrieval is pure cosine. | False for current code. Memory V2/V3 has vector + BM25 + RRF hybrid recall, a prebuilt BM25 index, graph expansion, contradiction-aware recall, and three-factor reranking. Missing piece is a learned cross-encoder/reranker and Letta-style summarizer-driven message eviction. | `xmclaw/memory/v2/bm25.py:1`, `xmclaw/memory/v2/service.py:2384`, `xmclaw/memory/v2/service.py:2579`, `xmclaw/memory/v2/gateway.py:223` | P1/P2 |
| Memory has no summarization. | Too broad. Context compression, dream/session/reflection summarization, and memory consolidation exist. What is still missing is a dedicated SummarizerAgent that owns partial eviction of long conversation/tool trajectories with replay provenance. | `xmclaw/context/compressor.py`, `xmclaw/cognition/reflection_cycle.py:1`, `xmclaw/cognition/reflection_materializer.py:1` | P1 |
| Tool system has no retry templates. | Partly outdated. `ErrorAwareRetryProvider` adds LLM-guided fixups with structured JSON actions and multi-attempt backoff. There is still no SWE-agent-like library of named per-tool error templates or history processor policy exposed as a first-class runtime layer. | `xmclaw/providers/tool/retry_aware.py:1`, `xmclaw/providers/tool/retry_aware.py:116` | P1 |
| Tool sandbox is only host shell. | Partly true. Builtin shell still executes on host shell with guardrails and timeouts. However, skill runtimes include Docker with no-network/read-only/memory/CPU defaults. The gap is promoting sandbox policy to high-risk builtins like shell/python/browser, not merely adding Docker somewhere. | `xmclaw/providers/tool/builtin_shell.py:91`, `xmclaw/providers/runtime/docker.py:1`, `xmclaw/providers/runtime/docker.py:121` | P0/P1 |
| Config has no schema validation. | Outdated. There is hand-rolled config validation with clear `ConfigError`s and tests, though not Pydantic. The accurate gap is typed config models and config-doc synchronization, not total absence. | `xmclaw/daemon/config_schema.py:1`, `xmclaw/daemon/config_schema.py:622`, `tests/unit/test_config_schema.py` | P2 |
| Reflection is only daemon ticks and does not affect memory/prompt. | Partly outdated. Reflection cycles, metacognition, memory consolidation, and `ReflectionMaterializer` exist and can write back into persona/memory files. Still missing: Reflexion-style self-critique attached to every failed trajectory with stable scoring dimensions. | `xmclaw/cognition/reflection_cycle.py:1`, `xmclaw/cognition/reflection_materializer.py:1`, `xmclaw/daemon/app_lifespan.py:1447` | P1 |
| MCP is only a client. | Outdated. There is an MCP stdio server plus MCP bridge clients. Remaining gap is modern MCP surface completeness and conformance, not server absence. | `xmclaw/mcp/server.py:1`, `xmclaw/mcp/server.py:20`, `xmclaw/providers/tool/mcp_bridge.py:48`, `xmclaw/providers/tool/mcp_http_bridge.py:40` | P2 |
| Channel / OpenClaw-style integration is missing. | Outdated. There is a channel abstraction and adapters/tools for Feishu, DingTalk, WeCom, Telegram, Slack, Discord, Email, ACP, and WS. Gaps are production-grade auth/reconnect/rate-limit parity by channel and missing WhatsApp-like coverage, not a missing channel layer. | `xmclaw/providers/channel/base.py:35`, `xmclaw/providers/channel/dingtalk/adapter.py`, `xmclaw/providers/channel/email/adapter.py`, `xmclaw/providers/tool/integrations.py` | P2 |
| Frontend gaps were not covered by external audit. | Confirmed. Primary P0 frontend risks are mobile navigation loss, no Playwright/E2E browser smoke, artifact preview hardening, endpoint inventory drift from legacy static UI, and query-param token leakage. | `webui/src/components/TaskRail.tsx:64`, `webui/src/components/MermaidView.tsx:55`, `webui/src/components/WorkspacePanel.tsx:55`, `webui/src/lib/api.ts:16`, `webui/package.json` | P0 |

## Priority Table

| Priority | Theme | Gap | Impact | First Fix |
|---|---|---|---|---|
| P0 | Frontend navigation | `TaskRail` owns domain nav but is hidden below `md`; mobile users lose access to Memory/Skills/Files/Team/System. | Mobile UI becomes task-only. | Add mobile bottom nav or unhide compact domain nav outside `TaskRail`. |
| P0 | Frontend test realism | No browser E2E or visual regression for the React app; `webui/package.json` has only `dev/build/preview`. | Layout, streaming, keyboard, and mobile regressions can ship green. | Add Playwright smoke: boot app, mock/real TestClient server, desktop + mobile screenshots. |
| P0 | Endpoint inventory drift | `test_v2_ui_endpoint_smoke.py` inventory is documented as extracted from legacy static pages, while primary UI now lives in `webui/src`. | New React API calls can 404/422 without the inventory catching them. | Generate inventory from `webui/src` or maintain a new Mission Control endpoint test. |
| P0 | Artifact/XSS boundary | Markdown/Mermaid/HTML/SVG artifacts accept LLM/tool-originated content; Mermaid uses `dangerouslySetInnerHTML`, HTML/SVG use `srcDoc` with scripts allowed. | A malicious tool result or artifact can execute inside preview contexts; sandbox is partial. | DOMPurify sanitize SVG/HTML, stricter iframe sandbox/CSP, explicit trusted artifact kinds. |
| P1 | Bundle size | Mermaid diagram submodules, Cytoscape, KaTeX and core Mermaid chunks exceed Vite warning threshold. | Slow first interaction on graph/markdown-heavy UI, especially mobile. | Manual chunks and lazy-load heavy renderers only per view/feature. |
| P1 | API cancellation | React views fire async loads without AbortController/request identity guards in several places. | Stale responses can overwrite newer view/session state. | Adopt a `useSafeQuery` hook for all view fetches. |
| P1 | Accessibility | Many icon-only controls rely on `title`, hover-only affordances, emoji glyphs, and missing `aria-pressed` on toggles. | Keyboard/screen-reader use is fragile. | Standardize IconButton/ToggleButton components with labels, pressed state, and focus behavior. |
| P1 | State durability | Session/sidebar state is spread across localStorage, server sync, WS replay, REST hydration, and reducer heuristics. | Refresh/reconnect edge cases can duplicate/drop cards. | Define a state hydration protocol and test replay + REST interleavings. |
| P1 | Token handling | API and media auth use `?token=` query params. | Tokens may leak into logs/history/referrer-like surfaces. | Prefer auth header or same-origin httpOnly/session-style mechanism; redact query token everywhere. |
| P2 | Design system | Tokens are minimal and one dominant purple/dark palette drives most states. | Dense operational UI works, but status hierarchy and domains blur together. | Add semantic surface/status/domain tokens and component primitives. |

## Backend Runtime Gaps From v2.x Notes

### ReAct Loop

Current posture: system-prompt ReAct plus tool-hop budget. The audit gap is not "no loop at all"; it is the lack of a named, inspectable ReAct state machine.

Recommended implementation:

- Add `max_react_loop` or reuse `agent.max_hops` as a user-visible explicit loop cap.
- Emit structured events for `react_iteration_started`, `react_iteration_finished`, `react_exit_reason`.
- Keep compatibility with current `AgentLoop` and `hop_loop`; do not fork a second orchestration path.

### Multi-Agent Planning

Current posture: flat `parallel_subagents` / team mode. The high-value gap is not parallelism; it is shared state, reducers, conditional edges, retry policy, and node-local failure handling.

Recommended implementation:

- Introduce a small `GraphState` protocol before adopting full Pregel/BSP.
- Start with reducers for: subtasks, artifacts, tool results, errors, confidence, final synthesis.
- Add node policy: timeout, retry count, cache key, error handler.

### Memory

Current posture: V2/V3 fact store, buckets, LanceDB, optional BM25, reflection/session extractors. The real missing part is summarizer-agent eviction over conversation buffers.

Recommended implementation:

- Add SummarizerAgent for long sessions and tool-heavy trajectories.
- Use partial eviction by ratio with assistant-boundary protection.
- Store summary provenance and evicted message ranges so replay/audit remains possible.

### Tools

Current posture: broad tool surface and optional guardians. The gap is execution isolation, error-template feedback, and history pruning for tool-heavy turns.

Recommended implementation:

- Add a `ToolHistoryProcessor` that compresses old tool outputs and keeps error/success summaries.
- Promote Docker/process sandbox from optional skill runtime to shell/python/browser tool execution policy where feasible.
- Add per-tool error templates such as `shell_check_error_template`, `json_schema_retry_template`, and `browser_action_recover_template`.

### Reflection

Current posture: reflection daemon/session reflection exists. The gap is Reflexion-style per-failed-trajectory verbal self-critique with stable dimensions.

Recommended implementation:

- Add self-critique prompt templates for: plan quality, tool choice, evidence, safety, user fit, retry decision.
- Trigger on failed or low-score trajectories, not only ticks/session close.
- Write distilled lessons into long-term memory with source trajectory ids and expiry policy.

## Frontend Findings

### P0: Mobile Has No Domain Navigation

`webui/src/App.tsx` renders `TaskRail`, then the active main panel. `TaskRail` is hidden under `md` via `hidden md:flex`, and `DomainNav` is nested inside it. On small screens, users cannot switch to Memory, Skills, Files, Team, or System.

Fix:

- Move domain navigation to an app-level responsive component.
- Desktop: keep it in rail.
- Mobile: bottom tab bar or compact top segmented nav.
- Add mobile Playwright test that visits `/ui/`, switches each domain, and screenshots.

### P0: Mission Control Lacks Real Browser Tests

`webui/package.json` only defines `dev`, `build`, and `preview`. Current Python tests cover route serving and some API shapes, but not the browser runtime.

Fix:

- Add Playwright.
- Test boot, pairing, websocket connect/reconnect, message streaming, tool cards, plan strip, artifact preview, mobile nav.
- Capture screenshots at 390x844, 768x1024, 1440x900.

### P0: React API Inventory Is Not Canonical

`tests/integration/test_v2_ui_endpoint_smoke.py` says its endpoint inventory was extracted from `xmclaw/daemon/static/pages/*.js`. Primary UI calls now come from `webui/src`. Examples include `/api/v2/memory/v2/overview`, `/api/v2/memory/v2/graph`, `/api/v2/system/health`, `/api/v2/session_workspaces/*`, `/api/v2/tasks?limit=100`.

Fix:

- Add a Mission Control endpoint inventory test generated or curated from `webui/src`.
- Keep legacy inventory only for `/ui-legacy/`.

### P0: Artifact Preview Boundary Needs Hardening

Risk points:

- `MermaidView` injects rendered SVG with `dangerouslySetInnerHTML`.
- `WorkspacePanel` renders HTML artifacts via iframe `srcDoc` with `sandbox="allow-scripts"`.
- SVG artifacts are wrapped into `srcDoc` and also allow scripts.
- Markdown allows external links and image URLs, with media token query rewriting.

Fix:

- Sanitize SVG/HTML with DOMPurify or equivalent before insertion.
- Use `sandbox=""` by default; enable scripts only for explicitly trusted interactive artifacts.
- Add iframe CSP in `srcDoc` for artifacts.
- Add tests for script tags, event handlers, `javascript:` links, external image URLs, and Mermaid unsafe labels.

### P1: Bundle Size Needs Ownership

Build warnings show chunks above 500 kB. The likely culprits are Mermaid core/subdiagrams, Cytoscape, KaTeX, and Markdown rendering. Lazy loading exists but chunking is still coarse.

Fix:

- Add `manualChunks` for Mermaid, Cytoscape, KaTeX/highlight, markdown.
- Move graph-only libraries under graph views.
- Add a build budget test that fails if initial JS/CSS exceeds a threshold.

### P1: Async View Fetches Need Cancellation Discipline

Several views call `apiGet(...).then(setState)` directly. If users switch sessions/views quickly, stale responses can overwrite current state.

Fix:

- Add `useSafeQuery` / `useAbortableApi`.
- Use request ids for session-scoped state.
- Test rapid switching between sessions and views.

### P1: Accessibility Is Below the App's Complexity Level

There are useful `aria-label`s in places, but patterns are inconsistent. Many toggles are buttons without `aria-pressed`; hover-only delete buttons are hard to discover by keyboard; `title` is used as a tooltip substitute; emoji/icon controls need stable accessible names.

Fix:

- Add shared `IconButton`, `ToggleButton`, `SegmentedControl`, `Tooltip`.
- Add keyboard tests for task selection, slash menu, modal/lightbox close, tab navigation, plan/fanout approval.

## Suggested 4-Week Plan

### Week 1: Frontend Safety and Test Bed

- P0 mobile domain nav.
- Playwright smoke harness with desktop/mobile screenshots.
- Mission Control endpoint inventory from `webui/src`.
- Artifact sanitizer and sandbox policy.

### Week 2: Runtime Control Loops

- Explicit ReAct loop event model and exit reasons.
- Tool history processor.
- Error-feedback templates for shell/browser/schema retries.

### Week 3: Memory and Reflection

- SummarizerAgent partial eviction.
- Failed-trajectory self-critique prompt.
- Long-term lesson write with provenance.

### Week 4: Graph-State Multi-Agent

- Minimal GraphState + reducers.
- Node policy: timeout/retry/cache/error handler.
- Team view renders graph state rather than flat worker cards.

## Checklist Additions

Add these to the existing 110+ checklist:

- F01: Mobile domain navigation works without desktop rail.
- F02: Playwright boot smoke covers `/ui/` on desktop and mobile.
- F03: React endpoint inventory is generated/maintained from `webui/src`.
- F04: Artifact previews are sanitized and sandboxed by trust level.
- F05: Mermaid unsafe content regression test.
- F06: Markdown external link/image URL policy test.
- F07: Initial bundle and lazy chunk size budgets.
- F08: Abort/cancel stale view fetches.
- F09: WS replay + REST hydration interleaving test.
- F10: Keyboard-only task/chat/approval flow.
- F11: Toggle buttons expose `aria-pressed`.
- F12: ErrorBoundary reports route/view and correlation id.
- F13: Build output line ending policy for committed `webui_dist`.
- F14: Visual regression screenshots for key views.
- F15: Token-in-query redaction and migration plan.
