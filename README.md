# 🦞 XMclaw — Local-First, Self-Evolving AI Agent Runtime

<p align="center">
  <strong>A personal AI agent that runs on your machine. Thinks, acts, remembers, and improves itself on hard evidence — not on its own self-assessment.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <img src="https://img.shields.io/badge/status-1.0.0%20(stable)-brightgreen?style=for-the-badge" alt="Status">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform">
</p>

> ### 🟢 Status: 1.0.0 — Stable (2026-04-25, last hardening pass 2026-04-29)
>
> **1.0 GA shipped.** The core local-first self-evolving runtime is feature-complete, tested (1565 unit + 1700+ total tests), and contract-frozen — breaking changes go to a `2.x` major; new features go to `1.x` minors. The post-GA hardening pass (B-71 → B-83) added atomic-write durability, full HTTP-auth coverage, OOM-defending request size cap, persona-file prompt-injection scanning, Chinese-language injection patterns, a first-run setup banner, and inline LLM / embedding configuration forms — all without breaking the 1.0 contract. See [CHANGELOG.md](CHANGELOG.md) for what shipped, [SECURITY.md](SECURITY.md) for the disclosure policy.
>
> **What 1.0 actually is:**
>
> - The **self-evolution spine** — streaming bus + Honest Grader + Online Scheduler + EvolutionController + SkillRegistry — is real code, used end-to-end by live benches on MiniMax:
>
>   | Live bench | Result | Gate |
>   |---|---|---|
>   | [`phase1_live_learning_curve`](tests/bench/phase1_live_learning_curve.py) | **1.12× over uniform baseline** | ≥ 1.05× |
>   | [`phase2_tool_aware_live`](tests/bench/phase2_tool_aware_live.py) | **100% real tool-firing** per scored turn | ≥ 80× |
>   | [`phase3_autonomous_evolution_live`](tests/bench/phase3_autonomous_evolution_live.py) | **1.18× session-over-session** after auto-promote | ≥ 1.05× |
>
> - A **FastAPI + WebSocket daemon** with pairing-token auth, event replay, and SQLite WAL + FTS5 event persistence.
> - A **CLI** covering daemon lifecycle / interactive chat / config / secrets / backup / memory / doctor (21 checks, 5 auto-fixable — the most recent additions: `MemoryIndexerCheck`, `MemoryProviderConfigCheck`, `PersonaProfileCheck`, `DreamCronCheck`, `ConfigDeadFieldsCheck`).
> - A **web UI** (vanilla ESM under `xmclaw/daemon/static/`, no Node build) at `http://127.0.0.1:8765/ui/` — chat workspace + WS streaming markdown + first-run **setup banner** that walks new users through configuring their LLM / persona / embedding inline.
>
> **What 1.0 is *not*** (now on the `v2.x` roadmap, deliberately deferred):
>
> - Epic #1 Channel SDK (Discord / Slack / Telegram adapters)
> - Epic #7 IDE + ACP entrypoints (Zed / VS Code)
> - Epic #8 Skill Hub (`xmclaw skills install <name>`)
> - Epic #18 Web UI Phase 2 — rich evolution / memory / tool panels
> - Epic #4 Phase D — `gene_forge` rich UI + a recorded killer-demo GIF (the *engine* ships in 1.0)
> - Epic #2 plugin SDK pilot (boundary + CI guard ship in 1.0; first real third-party plugin lands in 2.x)
> - Epic #3 AgentLoop → `SkillRuntime.fork` migration (Guardians + ApprovalService + Scanner ship in 1.0)
>
> **Why this scope.** XMclaw 1.0 = "the local-first self-evolving runtime is stable and contract-frozen," not "every feature ever imagined ships." The 7 deferred items above are growth / surface-layer enhancements, not the core thesis. See [docs/DEV_ROADMAP.md § M9](docs/DEV_ROADMAP.md) for the full scope decision.
>
> Design docs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/V2_DEVELOPMENT.md](docs/V2_DEVELOPMENT.md) · [docs/V2_STATUS.md](docs/V2_STATUS.md).

**XMclaw** is a personal AI agent that runs entirely on your machine. It is not a chatbot — it is a runtime that **thinks, acts, remembers, and measures its own skill versions against hard evidence** to decide what to promote.

Unlike a stateless chat interface, XMclaw keeps durable memory across sessions, executes real tools on your filesystem, and runs an evidence-based evolution loop (Honest Grader → Online Scheduler → Skill Registry) that promotes new skill versions when *measured* outcomes beat the incumbent — **not when the model claims they do.**

