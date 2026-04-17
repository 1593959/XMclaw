# XMclaw

> 🤖 A local-first, self-evolving AI Agent runtime that runs entirely on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue.svg)]()

**XMclaw is not a chatbot.** It's a runtime that can think, act, remember, reflect, and continuously improve itself.

---

## ✨ Features

| Category | Features |
|----------|----------|
| **🧠 Core Runtime** | AgentLoop, Orchestrator, LLMRouter, CostTracker |
| **💾 Memory** | Session logs, SQLite metadata, Vector memory (sqlite-vec) |
| **🔧 Tools** | 20+ built-in tools: file, bash, browser, git, mcp, test... |
| **🔄 Self-Evolving** | GeneForge, SkillForge, VFM scoring, Hot reload |
| **🛡️ Security** | Docker sandbox, Git rollback, Code quality gates |
| **🖥️ Interfaces** | Desktop app (PySide6), Web UI, Rich CLI |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     XMclaw Architecture                      │
└─────────────────────────────────────────────────────────────┘

                     ┌─────────────────┐
                     │  User Interface  │
                     │  Desktop/Web/CLI │
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │   Gateway        │
                     │  WebSocket/HTTP  │
                     └────────┬────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ▼               ▼               ▼
        ┌───────────┐   ┌───────────┐   ┌───────────┐
        │  Memory   │   │ Evolution │   │  Sandbox  │
        │  Session  │   │ GeneForge │   │  Docker   │
        │  SQLite   │   │ SkillForge│   │  Git Roll│
        │  Vector   │   │ Validator │   │  ruff     │
        └───────────┘   └───────────┘   └───────────┘
                              ▲
                              │
              ┌───────────────▼───────────────┐
              │         AgentLoop             │
              │  Prompt → LLM → Tools → Reflect│
              └───────────────────────────────┘
```

---

## 🚀 Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/1593959/XMclaw.git
cd XMclaw

# Install dependencies
pip install -e .

# Optional extras
pip install pyautogui mss Pillow     # computer_use support
pip install mcp                       # MCP integration
pip install playwright && playwright install chromium  # browser automation
```

### Configuration

Edit `agents/default/agent.json`:

```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4",
    "api_key": "your-api-key"
  }
}
```

### Run

```bash
# Start the daemon
xmclaw start

# Open desktop app
python -m xmclaw.desktop.app

# Or use CLI
xmclaw chat
xmclaw chat --plan   # plan mode

# Stop daemon
xmclaw stop
```

---

## 📁 Project Structure

```
XMclaw/
├── xmclaw/              # Core runtime
│   ├── core/            # AgentLoop, Orchestrator, PromptBuilder
│   ├── daemon/          # FastAPI server, WebSocket gateway
│   ├── desktop/         # PySide6 desktop app
│   ├── evolution/       # GeneForge, SkillForge, Validator
│   ├── gateway/         # HTTP/WebSocket handlers
│   ├── genes/           # Gene implementations
│   ├── llm/             # LLM routers (OpenAI, Anthropic)
│   ├── memory/           # Memory managers
│   ├── sandbox/          # Docker isolation
│   ├── tools/            # Tool registry & implementations
│   └── utils/            # Utilities
├── web/                 # Web UI assets
├── shared/              # Shared resources (genes, skills)
├── agents/              # Agent configurations
├── docs/                # Documentation
├── scripts/              # Build & utility scripts
└── tests/               # Test suites
```

---

## 🛡️ Security

XMclaw implements multiple security layers:

- **Docker Sandbox** - Execute untrusted code in isolation
- **Git Rollback** - Auto-commit before changes, rollback on failure
- **Code Quality Gates** - ruff linting enforced before commits
- **Dangerous Pattern Guards** - Blocks destructive bash commands

---

## 🔄 Evolution System

XMclaw continuously improves itself:

1. **Pattern Detection** - Analyzes conversation logs for patterns
2. **Insight Extraction** - Generates structured lessons
3. **Gene Generation** - Creates behavioral prompts (GeneForge)
4. **Skill Generation** - Builds executable tools (SkillForge)
5. **Validation** - Real execution testing + VFM scoring
6. **Hot Reload** - New skills available immediately

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design & data flow |
| [TOOLS.md](docs/TOOLS.md) | Built-in tools reference |
| [EVOLUTION.md](docs/EVOLUTION.md) | Self-evolution system |
| [DESKTOP.md](docs/DESKTOP.md) | Desktop app guide |
| [CLI.md](docs/CLI.md) | Terminal commands |

---

## 🧪 Development

```bash
# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=xmclaw --cov-report=html

# Lint
ruff check xmclaw/

# Build distribution
python -m build
```

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with ❤️ for developers who want a truly personal AI agent.**
