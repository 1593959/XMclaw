# Changelog

All notable changes to XMclaw are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **RC stability promise.** From `1.0.0rc1` onward the public surface is **contract-frozen for `1.0`**. Breaking changes during the RC → GA window are bug fixes only; new features go to `1.1`. The `2.0.0.dev0` line below is the prior development-preview snapshot, kept for archaeology.

## [Unreleased]

No changes since `1.0.0rc1`.

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