[Docs](./docs) · [Architecture](./docs/ARCHITECTURE.md) · [Tools](./docs/TOOLS.md) · [Events](./docs/EVENTS.md) · [Doctor](./docs/DOCTOR.md) · [Config](./docs/CONFIG.md) · [Roadmap](./docs/DEV_ROADMAP.md)

---

## ✨ What makes XMclaw different

| | |
|---|---|
| **🧠 Evolution-as-Runtime** | Every LLM call, tool invocation, and skill execution becomes a `BehavioralEvent`. The **Honest Grader** scores outcomes on hard evidence (did the tool actually run? was a real side-effect produced?), the **Online Scheduler** treats skills as bandit arms, and the **EvolutionController** promotes or rolls back versions. LLM self-assessment gets at most 0.2 weight in the decision loop — the hard signals dominate. |
| **💾 Local-First State** | Events, memory, and pairing token all live in `~/.xmclaw/v2/` (SQLite + sqlite-vec). `XMC_DATA_DIR` moves the whole workspace in one lever. Nothing leaves your disk unless you explicitly opt in. |
| **🔁 Event Replay** | Every WS reconnect replays the session's events so the UI hydrates without round-tripping the LLM. `/api/v2/events` supports `session_id` / `since` / `types` filters + FTS5 keyword search across payloads. |
| **🛡️ Anti-Req Driven** | 14 explicit anti-requirements (e.g. *"the scheduler must not trust text that describes a tool call"*, *"no LLM self-grading"*, *"WS auth via pairing token with `close(4401)`"*). Each is encoded in the code path with a dedicated test; violations emit `ANTI_REQ_VIOLATION` events. Scorecard in [docs/V2_STATUS.md](docs/V2_STATUS.md). |
| **🔌 MCP + Provider Model** | Tools are composed from `ToolProvider` backends: `builtin`, `browser` (Playwright), `lsp`, `mcp_bridge` (stdio / SSE / WS). Add your own by implementing `list_tools()` + `invoke()`. |
| **🩺 Doctor with Plugins** | `xmclaw doctor` runs **21 built-in checks** (config / config-dead-fields / LLM / tools / workspace / pairing / port / events-db / memory-db / memory-providers / memory-provider-config / memory-indexer / persona-profile / dream-cron / skill-runtime / connectivity / roadmap-lint / stale-pid / daemon-health / backups / secrets) plus any third-party check on the `xmclaw.doctor` entry-point group. `--fix` auto-remediates 5 of them. The `config-dead-fields` check (B-78) flags config keys no production code path actually reads — catches stale templates copied from out-of-date docs. |
| **🔐 Secrets Layer (Phase 1)** | Three-tier resolution: env `XMC_SECRET_<NAME>` > `~/.xmclaw/secrets.json` (chmod 0600) > optional `keyring`. Leave `api_key: ""` in config and set the value via `xmclaw config set-secret llm.anthropic.api_key`; the daemon resolves it at startup without touching your JSON. Phase 2 (Fernet-at-rest) is on the roadmap. |
| **💾 Backup & Restore** | `xmclaw backup create/list/info/verify/delete/prune/restore` — tar.gz + manifest with sha256 integrity gate, atomic swap on restore, tar-slip defense. `--json` output on every subcommand for pipelines. |
| **🛰️ Structured Events** | Typed `BehavioralEvent` stream over WebSocket at `/agent/v2/{session_id}`. No custom XML parsing — tool calls are decoded by per-provider translators into a structured `ToolCall` IR. |
| **🧪 Smart-Gate CI** | `scripts/test_changed.py` maps edited paths to test lanes via `scripts/test_lanes.yaml` — PRs only run the tests they can actually break; `push` to `main` always runs the full suite as ground truth. |

---

## Install

```bash
# Clone
git clone https://github.com/1593959/XMclaw.git
cd XMclaw

# Install (runtime)
pip install -e .

# With dev extras (pytest, ruff, mypy, pip-tools)
pip install -e ".[dev]"

# Reproducible installs (exact pinned versions)
pip install -r requirements-lock.txt             # prod
pip install -r requirements-dev-lock.txt         # prod + dev

# Optional extras
pip install pyautogui mss                        # computer-use tools
pip install playwright && playwright install chromium   # browser tools
```

Python ≥ 3.10. The CI matrix runs Ubuntu / macOS / Windows on Python 3.10.

