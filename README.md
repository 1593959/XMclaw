# XMclaw

[![GitHub stars](https://img.shields.io/github/stars/1593959/XMclaw?style=flat-square)](https://github.com/1593959/XMclaw/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/1593959/XMclaw?style=flat-square)](https://github.com/1593959/XMclaw/network/members)
[![License](https://img.shields.io/github/license/1593959/XMclaw?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue?style=flat-square)]()

**Local-first, self-evolving AI Agent runtime.**

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

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           XMclaw Architecture                           │
└─────────────────────────────────────────────────────────────────────────┘

                              ┌─────────────┐
                              │   User      │
                              │  Interface  │
                              └──────┬──────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
              ▼                      ▼                      ▼
     ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
     │   Desktop   │        │    Web     │        │     CLI     │
     │   (PySide6) │        │   Browser  │        │  (Rich)     │
     └──────┬──────┘        └──────┬──────┘        └──────┬──────┘
            │                      │                      │
            └──────────────────────┼──────────────────────┘
                                   │
                            WebSocket Gateway
                                   │
                    ┌───────────────┴───────────────┐
                    │                               │
                    ▼                               ▼
           ┌─────────────────┐             ┌─────────────────┐
           │   HTTP/REST     │             │  WebSocket/WS   │
           │   (config, mcp) │             │  (streaming)    │
           └─────────────────┘             └────────┬────────┘
                                                   │
                                                   ▼
                    ┌────────────────────────────────────────────────┐
                    │              AgentLoop                        │
                    │  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
                    │  │  Prompt  │→ │   LLM    │→ │  Tools   │  │
                    │  │ Builder  │  │  Router  │  │ Registry │  │
                    │  └──────────┘  └──────────┘  └────┬─────┘  │
                    │       ↑              │            │        │
                    │       └──────────────┴────────────┘        │
                    │              Think → Act → Observe          │
                    └─────────────────────┬──────────────────────┘
                                          │
           ┌──────────────────────────────┼──────────────────────────────┐
           │                              │                              │
           ▼                              ▼                              ▼
  ┌─────────────────┐          ┌─────────────────┐          ┌─────────────────┐
  │     Memory       │          │    Evolution     │          │    Sandbox      │
  │  ┌───────────┐  │          │  ┌───────────┐  │          │  ┌───────────┐  │
  │  │  Session  │  │          │  │   Gene    │  │          │  │  Docker   │  │
  │  │   Logs    │  │          │  │   Forge   │  │          │  │  (secure) │  │
  │  ├───────────┤  │          │  ├───────────┤  │          │  ├───────────┤  │
  │  │   SQLite  │  │          │  │   Skill   │  │          │  │  Git Roll │  │
  │  │ (metadata)│  │          │  │   Forge   │  │          │  │   Back    │  │
  │  ├───────────┤  │          │  ├───────────┤  │          │  ├───────────┤  │
  │  │  Vector   │  │          │  │ Validator │  │          │  │  Code     │  │
  │  │ (sqlite-  │  │          │  │  + VFM    │  │          │  │  Quality  │  │
  │  │   vec)    │  │          │  │           │  │          │  │  (ruff)   │  │
  │  └───────────┘  │          │  └───────────┘  │          │  └───────────┘  │
  └─────────────────┘          └─────────────────┘          └─────────────────┘
                                   ▲                              ▲
                                   │         ┌──────────┐       │
                                   └────────►│ Reflection│◄──────┘
                                             │  Engine  │
                                             └──────────┘
```

### Data Flow

```
1. User sends message via WebSocket
         ↓
2. AgentLoop assembles context + injects matched Genes
         ↓
3. LLM processes + streams response
         ↓
4. ToolRegistry executes tool calls (with Sandbox protection)
         ↓
5. Loop continues until no more tools
         ↓
6. ReflectionEngine reviews + saves lessons
         ↓
7. EvolutionEngine generates new Genes/Skills (periodic)
```

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

### Security & Safety
- [x] **Docker sandbox** — Isolated execution environment for untrusted code
- [x] **Git rollback** — Auto-commit before changes, rollback on critical failures
- [x] **Code quality gates** — ruff linting before commit
- [x] **Dangerous pattern guards** — Blocks destructive bash commands

### Autonomous evolution
- [x] **Pattern detection** — Intent and trend analysis from conversation logs
- [x] **Insight extraction** — Structured lessons and improvement opportunities
- [x] **GeneForge** — Auto-generate behavioral Genes with prompt injection
- [x] **SkillForge** — Auto-generate executable Skill tools
- [x] **EvolutionValidator** — Real execution validation (compile + import + instantiate + run)
- [x] **VFM scoring** — Value Function Model decides whether to solidify
