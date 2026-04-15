# XMclaw

**Local-first, self-evolving AI Agent runtime.**

Inspired by OpenClaw, Hermes Agent, and Claude Code.

[Website](#) · [Docs](./docs) · [Architecture](./docs/ARCHITECTURE.md) · [Evolution](./docs/EVOLUTION.md) · [Tools](./docs/TOOLS.md)

---

## What is XMclaw?

XMclaw is a **personal AI agent operating system** that runs entirely on your machine. It is not a chatbot. It is a runtime that can think, act, remember, reflect, and improve itself over time.

- **Local-first**: All memory, config, and evolution data stays on your device.
- **Dual LLM ports**: OpenAI-compatible APIs and Anthropic Claude.
- **Self-evolving**: Automatically generates Genes and Skills from conversation patterns.
- **Agent OS dashboard**: A native desktop app built with PySide6.
- **CLI-first**: Powerful terminal interface with streaming, plan mode, and tool visibility.

---

## Everything we built so far

### Core runtime
- [x] **Daemon + WebSocket gateway** — FastAPI server with real-time streaming
- [x] **AgentLoop** — Think → act → observe → reflect cycle
- [x] **Orchestrator** — Multi-agent instance management
- [x] **PromptBuilder** — Dynamic system prompt assembly with Gene injection
- [x] **LLMRouter** — Unified OpenAI and Anthropic client routing
- [x] **CostTracker** — Token and cost estimation per turn

### Memory
- [x] **Session logs** — JSONL conversation history
- [x] **SQLite metadata store** — Agent configs, todos, task states
- [x] **Vector memory** — SQLite-vec with LLM embeddings for long-term retrieval
- [x] **MEMORY.md / PROFILE.md integration** — Structured long-term memory files

### Tools
- [x] `file_read`, `file_write`, `file_edit`
- [x] `bash` — with dangerous/suspicious pattern guards
- [x] `browser` — headless and visible modes
- [x] `web_search`, `web_fetch`
- [x] `todo`, `task`
- [x] `ask_user` — pause and resume for human confirmation
- [x] `agent` — spawn sub-agents for delegated work
- [x] `skill` — dynamically load generated skills
- [x] `memory_search` — vector search over long-term memory
- [x] `glob`, `grep`
- [x] `git` — commit, push, pull, status
- [x] `computer_use` — screenshot, click, type, keypress
- [x] `test` — auto-generate and run pytest suites
- [x] `mcp` — Model Context Protocol integration

### Autonomous evolution
- [x] **Pattern detection** — Intent and trend analysis from conversation logs
- [x] **Insight extraction** — Structured lessons and improvement opportunities
- [x] **GeneForge** — Auto-generate behavioral Genes with prompt injection
- [x] **SkillForge** — Auto-generate executable Skill tools
- [x] **EvolutionValidator** — Real execution validation (compile + import + instantiate + run)
- [x] **VFM scoring** — Value Function Model decides whether to solidify
- [x] **Hot reload** — New Skills are available immediately without restart
- [x] **ReflectionEngine** — Post-conversation self-review and lesson extraction

### Interfaces
- [x] **Desktop app** — PySide6 Agent OS dashboard with 6 views (chat, workspace, evolution, memory, tools, settings)
- [x] **Web UI** — Agent OS dashboard in the browser
- [x] **CLI** — Rich-based terminal client with full protocol support

### Plan mode & task system
- [x] **Plan mode** — Agent thinks before acting; plans are user-editable
- [x] **Task system** — Track long-running tasks across sessions
- [x] **ask_user pause** — Agent state becomes WAITING until human responds

---

## How it works (short)

```
User (Desktop / Web / CLI)
           │
           ▼
    WebSocket Gateway
           │
           ▼
    ┌──────────────┐
    │ AgentLoop    │
    │  - Prompt    │
    │  - LLM       │
    │  - Tools     │
    │  - Memory    │
    │  - Reflect   │
    └──────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
  Genes      Skills
     │           │
     └─────┬─────┘
           ▼
    Evolution Engine
```

1. The user sends a message over WebSocket.
2. `AgentLoop` assembles context, injects matched **Genes**, and streams the LLM response.
3. If the model emits tool calls, `ToolRegistry` executes them and returns observations.
4. The loop continues until the model has no more tools to call.
5. `ReflectionEngine` reviews the conversation and saves lessons.
6. The `EvolutionEngine` periodically analyzes logs and generates new **Genes** and **Skills**.

---

## Quick start

```bash
# Install
pip install -e .

# Optional extras
pip install pyautogui mss Pillow   # computer_use
pip install mcp                     # MCP integration

# Start the daemon
xmclaw start

# Open the desktop app
python -m xmclaw.desktop.app

# Or use the CLI
xmclaw chat
xmclaw chat --plan

# Stop the daemon
xmclaw stop
```

Configure your LLM in `agents/default/agent.json`.

---

## Key subsystems

| Subsystem | Docs | Description |
|-----------|------|-------------|
| Architecture | [ARCHITECTURE.md](./docs/ARCHITECTURE.md) | Gateway, agent loop, wire protocol, data flow |
| Tools | [TOOLS.md](./docs/TOOLS.md) | Built-in tools, skill generation, security model |
| Evolution | [EVOLUTION.md](./docs/EVOLUTION.md) | Gene/Skill generation, VFM, hot reload |
| Desktop | [DESKTOP.md](./docs/DESKTOP.md) | Native app usage guide |
| CLI | [CLI.md](./docs/CLI.md) | Terminal commands and chat protocol |

---

## Development principles

1. **Never slow, never forget** — Performance and memory are non-negotiable.
2. **Commit after every change** — Git is our safety net.
3. **CLI first, then GUI, then voice** — Progressive interaction expansion.
4. **Connections must be explicit** — Every module's inputs and outputs are traceable.
5. **Verify, reflect, evolve** — Closed-loop self-improvement.

---

## License

MIT
