# Changelog

All notable changes to XMclaw are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **1.0 stability promise.** From `1.0.0` onward the public surface is **contract-frozen**. Breaking changes go to a `2.x` major; new features go to `1.x` minors. The `2.0.0.dev0` line below is the prior development-preview snapshot, kept for archaeology — the rewrite landed as `1.0.0` after the codebase was reframed around the local-first self-evolving runtime thesis.

## [Unreleased]

## [1.0.1] - 2026-07-02

### Fixed

- Windows one-shot installer now avoids non-ASCII user-profile paths by using ASCII-only venv/temp fallbacks when needed.
- `xmclaw[all]` now resolves on Python 3.14 by selecting the latest RapidOCR ONNX runtime version available for that interpreter.

### Removed

- Removed repository test, diagnosis, probe, and smoke-test files from the distributable source tree.
- Removed pytest configuration and dev dependency entries after test cleanup.

### Added — MCP HTTP transport (SSE / streamableHttp)

- `xmclaw/providers/tool/mcp_http_bridge.py` — new `MCPHttpBridge` class implementing JSON-RPC 2.0 over HTTP. Supports both `sse` (Server-Sent Events) and `streamableHttp` transports via `httpx`.
- `MCPHub` now accepts `url`-based configs (was rejected with "non-stdio transports not yet supported"). Per-server `transport` field drives bridge selection: `stdio` → `MCPBridge`, `sse`/`streamableHttp` → `MCPHttpBridge`.

### Added — Plugin SDK dynamic loading

- `xmclaw/plugins/loader.py` — entry-point discovery via `importlib.metadata.entry_points(group="xmclaw.plugins")`. Resolves subclasses of `ToolProvider`, `Skill`, or `ChannelAdapter` (or factory callables returning them). Broken plugins are logged and skipped; they never crash daemon boot.
- `xmclaw/providers/channel/registry.py` — `discover()` now merges external channel plugins from entry points alongside built-in channels.

### Added — Email inbound attachment support

- `xmclaw/providers/channel/email/adapter.py` — `_extract_attachments()` + `_save_email_attachments()`. Attachments up to 25 MB (configurable) are extracted, sanitized, and persisted to `~/.xmclaw/v2/uploads/email/`.
  - Image attachments are surfaced via `InboundMessage.raw["images"]` so `AgentLoop.run_turn(user_images=...)` vision path works.
  - Non-image attachments are listed in the inbound text with filename, MIME type, size, and saved path.

### Added — ContextEngine shadow wiring

- `AgentLoop` accepts an optional `context_engine` parameter. When wired, `run_turn` calls `bootstrap()`/`assemble()` for history load and `_persist_history` syncs cleaned history to `engine.after_turn()` via `_sync_engine_history()`. This is a **shadow / observational** integration — `self._histories` remains the primary store for backward compatibility; future refactor will flip the primary/secondary relationship.

### Added — Epic #25 · 贾维斯化 (R1–R6 主动认知 + 多模态感知 + 自主性)

**Framework-level capability jump.** XMclaw was previously a reactive turn-by-turn REPL — user sends message, agent responds. Epic #25 added a continuous cognition layer that keeps thinking, sensing, and proposing while the user is silent. Lifespan now wires up six R-track subsystems by default; Mind page (`/mind`) makes them visible.

