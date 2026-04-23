# 🦞 XMclaw — Local-First, Self-Evolving AI Agent Runtime

<p align="center">
  <strong>A personal AI agent that runs on your machine. Thinks, acts, remembers, and improves itself on hard evidence — not on its own self-assessment.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <img src="https://img.shields.io/badge/status-pre--v1%20(2.0.0.dev0)-orange?style=for-the-badge" alt="Status">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform">
</p>

> ### 🚧 Honest status (2026-04-24)
>
> XMclaw is a **development preview**, not a 1.0 release. Current version is `2.0.0.dev0`.
>
> **What works today** (`main` + `tests/` green locally — 1100 tests collected, 1093 pass, 7 skip):
>
> - The **self-evolution spine** — streaming bus + Honest Grader + Online Scheduler + EvolutionController + SkillRegistry — is real code, used end-to-end by live benches on MiniMax:
>
>   | Live bench | Result | Gate |
>   |---|---|---|
>   | [`phase1_live_learning_curve`](tests/bench/phase1_live_learning_curve.py) | **1.12× over uniform baseline** | ≥ 1.05× |
>   | [`phase2_tool_aware_live`](tests/bench/phase2_tool_aware_live.py) | **100% real tool-firing** per scored turn | ≥ 80× |
>   | [`phase3_autonomous_evolution_live`](tests/bench/phase3_autonomous_evolution_live.py) | **1.18× session-over-session** after auto-promote | ≥ 1.05× |
>
> - A usable **FastAPI + WebSocket daemon** with pairing-token auth, event replay, and SQLite WAL + FTS5 event persistence.
> - A **CLI** covering daemon lifecycle / interactive chat / config / secrets / backup / memory / doctor (15 checks, 5 auto-fixable).
> - A **basic web UI** (vanilla JS under `xmclaw/daemon/static/`) served at `http://127.0.0.1:8765/ui/` — enough to drive the daemon, not a polished product.
>
> **What's in progress** (see [Roadmap status](#-roadmap-status)):
>
> - **Epic #4** — surfacing evolution to the user (`gene_forge` / `xmclaw evolution show` CLI / killer demo GIF). The engine works; making it visible is the next push.
> - **Epic #2** — plugin SDK pilot migration (boundary + CI guard shipped; example plugin pending).
> - **Epic #3** — skill sandbox (runtime + factory shipped; AgentLoop wiring + 8 guardian rules + ApprovalService pending).
> - **Epic #16** — secrets Phase 2 (Fernet-at-rest; Phase 1 three-tier env/file/keyring lookup already live).
> - **Epic #20** — backup/restore zero-downtime reload (Phase 1 CLI complete; daemon reloader pending).
>
> **What's not built yet** (planned, `⬜` in the roadmap): Channel SDK (#1), IDE / ACP entrypoints (#7), Skill Hub (#8), Onboarding wizard (#9), multi-agent (#17), rich Web UI Phase 2 (#18), cloud/systemd templates (#19).
>
> **Roadmap:** 20 Epics — **8 ✅ done, 4 🟡 in progress, 8 ⬜ planned**. None of the 9 milestones (M1–M9) have been officially closed yet. See [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md) for the full execution plan.
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
| **🩺 Doctor with Plugins** | `xmclaw doctor` runs **15 built-in checks** (config / LLM / tools / workspace / pairing / port / events db / memory db / skill runtime / connectivity / roadmap lint / stale pid / daemon health / backups / secrets) plus any third-party check on the `xmclaw.doctor` entry-point group. `--fix` auto-remediates 5 of them. |
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

## 📸 UI Preview

![XMclaw Warm Theme Dashboard](docs/assets/ui-preview.png)

*The current web UI is a vanilla-JS single-page app under `xmclaw/daemon/static/` — enough to drive the daemon end-to-end. A richer Web UI (Epic #18) is planned for Milestone M8.*

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

- **WS pairing token** — the daemon writes a 0600 token to `~/.xmclaw/v2/pairing_token.txt` on start; WS connects without it get `close(4401)`. Constant-time compare (`xmclaw/daemon/auth.py`).
- **Filesystem sandbox** — `tools.allowed_dirs` in `daemon/config.json` gates every `file_read` / `file_write` / `list_dir` argument; traversal attempts return `ToolResult(ok=False)`.
- **No shell metacharacter parsing** — `bash` tool uses `subprocess.run(argv, shell=False)`. Nothing the model emits can be interpreted by a shell.
- **Prompt-injection scanner** — every `ToolResult.content` passes `xmclaw.security.prompt_scanner.scan_text` before returning to the LLM; detections emit `PROMPT_INJECTION_DETECTED` (anti-req #14).
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

Delivery is tracked Epic-by-Epic in [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md). Snapshot as of **2026-04-24**:

| State | Epics |
|---|---|
| ✅ **Done** (8) | #5 Memory eviction · #6 `XMC__` env override · #10 Doctor (15 checks + 5 auto-fix) · #11 Smart-gate CI · #12 AGENTS.md layering · #13 SQLite event bus + FTS5 · #14 Prompt-injection defense · #15 Structured logging |
| 🟡 **In progress** (4) | #2 Plugin SDK (boundary + guard done; pilot pending) · #3 Sandbox (runtime + factory done; AgentLoop wiring pending) · #16 Secrets (Phase 1 done; Fernet Phase 2 pending) · #20 Backup/restore (Phase 1 CLI done; daemon reloader pending) |
| ⬜ **Planned** (8) | #1 Channel SDK · **#4 Evolution UX (★core)** · #7 IDE / ACP entry · #8 Skill Hub · #9 Onboarding · #17 Multi-agent · #18 Web UI Phase 2 · #19 Cloud / systemd templates |

**Milestones M1–M9** all remain formally open; several (M1 Daemon GA, M8 Observability) are close to their exit criteria and awaiting a closeout pass.

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

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Any Epic-touching PR must cite the Epic number (`Epic #11:`, `Epic #14 partial:`, etc) and update [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md) per the [execution protocol](docs/DEV_ROADMAP.md#36-执行协议execution-protocol-每次开发必读). See [CLAUDE.md](CLAUDE.md) for the AI-assistant onboarding notes.

---

## 📄 License

MIT License — see [LICENSE](LICENSE).

---

Built for developers who want a personal, self-improving AI agent they fully own — code, data, and decisions.
