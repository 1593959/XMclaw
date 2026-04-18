# 🦞 XMclaw — Local-First, Self-Evolving AI Agent Runtime

<p align="center">
  <strong>Your AI agent that runs on your machine. Thinks. Acts. Remembers. Improves itself.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-package-conda.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <a href="https://github.com/1593959/XMclaw/releases"><img src="https://img.shields.io/github/v/release/1593959/XMclaw?include_prereleases&style=for-the-badge" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
</p>

**XMclaw** is a personal AI agent that runs entirely on your machine. It is not a chatbot — it is a runtime that can think, act, remember, and continuously improve itself over time.

Unlike a stateless chat interface, XMclaw maintains memory across sessions, executes real tools on your filesystem and system, and automatically evolves its own gene pool and skill library based on your usage patterns.

[Website](#) · [Docs](./docs) · [Architecture](./docs/ARCHITECTURE.md) · [Evolution](./docs/EVOLUTION.md) · [CLI](./docs/CLI.md) · [Tools](./docs/TOOLS.md)

---

## ✨ What makes XMclaw different

| | |
|---|---|
| **🧠 Self-Evolving** | XMclaw watches its own performance. The EvolutionEngine detects patterns, scores insights with VFM (Value · Faithfulness · Measurability), and auto-generates new Genes and Skills — no manual curation needed. |
| **💾 Local-First Memory** | All sessions, metadata, and vectors live in SQLite + sqlite-vec on your machine. Nothing leaves your disk unless you explicitly push it. |
| **🔧 Hot-Reload Skills** | Generated skills are compiled, validated, and registered without restart. Next message already uses the new capability. |
| **🛡️ Built-In Security** | Bash guard rails, dangerous pattern blocking, Git auto-rollback before file changes, and encrypted API key storage. |
| **🌐 Multi-Interface** | Desktop app (Browser + System Tray), Web UI, or Rich CLI — all sharing the same running daemon. |
| **🔌 MCP & Integrations** | MCP protocol support, plus Slack / Discord / Telegram / GitHub / Notion integrations ready to connect. |

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
# Desktop app (opens browser + system tray)
python -m xmclaw.desktop.app

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
│  ├── ToolRegistry  ← 20+ tools   │
│  │   ├── file / bash / browser   │
│  │   ├── git / mcp / skill       │
│  │   └── web_search / memory…    │
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
Third-party: Slack · Discord · Telegram · GitHub · Notion
```

---

## 🔄 How Evolution Works

XMclaw continuously gets better without you lifting a finger:

1. **Pattern Detection** — analyzes session logs after each conversation
2. **Insight Extraction** — identifies behavioral patterns and useful tool sequences
3. **Gene Generation** — creates lightweight behavioral prompts (GeneForge)
4. **Skill Generation** — builds executable Python skills from proven patterns (SkillForge)
5. **Validation** — compiles, runs, and scores new code before registration (VFM scoring)
6. **Hot Reload** — new skills are immediately available in the next turn

Genes are injected into the system prompt at runtime. Skills become real tools. Over time, XMclaw accumulates a personal knowledge base tailored to exactly how *you* work.

---

## 🛡️ Security

XMclaw treats your system as a production environment:

- **Bash Guard Rails** — blocks `rm -rf /`, `mkfs`, `dd`, and other destructive patterns
- **Dangerous Pattern Blocking** — warns on `curl | bash`, `git push --force`, and similar
- **Git Auto-Rollback** — commits state before file changes, rolls back on failure
- **Encrypted Secrets** — API keys stored with Fernet encryption + PBKDF2 key derivation
- **Sandbox Ready** — Docker/process sandboxing available for untrusted skills

Run `xmclaw doctor` to audit your security posture.

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
├── core/           AgentLoop, Orchestrator, PromptBuilder, Reflection
├── daemon/         FastAPI server, WebSocket gateway, lifecycle
├── desktop/        Browser + System Tray app
├── evolution/      GeneForge, SkillForge, VFM, Validator, Scheduler
├── genes/          Gene matching and registry
├── gateway/        HTTP/WebSocket handlers
├── integrations/   Slack, Discord, Telegram, GitHub, Notion
├── llm/            Anthropic + OpenAI router
├── memory/         SQLite, VectorStore, SessionManager
├── sandbox/        Docker + process sandboxing
├── tools/          20+ built-in tools + MCP
└── utils/          Logging, paths, security
web/                Web UI assets
shared/
├── genes/          Auto-generated gene pool (~200 genes)
└── skills/        Auto-generated skill library (~100 skills)
agents/             Agent profiles and configuration
docs/               Architecture, CLI, Tools, Evolution, Desktop
tests/              pytest test suites
```

---

## 📚 Documentation

| | |
|---|---|
| [Architecture](./docs/ARCHITECTURE.md) | System design, data flows, wire protocol |
| [Tools](./docs/TOOLS.md) | Built-in tools reference (file, bash, git, browser, mcp…) |
| [Evolution](./docs/EVOLUTION.md) | Self-evolution system, GeneForge, SkillForge, VFM |
| [CLI](./docs/CLI.md) | All terminal commands |
| [Desktop](./docs/DESKTOP.md) | Desktop app guide |

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
