# XMclaw

A local-first, self-evolving AI Agent runtime.

## Features

- **Local-first**: All data stays on your machine
- **Dual LLM support**: OpenAI-compatible + Anthropic Claude
- **Real-time streaming**: WebSocket-based interactive chat
- **Extensible tools**: File operations, bash, browser, web search, memory
- **Self-evolving**: Built-in framework for Gene/Skill evolution
- **Rich CLI**: Beautiful terminal interface with `rich`

## Quick Start

```bash
# Install dependencies
pip install -e .

# Start the daemon
xmclaw start

# Chat with the agent
xmclaw chat

# Stop the daemon
xmclaw stop
```

## Architecture

```
xmclaw/
├── daemon/        # FastAPI + WebSocket server
├── gateway/       # Connection abstractions
├── core/          # Agent loop, orchestrator, prompts
├── llm/           # OpenAI & Anthropic clients
├── tools/         # Built-in tool implementations
├── memory/        # SQLite + JSONL + ChromaDB
└── cli/           # Terminal interface
```

## Configuration

Edit `daemon/config.json` to set your LLM API keys.

## License

MIT
