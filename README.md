# 🦞 XMclaw — Local-First, Self-Evolving AI Agent Runtime

<p align="center">
  <strong>Your AI agent that runs on your machine. Thinks. Acts. Remembers. Improves itself.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <a href="https://github.com/1593959/XMclaw/releases"><img src="https://img.shields.io/github/v/release/1593959/XMclaw?include_prereleases&style=for-the-badge" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/badge/CI-Windows%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform"></a>
</p>

> ### 🚀 v2 delivery status (2026-04-21)
>
> A ground-up v2 rewrite is live on `main`. The self-evolution spine —
> streaming observer bus + honest grader + online scheduler + versioned
> skill registry + autonomous evolution controller — is **validated on a
> real LLM with no human in the loop**:
>
> | Live bench | On | Result | Gate |
> |---|---|---|---|
> | [Learning curve](tests/bench/phase1_live_learning_curve.py) | MiniMax | **1.12× over uniform baseline** | ≥ 1.05× |
> | [Tool-aware loop](tests/bench/phase2_tool_aware_live.py) | MiniMax | **100% real tool-firing** on every scored turn | ≥ 80% |
> | [Autonomous evolution](tests/bench/phase3_autonomous_evolution_live.py) | MiniMax | **1.18× session-over-session** after auto-promote | ≥ 1.05× |
>
> **410 v2 tests pass across Windows / macOS / Linux.** End-to-end
> usable via the v2 CLI:
>
> ```bash
> xmclaw v2 ping                 # bus round-trip smoke test
> xmclaw v2 serve                # FastAPI + WS daemon
>                                #   reads daemon/config.json (LLM key + fs allowlist)
>                                #   writes pairing token to ~/.xmclaw/v2/ (0600)
> xmclaw v2 chat                 # interactive REPL talking to the daemon
> ```
>
> **12 / 14 anti-requirements** are encoded in code with dedicated tests,
> including ClawJacked-style cross-origin WS hijack defense (pairing
> token, constant-time compare, `close(4401)` on invalid auth). See the
> full scorecard in [docs/V2_STATUS.md](docs/V2_STATUS.md). Design docs:
> [docs/REWRITE_PLAN.md](docs/REWRITE_PLAN.md) ·
> [docs/V2_DEVELOPMENT.md](docs/V2_DEVELOPMENT.md).
>
> v1 modules still live alongside v2 during strangler-fig transition — the
> shipping installer continues to use v1 until Phase 4 (daemon integration +
> release pipeline rewrite) completes.

**XMclaw** is a personal AI agent that runs entirely on your machine. It is not a chatbot — it is a runtime that can think, act, remember, and continuously improve itself over time.

Unlike a stateless chat interface, XMclaw maintains memory across sessions, executes real tools on your filesystem and system, and automatically evolves its own gene pool and skill library based on your usage patterns.

[Docs](./docs) · [Architecture](./docs/ARCHITECTURE.md) · [Tools](./docs/TOOLS.md) · [Events](./docs/EVENTS.md) · [Doctor](./docs/DOCTOR.md) · [Config](./docs/CONFIG.md) · [Roadmap](./docs/DEV_ROADMAP.md)

---

## ✨ What makes XMclaw different

| | |
|---|---|
| **🧠 Self-Evolving** | XMclaw watches its own performance. The EvolutionEngine detects 5 pattern types, scores insights with VFM, and auto-generates Genes and Skills — no manual curation needed. |
| **💾 Local-First Memory** | All sessions, metadata, and vectors live in SQLite + sqlite-vec on your machine. Import/Export in JSONL, JSON, or ZIP. Nothing leaves your disk unless you explicitly push it. |
| **🔧 Hot-Reload Skills** | Generated skills are compiled, validated, and registered without restart. Next message already uses the new capability. |
| **🛡️ Built-In Security** | Unified Permission Manager (ALLOW/ASK/BLOCK), 23-tool categorization, path sandbox, URL whitelist, audit logging, encrypted secrets. |
| **🌐 Multi-Interface** | Web UI and Rich CLI — both share the same running daemon. |
| **🔌 MCP & Integrations** | MCP protocol support, plus Slack / Discord / Telegram / GitHub / Notion / 飞书 / QQ频道 / 企业微信 integrations ready to connect. |
| **📊 Performance Monitoring** | Per-session LLM token counts, tool call stats, skill success rates, and cost estimation. |
| **🔁 Multi-Trigger Reflection** | Auto-reflection on errors, conversation end, periodic intervals, or on demand. Insights feed back into the evolution pipeline. |