Configure API keys — three equivalent ways:

```bash
xmclaw config init                                  # interactive bootstrap
# or edit daemon/config.json (copy from daemon/config.example.json)
# or set env: XMC__llm__anthropic__api_key="sk-ant-..."
# or keep config clean and use secrets:
xmclaw config set-secret llm.anthropic.api_key     # reads via stdin (getpass)
```

Verify your setup:

```bash
xmclaw doctor           # 15 checks
xmclaw doctor --fix     # auto-remediate 5 fixable ones
```

---

## Quick Start

```bash
# Start daemon (serves API + basic web UI)
xmclaw start
# → API at  http://127.0.0.1:8765
# → UI at   http://127.0.0.1:8765/ui/
# → WS at   ws://127.0.0.1:8765/agent/v2/{session_id}

# Interactive CLI
xmclaw chat
xmclaw chat --plan                                   # plan mode: approve steps before they run

# Smoke test (bus round-trip)
xmclaw ping

# Stop daemon
xmclaw stop
```

---

## 🚀 First-Run Onboarding

Open `http://127.0.0.1:8765/ui/` after `xmclaw start`. If anything is missing for normal operation, the **Setup Banner** at the top of every page (B-81) tells you exactly what to do — no docs hunt, no config-field hunt:

| Missing | What it means | Fix it from the UI |
|---|---|---|
| **LLM API key** | Agent runs in echo mode (just mirrors your messages back) | Click "立即配置" on the banner — opens an inline form (provider · key · base_url · default_model) right there. Submit, restart daemon. (B-83) |
| **Persona files** | No SOUL.md / IDENTITY.md — agent has no identity | Click "复制命令" to grab `xmclaw onboard`, paste in terminal, follow the wizard. |
| **Vector embedding** | `memory_search` falls back to keyword scan, no semantic recall | Memory page → Providers tab → "配置 embedding" inline form. Defaults pre-filled for local Ollama (`qwen3-embedding:0.6b @ 1024`). (B-76) |

The banner auto-disappears once everything checks out and re-surfaces if state regresses (e.g. you wipe `config.json`). Per-item dismiss is per-browser (localStorage). Backed by `GET /api/v2/setup`.

If you prefer the CLI: `xmclaw onboard` is an interactive wizard that walks the same three steps end-to-end.

---

## 🗂️ Architecture

```
Clients  (CLI  ·  Web UI  ·  future channel adapters)
         ↕   WS  /agent/v2/{session_id}    +   HTTP  /api/v2/*
┌──────────────────────────────────────────────┐
│  Daemon   (FastAPI + Uvicorn + StaticFiles)  │
│  ├── AgentLoop    (per session)              │
│  │     run_turn: user → LLM → tools →        │
│  │                tools → LLM → done         │
│  │                                           │
│  ├── LLMProvider      anthropic / openai + translators
│  ├── ToolProvider     builtin / browser / lsp / mcp / composite
│  ├── MemoryProvider   sqlite-vec  (LRU + pinned_tags + cap)
│  ├── Skills           SkillBase + Registry
│  ├── SkillScheduler   bandit / promote / rollback
│  ├── HonestGrader     ran / returned / type_matched / side_effect
│  ├── EvolutionController   candidate → grader → promote
│  │                                           │
│  └── EventBus   (InProcess + SQLite WAL + FTS5)
│         ↑ subscribers: grader, scheduler, memory, cost, WS forward
└──────────────────────────────────────────────┘
   Data:  ~/.xmclaw/v2/{events.db, memory.db, pairing_token.txt, daemon.pid}
```

Authoritative design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Event contract: [docs/EVENTS.md](docs/EVENTS.md) · Tool contract: [docs/TOOLS.md](docs/TOOLS.md) · Data layout: [docs/WORKSPACE.md](docs/WORKSPACE.md).

---

## 🔄 How evolution works

Evolution is the runtime path, not a batch job:

1. **Propose** — the `EvolutionController` emits `skill_candidate_proposed` (a human-written skill or an LLM-proposed variant).
2. **Exercise** — the `SkillScheduler` routes real turns to candidate versions as bandit arms; real `ToolCall` / `ToolResult` events get generated.
3. **Grade on evidence** — the `HonestGrader` reads the event stream and decides `ran / returned / type_matched / side_effect_observable` per call. An LLM's opinion contributes ≤ 0.2 weight; the hard signals dominate.
4. **Promote or roll back** — the scheduler reads graded verdicts, emits `skill_promoted` or `skill_rolled_back` with `evidence: list[str]`. Promotion is a registry mutation; the next turn uses the new HEAD without restart.
5. **Audit forever** — every step is a `BehavioralEvent` in `events.db` (SQLite WAL + FTS5). Any decision replayable end-to-end months later.

