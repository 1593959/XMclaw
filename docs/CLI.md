---
summary: "Terminal interface commands and chat protocol"
read_when:
- Using the CLI for the first time
- Looking up available commands
- Understanding how CLI renders streaming events
title: "CLI"
---

# CLI

XMclaw's CLI is built with **Typer** (commands) and **Rich** (rendering). It provides full access to the agent runtime from the terminal.

---

## Entry point

After installing the project, the `xmclaw` command is available:

```bash
xmclaw --help
```

You can also run it as a module:

```bash
python -m xmclaw.cli.main --help
```

---

## Commands

### Daemon management

```bash
# Start the Daemon (first run triggers setup wizard if no config)
xmclaw start

# Stop the Daemon
xmclaw stop

# Check Daemon status
xmclaw status

# Diagnose setup issues (API keys, ports, dependencies)
xmclaw doctor
```

### Chat

```bash
# Start an interactive chat session
xmclaw chat

# Chat with a specific agent
xmclaw chat --agent myagent

# Start in plan mode
xmclaw chat --plan
```

In chat mode:
- Type normally to send messages.
- Type `/quit` or `/exit` to leave.
- When the agent asks a question via `ask_user`, just type your answer.

### Tasks

```bash
# List all tasks
xmclaw task-list

# Create a new task
xmclaw task-create "Implement auth" --description "Add JWT login and middleware"
```

### Evolution

```bash
# Show Gene and Skill counts
xmclaw evolution-status
```

### Memory

```bash
# Search agent memory files
xmclaw memory-search "database config"

# Search a specific agent's memory
xmclaw memory-search "project progress" --agent default
```

### Configuration

```bash
# Show current daemon config (secrets are masked as ***)
xmclaw config show

# Set a config value — supports dot-notation for nested keys
xmclaw config set evolution.interval_minutes 15
xmclaw config set evolution.enabled false
xmclaw config set llm.default_provider openai
xmclaw config set tools.browser_headless true

# Reset a key to its default value
xmclaw config reset evolution.interval_minutes

# Reset entire config to defaults
xmclaw config reset all

# Run the interactive first-run setup wizard
xmclaw config init
```

#### Environment variable overrides

Config values can be overridden via environment variables (useful for containers/CI):

```bash
# Format: XMC__{section}__{nested_key}
export XMC__llm__anthropic__api_key="sk-ant-..."
export XMC__llm__openai__api_key="sk-..."
export XMC__evolution__enabled="false"
export XMC__gateway__port="8765"
```

Environment variables take precedence over `daemon/config.json`.

### Testing

```bash
# Run all tests
xmclaw test

# Run tests for a specific module
xmclaw test --module bash
```

### Utilities

```bash
# Print shell completion setup instructions
xmclaw completion

# Print environment variable override reference
xmclaw config env

# Diagnose common setup issues
xmclaw doctor

# View recent event bus activity
xmclaw events --limit 50
xmclaw events --type tool:called
```

---

## Event rendering

The CLI receives the full WebSocket event stream. Each event type is rendered differently:

| Event type | CLI output |
|------------|------------|
| `chunk` | Streamed text (no prefix) |
| `state` | Dim italic line: `State: THINKING \| Analyzing request...` |
| `tool_result` | Yellow-bordered panel with tool name and result snippet |
| `ask_user` | Magenta-bordered panel asking for confirmation |
| `reflection` | Cyan-bordered panel with summary, problems, lessons, and improvements |
| `error` | Red error text |

---

## Plan mode example

```bash
$ xmclaw chat --plan
You: Refactor the user module to add caching and logging
[State: PLANNING | Plan mode enabled, constructing execution plan...]
[Agent outputs a multi-step plan...]
[ask_user] XMclaw asks: Execute the above plan?
You: Yes, but skip step 3 for now
[Agent begins execution...]
[tool_result] file_edit -> Updated user.py
[tool_result] bash -> pytest passed
[reflection] Task completed successfully
[done]
```

---

## Development debugging

```bash
# Run commands directly via Python module
python -m xmclaw.cli.main start
python -m xmclaw.cli.main chat --plan
python -m xmclaw.cli.main evolution-status
python -m xmclaw.cli.main config show
python -m xmclaw.cli.main doctor
```

---

## Related

- [Desktop](./DESKTOP.md) — GUI alternative to the CLI
- [Architecture](./ARCHITECTURE.md) — WebSocket protocol and event types