- **R1 ReflectionCycle** — `xmclaw/cognition/reflection_cycle.py` runs a 3-bucket schedule (5min hot / 1h warm / 1day cold) + a 60s metacognize tick. Emits `reflection_cycle_ran` events with bucket + summary.
- **R2 HTNPlanner DAG** — `xmclaw/cognition/planner.py` extended to return task DAGs; `POST /api/v2/cognition/goals/plan` is the user-facing endpoint, wired to the Mind page Goals panel.
- **R3 MetaCognitionPass** — `xmclaw/core/metacognition/pass_.py` + `reformer.py` + `xmclaw/core/decisions/recorder.py`. Periodically scans recent `DecisionTrace` rows and asks the LLM "what patterns do you see?". Anti-overclaim three-piece kit enforced: `confidence_cap = 0.6` (Iron Rule #2 mirror), `min_evidence ≥ 3`, all-`outcome=ok` evidence rejected. Hits `metacognition_proposal` events the Reformer routes into `curriculum_edit` / `skill` / `preference` proposals. Default ON at 60s interval.
- **R4 Multi-modal perception** — `xmclaw/cognition/perception/{screen,window,clipboard,calendar}_watcher.py`. Each watcher pushes percepts into the `PerceptionBus`; unsupported platforms / missing optional deps degrade per-watcher without killing siblings. Default ON.
- **R5 AutonomyPolicy + SuggestionInbox** — `xmclaw/cognition/autonomy.py` (continuous 0–100 level with tier semantics) + `xmclaw/cognition/suggestion_inbox.py` (sqlite-backed pending queue). `GET /api/v2/cognition/suggestions[?status=pending|all]` exposed; default `autonomy_level = 50` (Suggest tier — agent proposes but doesn't execute).
- **R6 Mind page UI** — `xmclaw/daemon/static/pages/Mind.js` aggregates four panels (InnerMonologue / ReflectionTimeline / Goals / Suggestions). Each panel subscribes to `/api/v2/events` with the appropriate type filters.

### Added — Patch A · Path unification

11 hardcoded `Path.home() / ".xmclaw"` callsites across the codebase (cognitive_state, graph_db, experiments_db, evolution proposals, eval cache, decisions_db, suggestions_db) all routed through new helpers in `xmclaw/utils/paths.py`. Each helper honours `XMC_DATA_DIR` and accepts an optional narrow override (e.g. `XMC_V2_GRAPH_DB_PATH`). New CI lint test (`tests/unit/test_v2_paths_unified.py`) greps non-`paths.py` files for the literal `Path.home() / ".xmclaw"` pattern; future drifts fail the build.

### Added — `/api/v2/evolution/proposals` aggregator

Replaces 3 separate `/api/v2/events?types=...` round-trips that `Evolution.js` was making. Single endpoint returns 4 buckets (`proposals` / `verdicts` / `promotions` / `rollbacks`) from one `events.db` read. Tested in `tests/integration/test_v2_ui_endpoint_smoke.py` UI inventory.

### Changed — Default conservative posture removed (2026-05-09)

User explicitly revoked the "保守隐私姿态" — Jarvisification subsystems now default-on out of the box. `daemon/config.example.json` synced to match the code:

- `cognition.continuous_loop.autonomy_level`: `0` → `50` (Suggest tier)
- `cognition.metacognize`: hidden → default-on at 60s
- `cognition.perception.{screen,window,clipboard,calendar}.enabled`: `false` → `true`
- `cognition.continuous_loop.enabled`: stub → really running

### Changed — Code splitting (-1,800+ LOC across 5 monoliths)

Five large files split into smaller, focused modules. Pure refactors — zero behaviour change, validated by 1791 unit + 15 b298 + 68 agent_loop tests staying green.

- `xmclaw/providers/channel/_shared.py` (NEW, 32 LOC) — extracted shared base from 5 channel adapters (discord/slack/telegram/lark/email); net **-130 LOC**.
- `xmclaw/providers/llm/streaming_utils.py` (NEW, +66 LOC) — Anthropic + OpenAI streaming common path (cumulative usage / max_tokens truncation handling / chunk merge).
- `xmclaw/providers/tool/builtin.py` — **3,241 → 592 LOC (-82%)**. Monster split into 8 mixins: `DbMixin` / `FsMixin` / `MemoryMixin` / `PersonaMixin` / `ShellMixin` / `UserMixin` / `VoiceMixin` / `WorktreeMixin`. `BuiltinTools` MRO: `8 mixins → ToolProvider`.
- `xmclaw/daemon/app.py` — **3,546 → 1,912 LOC (-46%)**. Lifespan body extracted to new `xmclaw/daemon/app_lifespan.py` (1,786 LOC). 24 sites of `except Exception:` (no `as exc`) that referenced `exc` in the body fixed to `except Exception as exc:` to remove latent `UnboundLocalError`s.
- `xmclaw/daemon/agent_loop.py` — `_run_turn_inner` split into a turn-level setup/teardown wrapper plus a new `_run_hop_loop` for the LLM↔tool inner loop. State boundaries are now clean; per-hop state (`_stuck_loop_deque`) no longer leaks into turn scope.

### Fixed — `core/metacognition/pass_.py` import-direction violation

Module was importing `xmclaw.providers.llm.base.Message` (violates `core cannot import from providers` per `scripts/check_import_direction.py`). Replaced with a tiny local `_Msg` dataclass — the LLM consumer is duck-typed against `role`+`content` attributes anyway.

### Fixed — Silent `except: pass` cleanup (5 sites)

`xmclaw/cognition/file_watcher.py` (3 sites: bus publish, salience push, callback failure), `xmclaw/cognition/graph_extractor.py` (1 site: duplicate edge), and `xmclaw/utils/security.py` (2 sites: audit-log writes) all upgraded from silent swallow to `log.warning(..., exc_info=True)`. Audit-write failures in particular were a compliance gap — quietly losing audit lines is worse than the operation itself failing.

### Fixed — Doctor `EvolutionPipelineCheck` reads both app.py and app_lifespan.py

After the lifespan extraction, most wiring tokens (`HonestGrader` / `EvolutionAgent` / `JournalWriter` / `SkillDreamCycle` / `ProposalMaterializer` / `RealtimeEvolutionTrigger` etc.) live in `app_lifespan.py`. The doctor check now concatenates both files' source before scanning `REQUIRED_TOKENS`, eliminating a false "evolution chain not wired" report.

### Tests

- 23/23 `tests/unit/test_v2_lint_roadmap.py` — Epic #25 entry passes the roadmap lint guard (including `test_shipped_roadmap_passes`).
- 15/15 `tests/{unit/test_v2_b298_lifespan_wiring,integration/test_v2_b298_evolution_chain_e2e}.py` — B-298 lifespan extraction + import-path fixups verified.
- 68/68 `tests/unit -k "agent_loop or run_turn or hop"` — agent_loop hop-loop split is regression-clean.
- 6 new unit tests in `tests/unit/test_v2_paths_unified.py` — Patch A path guards.
- `tests/integration/test_v2_ui_endpoint_smoke.py` UI inventory extended with R2 / R5 / R6 endpoints + `/api/v2/evolution/proposals`. Every URL the static `pages/*.js` calls now smoke-checks against the real `create_app` (front-back boundary rule, 2026-05-09).

### Docs

- **`docs/architecture/XMclaw_Architecture_Assessment_2026-05-09.md`** (NEW, 670 lines) — full-codebase architecture review (~607K LOC, 4-subsystem deep dive, JARVIS-vision delta scored at ~60%, 4-phase implementation roadmap A→D). Foundation for Epic #25+ priority calls.
- **`docs/PROJECT_DEFINITION_2026-05-10.md`** (NEW) — code-derived project positioning. One sentence: *本地常驻、跨会话有持续记忆 + 持续认知 + 自主目标分解 + 多模态感知 + 自我进化的"个人贾维斯" runtime*. Author is sole user; not SaaS; not for others.
- **`docs/UI_FUNCTION_AUDIT_2026-05-10.md`** (NEW) — 22-page + 10-panel audit. Result: **20 ✅ / 8 🟡 / 0 🔴 / 0 P0 / 0 payload drift**. Tool lesson logged: future audits must grep BOTH `routers/*.py` AND `app.py` (the first audit pass missed inline `@app.get/post` endpoints and produced 3 false-alarm P0s).
- **`docs/DEV_ROADMAP.md`** — Epic #25 章节 added, lint-clean.
- `pages/ModelProfiles.js` annotated with a deprecation header (0 imports anywhere; Settings.js does its own LLM-profile management directly via `/api/v2/llm/profiles`). Pending `rm` once the sandbox lifts pre-existing-tracked-file removal.

### Removed

- `docs/codebase/.codebase-scan.txt` (the 80KB one-shot scratch dump from the audit run) is now `.gitignore`d. The architecture assessment in `docs/architecture/` is the durable artifact.

## [1.0.0] — 2026-04-25

**1.0 GA.** Promoted from `1.0.0rc1` on the same day. The core local-first self-evolving runtime is feature-complete and contract-frozen. Per [docs/DEV_ROADMAP.md § M9](docs/DEV_ROADMAP.md), 1.0 GA scope = *the runtime is stable*, not *every Epic ships*. Items explicitly post-1.0 (Channel SDK, IDE/ACP, Skill Hub, Web UI Phase 2 rich panels, Epic #4 Phase D `gene_forge` UI + killer-demo GIF, plugin SDK pilot, AgentLoop → SkillRuntime.fork migration) move to the `v2.x` roadmap.

### Changed (since `1.0.0rc1`)

- **`pyproject.toml` version** `1.0.0rc1` → `1.0.0`; `xmclaw/__init__.py::__version__` and `xmclaw/providers/tool/mcp_bridge.py::_CLIENT_VERSION` follow.
- **README "Status"** rewritten from *release candidate* tone to *1.0 GA stable* tone; the RC → GA promotion gate (formerly listed as outstanding) is removed.
- **DEV_ROADMAP § M9** RC → GA gate items collapsed into the GA-shipped record.

### Removed

- Stray `jest.config.js` / `package.json` from the repo root (pre-history dev scratch — the web UI under `xmclaw/daemon/static/` is still no-build-step ESM).

## [1.0.0rc1] — 2026-04-25

**Release candidate for `1.0`.** Core local-first self-evolving runtime is feature-complete and contract-frozen. Promotion to `1.0.0` gated on a 1–2 week dogfood window: 7 days no P0, 72h continuous-uptime soak, and Epic #4 real-data exit criteria (recorded killer-demo GIF, ≥ 0.1 grader-score lift over a week, ≥ 3 real evolution events visible to `xmclaw evolution show --since 7d`).

### 1.0 GA scope decision (2026-04-25)

XMclaw 1.0 = **the local-first self-evolving runtime is stable and contract-frozen**, not "every feature ever imagined ships." See [docs/DEV_ROADMAP.md § M9](docs/DEV_ROADMAP.md) for the authoritative scope record.

**Explicitly post-1.0** (now on the v2.x roadmap, not blocking GA):
- Epic #1 Channel SDK · Epic #7 IDE / ACP · Epic #8 Skill Hub · Epic #18 Web UI Phase 2 rich panels
- Epic #4 Phase D `gene_forge` rich UI + killer-demo GIF (the engine ships in 1.0)
- Epic #2 plugin SDK pilot · Epic #3 AgentLoop → `SkillRuntime.fork` migration

### Added (since `2.0.0.dev0`)

- **Repository governance** — `SECURITY.md` (private vulnerability disclosure, 5d / 14d / 90d SLA), `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `.github/ISSUE_TEMPLATE/{1-bug,2-feature,3-question}.yml`, `.github/PULL_REQUEST_TEMPLATE.md` (Anti-Req checklist + Epic citation reminder).
- **DEV_ROADMAP `§ M9` 1.0 GA scope record** — explicit list of what's in / out, plus the RC1 → GA promotion gate.

### Changed

- **`pyproject.toml` version** `2.0.0.dev0` → `1.0.0rc1`.
- **README "Status"** rewritten from *development preview* tone to *release candidate* tone, with the RC → GA gate enumerated.
- **DEV_ROADMAP M1 / M8** — M8 closeout (5/5 ✅), M1 5/6 with 72h soak deferred to GA gate.
- **DEV_ROADMAP M2 / M3 / M4 / M5 / M6 / M7** — annotated with explicit *post-1.0 / partial / deferred* status per the scope decision; nothing here blocks GA.

### Fixed

- **`xmclaw doctor` ↔ `factory.py` `tools.allowed_dirs` contract divergence** — doctor was raising `[!] tools` on stock configs because it required a non-empty `allowed_dirs`, but `xmclaw/daemon/factory.py:381` treats missing or `[]` as the default-open posture (full filesystem access). Doctor now mirrors the factory contract: missing or `[]` → `ok` with an advisory pointing at `tools.allowed_dirs: ["~/path", ...]` if the user wants to sandbox; non-list → error. Three corresponding unit tests (`test_v2_doctor.py::test_tools_*`) flipped to match.

### Tests

- 1387 unit + 1589 total tests pass locally on Windows 11 + Python 3.10.
- Doctor 15/15 ok on stock `daemon/config.json`.
- Real-model dialogue smoke (MiniMax-M2.7-highspeed): 6.9s simple prompt + 38.5s tool-using complex prompt, both green; transcripts under `tests/manual/_artifacts/`.

## [2.0.0.dev0] — 2026-04-25

First public preview of the **v2 self-evolving runtime**. The codebase is the FastAPI daemon + AgentLoop + Honest Grader + SkillScheduler + EvolutionController stack described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The v1 batch-grader prototype is archived at the `v1-final` tag.

### Highlights

- **Streaming behavioural-event bus** with SQLite WAL + FTS5 persistence and full session replay over WebSocket.
- **Honest Grader → Online Scheduler → EvolutionController** evidence pipeline; LLM self-grade weight capped at ≤ 0.2 (anti-req #1).
- **15-check `xmclaw doctor`** with 5 auto-fixable checks and a third-party plugin entry-point group (`xmclaw.doctor`).
- **Local-first workspace** under `~/.xmclaw/v2/` (events.db, memory.db, pairing token, daemon pid). `XMC_DATA_DIR` relocates the whole tree.
- **Pairing-token auth** on every WS / HTTP entrypoint; bad token → `close(4401)` with constant-time compare.
- **Smart-gate CI** (`scripts/test_changed.py`) — PRs only run lanes affected by the diff; `push` to `main` runs the full suite.

### Added — Epics merged

- **Epic #5** — Memory eviction (LRU + pinned tags + size cap, sqlite-vec backend).
- **Epic #6** — `XMC__` env-var override layer (deep-merge with config.json, secrets path-aware).
- **Epic #9** — Onboarding wizard (`xmclaw onboard`, 6-step interactive setup with provider smoke test).
- **Epic #10** — Doctor (15 built-in checks, 5 auto-fix, plugin entry-point, `--json` / `--network` / `--discover-plugins` flags).
- **Epic #11** — Smart-gate CI (`scripts/test_lanes.yaml` lane map, path → lane resolver, full suite on push to main).
- **Epic #12** — Per-subdir `AGENTS.md` layering with import-direction guard (`scripts/check_import_direction.py`).
- **Epic #13** — SQLite WAL + FTS5 event bus (replay, type filter, keyword search, sub-100ms search at representative scale).
- **Epic #14** — Prompt-injection scanner with `detect_only` / `redact` / `block` policy knob; `PROMPT_INJECTION_DETECTED` events on every detection.
- **Epic #15** — Structured logging (structlog, secret scrubbing, session contextvars).
- **Epic #16** — Secrets layer (Phase 1: env > secrets.json 0600 > keyring; Phase 2: Fernet-at-rest with sibling-dir migration CLI).
- **Epic #20** — Backup & restore (Phase 1: `xmclaw backup create/list/info/verify/delete/prune/restore` with sha256 manifest gate and atomic swap; Phase 2: auto-daily scheduler).

### Added — Epics in progress

- **Epic #2** — Plugin SDK (boundary frozen, import-direction guard live, pilot example pending).
- **Epic #3** — Skill sandbox (subprocess runtime + factory + tool guardians + ApprovalService + SkillScanner + CLI/REST shipped; AgentLoop wiring next).
- **Epic #4** — Evolution UX (Phase A `xmclaw evolution show` + Phase B `SKILL_EVOLVED` REPL flash + Phase C orchestrator wired into `serve` lifespan; killer-demo GIF pending).
- **Epic #19** — Cloud / systemd templates (Dockerfile + multi-arch GHCR publish workflow shipped; systemd unit + Helm chart pending).
- **Epic #23** — Web UI Phase 1 (Preact + htm shell, chat workspace, WS client, streaming markdown).

### Added — Infrastructure

- `SECURITY.md` — vulnerability disclosure policy + hardening tips (private-advisory channel, 5-day ack / 14-day triage / 90-day patch SLA for high-severity).
- `CODE_OF_CONDUCT.md` — community baseline, distilled from Contributor Covenant 2.1.
- `CONTRIBUTING.md` — dev workflow + Anti-Req checklist + DEV_ROADMAP §3.6 protocol.
- `.github/ISSUE_TEMPLATE/{1-bug_report,2-feature_request,3-question}.yml` — structured forms with components / scope dropdowns and automatic Epic linkage.
- `.github/PULL_REQUEST_TEMPLATE.md` — Anti-Req checklist + import-direction probe + Epic-citation reminder.
- `Dockerfile` + `docker-compose.yml` + `.github/workflows/docker-publish.yml` (Epic #19) — multi-arch image at `ghcr.io/1593959/xmclaw`.

### Added — Documentation

- `docs/ARCHITECTURE.md` — definitive system design and dependency DAG.
- `docs/DEV_ROADMAP.md` — 20 Epics + 9 milestones + execution protocol §3.6.
- `docs/EVENTS.md` — typed `BehavioralEvent` schema reference.
- `docs/TOOLS.md` — built-in tool reference (`file_*`, `bash`, `git`, `web`, `browser`, `mcp_*`).
- `docs/DOCTOR.md` — every check, its remediation, and how to write a plugin.
- `docs/CONFIG.md` — full `daemon/config.json` field reference + `XMC__` override layer.
- `docs/WORKSPACE.md` — `~/.xmclaw/v2/` layout + `XMC_DATA_DIR` relocation.
- `docs/V2_DEVELOPMENT.md`, `docs/V2_STATUS.md` — Anti-Req scorecard + bench numbers.
- `docs/BACKUP.md` — user-facing backup & restore guide.

### Fixed

- **Doctor ↔ factory `tools.allowed_dirs` contract divergence** — doctor now mirrors `xmclaw/daemon/factory.py:381`: missing or empty `allowed_dirs` is a *default-open* posture (advisory), not a critical error. Adds an advisory pointing users at how to sandbox if they want to.
- **Web UI `chat.css` regression** after the Epic #23 Phase 1 squash-merge — restored the link in `xmclaw/daemon/static/index.html`.
- **`app.js` / `store.js` / `layout.css` regression** — same root cause as above; restored from the pre-merge tree (commit `898b8a8`).
- **Ruff cleanup** — 31 → 0 errors across `xmclaw/` (unused imports / vars / multi-statement E701).

### Tests & benches

- 1387 unit + 1589 total tests (smart-gate selects per PR; full suite gates `push` to main).
- Live benches on MiniMax (gates listed):
  - [`phase1_live_learning_curve`](tests/bench/phase1_live_learning_curve.py) → 1.12× over uniform baseline (gate ≥ 1.05×).
  - [`phase2_tool_aware_live`](tests/bench/phase2_tool_aware_live.py) → 100% real tool-firing per scored turn (gate ≥ 80%).
  - [`phase3_autonomous_evolution_live`](tests/bench/phase3_autonomous_evolution_live.py) → 1.18× session-over-session after auto-promote (gate ≥ 1.05×).

### Known gaps (by design — see `⬜` in DEV_ROADMAP.md)

- **Epic #4 user-facing surface** — engine ships, killer-demo / `gene_forge` UI / GIF do not.
- **Epic #1** Channel SDK / **#7** IDE+ACP / **#8** Skill Hub / **#17** multi-agent / **#18** rich Web UI Phase 2 / **#19** systemd & Helm — planned, not built.
- **All 9 milestones (M1–M9)** remain formally open even though several (M1 Daemon GA, M8 Observability) are at or near their exit criteria — closeout passes pending.

## [v1-final] — archived

The v1 batch-grader prototype is preserved at the `v1-final` tag for archaeology. v2 is a ground-up rewrite, not a refactor — there is no migration path from v1 state.

[Unreleased]: https://github.com/1593959/XMclaw/compare/HEAD...HEAD
[2.0.0.dev0]: https://github.com/1593959/XMclaw/tree/main
[v1-final]: https://github.com/1593959/XMclaw/releases/tag/v1-final
