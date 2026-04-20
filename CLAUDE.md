# CLAUDE.md

Guidance for Claude Code (and other AI coding assistants) when working in this repository.

## Project

**XMclaw** is a local-first, self-evolving AI agent runtime written in Python. A single FastAPI daemon hosts the AgentLoop, ToolRegistry, MemoryManager, and EvolutionEngine; clients (Web UI, CLI, desktop tray) connect to it over WebSocket. See [README.md](README.md) for the user-facing overview and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the definitive system design.

## Repository Layout

```
xmclaw/              Python package (the actual runtime)
├── core/            AgentLoop, Orchestrator, PromptBuilder, Reflection
├── daemon/          FastAPI + WebSocket server, lifecycle
├── gateway/         HTTP / WS request handlers
├── evolution/       GeneForge, SkillForge, VFM, Validator, Scheduler
├── genes/           Gene matching and registry
├── llm/             Anthropic + OpenAI router
├── memory/          SQLite, VectorStore (sqlite-vec), SessionManager
├── tools/           Built-in tools + MCP bridge
├── integrations/    Slack, Discord, Telegram, GitHub, Notion, 飞书, QQ, 企业微信
├── sandbox/         Docker + process sandboxing
├── desktop/         Desktop tray + browser launcher
├── cli/             `xmclaw` CLI entry points
└── utils/           Logging, paths, security helpers

web/                 Vite-based web UI (vanilla JS + CSS)
daemon/              Daemon runtime config (config.json — gitignored, config.example.json is the template)
agents/              Agent profiles (agent.json is gitignored; PROFILE.md / SOUL.md are committed)
shared/              Auto-generated genes/ and skills/ (populated at runtime)
plugins/             LLM and tool plugins
docs/                Architecture, CLI, Tools, Evolution, Integrations, Testing, Troubleshooting
tests/               pytest suites (test_bash.py, test_config.py, test_evolution.py, …)
scripts/             Build & installer scripts (build_exe_fast.py, xmclaw_setup.iss, …)
.github/workflows/   CI (python-package-conda.yml, python-publish.yml)
```

Anything not in that tree is either generated at runtime, gitignored dev scratch, or a legacy artifact — check `.gitignore` before assuming a root-level file belongs in git.

## Common Commands

```bash
# Install
pip install -e .                 # runtime
pip install -e ".[dev]"          # + pytest, ruff, mypy

# Run
xmclaw start                     # launch daemon + open web UI (http://127.0.0.1:8765)
xmclaw stop
xmclaw chat                      # interactive CLI
xmclaw chat --plan               # plan mode (approve steps first)
xmclaw config init               # interactive config
xmclaw doctor                    # diagnostics

# Test & lint
python -m pytest tests/ -v
python -m pytest tests/ --cov=xmclaw --cov-report=html
ruff check xmclaw/ --fix
mypy xmclaw/

# Build desktop installer (Windows)
python scripts/build_exe_fast.py
# then scripts/xmclaw_setup.iss produces the installer via InnoSetup
```

Dev env is Windows-first; scripts use `.bat` / `.ps1`. Use `bash` syntax on Git Bash / WSL — forward-slash paths work.

## Key Conventions

- **Config with secrets is gitignored.** `daemon/config.json` and `agents/*/agent.json` hold API keys — never commit. Use `daemon/config.example.json` or env vars prefixed with `XMC__` (e.g. `XMC__llm__anthropic__api_key`).
- **Generated code lives under `shared/`.** Genes and skills produced by the EvolutionEngine are written there at runtime; do not hand-edit committed ones without understanding the evolution pipeline in [docs/EVOLUTION.md](docs/EVOLUTION.md).
- **Tool additions are registered, not imported.** New tools go in `xmclaw/tools/` and are picked up by `ToolRegistry`. Update [docs/TOOLS.md](docs/TOOLS.md) when adding one.
- **Events are the contract.** The daemon emits a typed event stream (`chunk`, `state`, `tool_call`, `tool_result`, `ask_user`, `reflection`, `cost`, `done`, `error`) — see [docs/EVENTS.md](docs/EVENTS.md). Clients must not assume fields outside that schema.

## Git Workflow

- Main branch is `main`. Do not push directly to it.
- Create a feature branch: `git checkout -b feat/...` / `fix/...` / `docs/...`.
- Open a PR: `gh pr create`.
- Keep commit messages in English. Conventional Commits encouraged (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`).

## Prerequisites

- Python 3.10+ (see `pyproject.toml`).
- Optional: `playwright install chromium` for browser tools; `pyautogui` + `mss` for computer-use.
- No Node.js required for runtime — only for working on `web/` (Vite dev server).