On MiniMax, the full autonomous cycle lifts session-level mean reward by **18%** session-over-session with no human in the loop ([`tests/bench/phase3_autonomous_evolution_live.py`](tests/bench/phase3_autonomous_evolution_live.py)).

**What's missing (Epic #4):** a first-class view into what evolved and why. `xmclaw evolution show --since 7d`, a `SKILL_EVOLVED` flash in the REPL footer, and a reproducible `history.jsonl` under `~/.xmclaw/skills/` — currently engineering is rich, user-facing surface is thin.

---

## 🛡️ Security

XMclaw treats anything it didn't generate as untrusted:

- **WS + HTTP pairing token** — the daemon writes a 0600 token to `~/.xmclaw/v2/pairing_token.txt` on start; both the WebSocket handler and (since B-73) the entire `/api/v2/*` HTTP surface enforce it. WS rejects with `close(4401)`; HTTP returns 401. Constant-time compare. Allowlisted: `/health`, `/api/v2/pair` (UI bootstrap).
- **Request body size cap** — `BodySizeLimitMiddleware` (B-75) rejects any `/api/v2/*` request whose body exceeds 10 MB at 413, before parse. Stops a malicious or accidental 1 GB POST from OOM-killing the daemon.
- **Filesystem sandbox** — `tools.allowed_dirs` in `daemon/config.json` gates every `file_read` / `file_write` / `list_dir` / `file_delete` argument; traversal attempts return `ToolResult(ok=False)`. `file_delete` refuses to operate on the sandbox root itself.
- **Atomic file writes** — every persona / notes / journal / config write goes through `atomic_write_text` (tmp + `os.replace`). A daemon crash mid-write can never leave SOUL.md / MEMORY.md / config.json truncated.
- **No shell metacharacter parsing** — `bash` tool uses `subprocess.run(argv, shell=False)`. Nothing the model emits can be interpreted by a shell.
- **Prompt-injection scanner** — every `ToolResult.content`, every recalled memory chunk, AND (since B-79) every persona file (SOUL.md / IDENTITY.md / MEMORY.md / USER.md) passes `xmclaw.security.prompt_scanner.scan_text` before reaching the LLM context. HIGH-severity findings get redacted in place with `[redacted:<pattern_id>]`. 90+ patterns covering instruction overrides, role forgery, jailbreaks, exfiltration, indirect injection, and tool hijack — both English AND Chinese (B-80). Detections emit `PROMPT_INJECTION_DETECTED` (anti-req #14).
- **XSS-safe markdown rendering** — the chat panel renders LLM markdown via `marked@12` + `dompurify@3` from CDN. If DOMPurify fails to load (offline, firewall), B-72 forces fallback to an in-house escape-only renderer instead of letting raw HTML through.
- **Skill isolation** — `providers/runtime/process.py` runs untrusted skills in subprocesses with wall-clock + CPU caps; no module-level state leaks between runs.
- **Secret redaction** — `api_key` / `token` / `password` fields go through `utils.redact` before events, logs, or UI rendering.
- **MCP subprocess boundary** — each MCP server gets its own subprocess with JSON-RPC on stdin/stdout; no env-var inheritance unless the `mcp_servers.*` config declares it.

Run `xmclaw doctor` to audit pairing, config, allowed_dirs, workspace permissions, and secrets file mode.

---

## 📊 Event replay & observability

Every turn writes a `BehavioralEvent` stream to `~/.xmclaw/v2/events.db` (SQLite WAL + FTS5). Clients can:

- **Replay** — on WS reconnect, the daemon re-emits the session's events so the UI rehydrates without re-hitting the LLM.
- **Query** — `GET /api/v2/events?session_id=&since=&types=&q=` supports type filter + FTS5 keyword search across payloads.
- **Audit** — any grader verdict or skill promotion can be re-traced end-to-end months later. Events are frozen dataclasses — no in-place edits.

Cost tracking rides the same bus: each LLM call emits a `COST_TICK` event with input/output tokens + estimated cost; the daemon's `PerformanceMonitor` aggregates by provider / model / session.

Logs are **structured** (`structlog`) with secret scrubbing and session `contextvars` propagation.

---

## 🔧 CLI Reference

### Daemon lifecycle
```bash
xmclaw start              # Start daemon (background) + serve UI
xmclaw stop               # Stop daemon
xmclaw restart            # Restart in place
xmclaw status             # Show pid / port / uptime
xmclaw serve              # Foreground daemon (for debugging)
xmclaw ping               # Event-bus round-trip smoke test
xmclaw version            # Text or --json (name / version / python / platform)
```

### Interactive
```bash
xmclaw chat               # REPL talking to the daemon
xmclaw chat --plan        # Plan mode: approve steps before execution
xmclaw tools list         # Show registered tools
```

### Diagnostics
```bash
xmclaw doctor                     # 15 built-in checks
xmclaw doctor --fix               # Auto-remediate fixable checks
xmclaw doctor --json              # Machine-readable report
xmclaw doctor --network           # Also probe LLM endpoints
xmclaw doctor --discover-plugins  # Load third-party checks
```

### Config CRUD
```bash
xmclaw config init                       # Bootstrap daemon/config.json
xmclaw config show                       # Pretty-print (secrets masked)
xmclaw config show --reveal              # Print raw values
xmclaw config get <dotted.key>           # Read one key (masks sensitive leaves)
xmclaw config set <dotted.key> <value>   # JSON-parse scalars; fall back to string
xmclaw config unset <dotted.key>         # Remove a key; --prune-empty cascades
```

### Secrets (env > secrets.json 0600 > keyring)
```bash
xmclaw config set-secret <name>          # Reads via stdin (getpass)
xmclaw config get-secret <name>          # Masked by default; --reveal to unmask
xmclaw config delete-secret <name>
xmclaw config list-secrets               # Flags env overrides
```

### Backup & restore
```bash
xmclaw backup create [name]              # tar.gz + manifest (auto name YYYY-MM-DD-HHMMSS)
xmclaw backup list                       # --json for pipelines
xmclaw backup info <name>                # Pretty-print manifest (no re-hash)
xmclaw backup verify <name>              # sha256 integrity gate, --json
xmclaw backup restore <name>             # Atomic swap with .prev-<ts> rollback
xmclaw backup delete <name> [--yes]
xmclaw backup prune --keep 5 --yes       # Drop oldest beyond keep
```

### Memory
```bash
xmclaw memory stats                      # Layer counts, bytes, pinned; --json
```

Full help: `xmclaw --help` / `xmclaw <cmd> --help`.

---

## 🚦 Roadmap status

Delivery is tracked Epic-by-Epic in [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md). Snapshot as of **2026-04-25** (1.0 GA):

| State | Epics |
|---|---|
| ✅ **Done** (10) | #5 Memory eviction · #6 `XMC__` env override · #10 Doctor (15 checks + 5 auto-fix) · #11 Smart-gate CI · #12 AGENTS.md layering · #13 SQLite event bus + FTS5 · #14 Prompt-injection defense · #15 Structured logging · #16 Secrets (Fernet at-rest) · #17 Multi-agent (one daemon / one bus, agent-id routed) |
| 🟢 **Substantially in 1.0** (5) | #4 Evolution engine (UX Phase D → v2.x) · #9 Onboarding (cross-platform soak → 1.x.y) · #19 Cloud (Docker + GHCR publish in; systemd / Helm → v2.x) · #20 Backup (CLI + auto-daily in; zero-downtime reload → 1.x.y) · #23 Web UI Phase 1 (chat workspace in; rich panels → Epic #18 / v2.x) |
| 🟡 **Substrate ships, full feature → v2.x** (2) | #2 Plugin SDK (boundary + CI guard in; first real third-party plugin → v2.x) · #3 Sandbox (Guardians + ApprovalService + Scanner in; AgentLoop runtime.fork → v2.x) |
| ⏭ **Explicitly v2.x** (4) | #1 Channel SDK · #7 IDE / ACP entry · #8 Skill Hub · #18 Web UI Phase 2 rich panels |

**Milestones**: M1 / M8 / M9 ✅ closed for 1.0 GA. M1's 72h continuous-soak — plus Epic #4's killer-demo GIF, weekly grader +0.1, and `xmclaw evolution show --since 7d ≥ 3` real events — are non-code follow-ups deferred to the `1.x.y` patch cycle (they need runtime hours, not more code). M2 / M3 / M7 are explicitly post-1.0; M4 / M5 / M6 ship their substrate in 1.0 with surface-layer items deferred to v2.x per the [§M9 scope decision](docs/DEV_ROADMAP.md).

---

## 📁 Project Structure

```
xmclaw/
├── core/           Bus, IR, grader, evolution, scheduler                → core/AGENTS.md
├── daemon/         FastAPI + WebSocket + AgentLoop + static UI          → daemon/AGENTS.md
│   └── static/     Vanilla-JS single-page web UI (served at /ui/)
├── providers/      LLM / tool / memory / runtime / channel adapters     → providers/AGENTS.md
├── security/       Prompt-injection scanner + redactor                  → security/AGENTS.md
├── skills/         SkillBase + registry + demo skills                   → skills/AGENTS.md
├── cli/            `xmclaw` entry points + doctor + config + memory     → cli/AGENTS.md
├── utils/          Paths, logging, redaction, cost, secrets helpers     → utils/AGENTS.md
├── plugin_sdk/     Frozen plugin re-export surface (Epic #2)            → plugin_sdk/AGENTS.md
├── backup/         Archive + manifest primitives (Epic #20)             → backup/AGENTS.md
└── plugins/        Third-party plugin drop-in (pilot pending)

daemon/             Runtime config — `config.json` gitignored; `config.example.json` is the template
docs/               ARCHITECTURE · DEV_ROADMAP · EVENTS · DOCTOR · TOOLS · WORKSPACE · V2_* · …
scripts/            Dev/ops — `setup.{ps1,bat}`, `test_changed.py`, `lint_roadmap.py`, …
tests/              `unit/` + `integration/` + `conformance/` + `bench/` · lane map in `scripts/test_lanes.yaml`
```

Runtime data (`events.db`, `memory.db`, `daemon.pid`, `pairing_token.txt`) lives under `~/.xmclaw/v2/` — **not in the repo**. See [docs/WORKSPACE.md](docs/WORKSPACE.md).

---

## 📚 Documentation

| | |
|---|---|
| [Architecture](./docs/ARCHITECTURE.md) | System design, data flows, wire protocol |
| [Tools](./docs/TOOLS.md) | Built-in tools reference (file / bash / git / browser / mcp…) |
| [Events](./docs/EVENTS.md) | Typed event stream contract |
| [Config](./docs/CONFIG.md) | `daemon/config.json` fields + `XMC__` env overrides |
| [Doctor](./docs/DOCTOR.md) | Diagnostic checks + `--fix` runner + plugin API |
| [Workspace](./docs/WORKSPACE.md) | `~/.xmclaw/` layout + `XMC_DATA_DIR` |
| [Dev Roadmap](./docs/DEV_ROADMAP.md) | Epics, milestones, execution protocol |
| [V2 Status](./docs/V2_STATUS.md) | Anti-req scorecard + bench numbers |

---

## 🧪 Development

```bash
# Full suite (slow)
python -m pytest tests/ -v

# Smart-gate — only affected lanes (Epic #11)
python scripts/test_changed.py --dry-run
python scripts/test_changed.py                # run the selected lanes
python scripts/test_changed.py --all          # forced full suite

# Coverage
python -m pytest tests/ --cov=xmclaw --cov-report=html

# Lint & type check
ruff check xmclaw/ --fix
mypy xmclaw/

# Roadmap lint (6 rules — enforces dev-discipline)
python scripts/lint_roadmap.py docs/DEV_ROADMAP.md
```

Per-directory contracts live in `xmclaw/<subdir>/AGENTS.md` — read those before editing code in that subdir. Import direction is enforced by `scripts/check_import_direction.py` (`core/` cannot import `providers/`, etc).

---

## 🤝 Contributing

Contributions welcome. Start here:

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev workflow, lint / type / test gates, two-commit roadmap protocol.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community baseline.
- [SECURITY.md](SECURITY.md) — vulnerability disclosure (please use private advisories, not public issues).
- [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) — Anti-Req checklist + roadmap discipline reminders.
- [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) — structured forms for bugs / features / questions (Discussions preferred for open-ended threads).

Any Epic-touching PR must cite the Epic number (`Epic #11:`, `Epic #14 partial:`, etc) and update [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md) per the [execution protocol](docs/DEV_ROADMAP.md#36-执行协议execution-protocol-每次开发必读). See [CLAUDE.md](CLAUDE.md) for the AI-assistant onboarding notes.

---

## 📄 License

MIT License — see [LICENSE](LICENSE).

---

Built for developers who want a personal, self-improving AI agent they fully own — code, data, and decisions.