---

## Install

```bash
# Clone
git clone https://github.com/1593959/XMclaw.git
cd XMclaw

# Install
pip install -e .

# With dev extras (pytest, ruff, mypy)
pip install -e ".[dev]"

# Optional: computer-use support
pip install pyautogui mss
pip install playwright && playwright install chromium
```

First run creates `daemon/config.json` automatically. Configure your API keys:

```bash
xmclaw config init
# or edit daemon/config.json directly
# or set env: XMC__llm__anthropic__api_key="sk-ant-..."
```

Verify your setup:

```bash
xmclaw doctor
```

---

## Quick Start

```bash
# Start daemon + web UI
xmclaw start
# → open http://127.0.0.1:8765

# Rich CLI
xmclaw chat
xmclaw chat --plan    # plan mode: see the execution plan before it runs

# Stop daemon
xmclaw stop
```

---

## 📸 UI Preview

![XMclaw Warm Theme Dashboard](docs/assets/ui-preview.png)

*Warm cream-toned Dashboard with coral accents — designed for extended use*

## 🗂️ Architecture

```
Clients (Desktop / Web / CLI)
         ↕ WebSocket
┌──────────────────────────────────┐
│  Daemon (FastAPI + Uvicorn)      │
│  ├── AgentLoop                    │
│  │   ├── think → act → observe   │
│  │   ├── PromptBuilder + Genes   │
│  │   └── ReflectionEngine        │
│  ├── ToolRegistry  ← 23 tools   │
│  │   ├── file / bash / browser   │
│  │   ├── git / mcp / skill       │
│  │   └── web_search / memory…    │
│  ├── SkillMatcher ← 5-dim scoring│
│  ├── LLMRouter  ← Anthropic/OpenAI│
│  ├── MemoryManager               │
│  │   ├── SessionManager (JSONL)  │
│  │   ├── SQLiteStore             │
│  │   └── VectorStore (sqlite-vec)│
│  ├── EvolutionEngine             │
│  │   ├── GeneForge               │
│  │   ├── SkillForge              │
│  │   └── VFM Scoring             │
│  └── EventBus  ← pub/sub         │
└──────────────────────────────────┘
         ↕ REST / WebSocket
Third-party: Slack · Discord · Telegram · GitHub · Notion · 飞书 · QQ频道 · 企业微信
```

---

## 🔄 How Evolution Works

XMclaw continuously gets better without you lifting a finger:

1. **Pattern Detection** — analyzes session logs after each conversation (5 pattern types)
2. **Insight Extraction** — identifies behavioral patterns and useful tool sequences
3. **Gene Generation** — creates lightweight behavioral prompts (GeneForge)
4. **Skill Generation** — builds executable Python skills from proven patterns (SkillForge)
5. **Validation** — compiles, runs, and scores new code before registration (VFM scoring)
6. **Hot Reload** — new skills are immediately available in the next turn
7. **Multi-Trigger Reflection** — auto-reflects after errors, on conversation end, periodically, or on demand

Genes are injected into the system prompt at runtime. Skills become real tools. Over time, XMclaw accumulates a personal knowledge base tailored to exactly how *you* work.

---

## 🛡️ Security

XMclaw treats your system as a production environment:

- **Bash Guard Rails** — blocks `rm -rf /`, `mkfs`, `dd`, and other destructive patterns
- **Dangerous Pattern Blocking** — warns on `curl | bash`, `git push --force`, and similar
- **Git Auto-Rollback** — commits state before file changes, rolls back on failure
- **Encrypted Secrets** — API keys stored with Fernet encryption + PBKDF2 key derivation
- **Sandbox Ready** — Docker/process sandboxing available for untrusted skills
- **Unified Permission Manager** — 3-level (ALLOW/ASK/BLOCK), 23-tool categorization, path sandbox, URL whitelist, audit logging
- **Hot-Reload Config** — `daemon/config.json` changes take effect without restart

Run `xmclaw doctor` to audit your security posture.

---

## 📊 Session Import/Export

Sessions can be exported and imported for backup, migration, or sharing:

- **Formats**: JSONL (line-by-line), JSON (array), ZIP (with metadata)
- **Import modes**: Replace, Append, Merge (deduplication)
- **Audit trail**: All exports listed with size and timestamp

---

## 📈 Performance Monitoring

Built-in performance tracking for every session:

- **LLM calls**: count, token usage (input/output), estimated cost
- **Tool calls**: per-tool call counts and success rates
- **Agent turns**: conversation depth and statistics
- **Skill stats**: usage frequency and success rate per skill

---

## 🔁 Multi-Trigger Reflection

XMclaw reflects on its own behavior at key moments:

- **ERROR_OCCURRED** — auto-triggered after failures; analyzes root cause and prevention
- **CONVERSATION_END** — summarizes the session, extracts lessons
- **PERIODIC** — regular checkpoint reflections during long conversations
- **USER_REQUEST** — on-demand reflection when user asks for it

Reflection insights are stored in memory and fed back into the evolution pipeline.

---

## 🔧 CLI Reference

```bash
xmclaw start              # Start daemon + web UI
xmclaw stop               # Stop daemon
xmclaw chat               # Interactive CLI chat
xmclaw chat --plan        # Plan mode (approve steps before execution)
xmclaw config init        # Interactively configure API keys
xmclaw config set <key> <value>   # e.g. xmclaw config set llm.anthropic.model claude-sonnet-4-20250514
xmclaw doctor             # Run diagnostics
xmclaw --help             # Full command reference
```

---

## 📁 Project Structure

```
xmclaw/
├── core/           Bus, IR, grader, evolution, scheduler
├── daemon/         FastAPI server, WebSocket gateway, lifecycle, factory
├── providers/      LLM / tool / memory / runtime / channel adapters
│   ├── llm/        Anthropic + OpenAI + router
│   ├── tool/       Built-in tools (file/bash/git/browser/…) + MCP
│   ├── memory/     SQLite-vec memory store
│   ├── runtime/    Sandbox / process runners
│   └── channel/    Integration channels (Slack / Discord / Telegram / …)
├── security/       Prompt-injection scanner + redactor + policy gate
├── skills/         SkillBase + registry + demo skills
├── cli/            `xmclaw` entry points + doctor + config / memory subcommands
├── utils/          Paths, logging, redaction, cost helpers
└── plugins/        Third-party plugin loader (Epic #2 WIP)
web/                Vite-based Web UI (vanilla JS + CSS)
shared/             Generated at runtime: genes/, skills/
agents/             Agent profiles (PROFILE.md / SOUL.md committed; agent.json gitignored)
daemon/             Runtime config (config.json gitignored; config.example.json is the template)
docs/               ARCHITECTURE, DEV_ROADMAP, EVENTS, DOCTOR, TOOLS, …
tests/              pytest suites
```

---

## 📚 Documentation

| | |
|---|---|
| [Architecture](./docs/ARCHITECTURE.md) | System design, data flows, wire protocol |
| [Tools](./docs/TOOLS.md) | Built-in tools reference (file, bash, git, browser, mcp…) |
| [Events](./docs/EVENTS.md) | Typed event stream contract |
| [Config](./docs/CONFIG.md) | `daemon/config.json` fields + `XMC__` env overrides |
| [Doctor](./docs/DOCTOR.md) | Diagnostic checks + `--fix` runner + plugin API |
| [Workspace](./docs/WORKSPACE.md) | `~/.xmclaw/` layout + `XMC_DATA_DIR` |
| [Dev Roadmap](./docs/DEV_ROADMAP.md) | Epics, milestones, execution protocol |

---

## 🧪 Development

```bash
# Run tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=xmclaw --cov-report=html

# Lint & type check
ruff check xmclaw/ --fix
mypy xmclaw/

# Build distribution
python -m build
```

---

## 🤝 Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## 📄 License

MIT License — see [LICENSE](LICENSE).

---

Built with ❤️ for developers who want a truly personal, self-improving AI agent.
