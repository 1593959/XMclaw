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
# Start the Daemon
xmclaw start

# Stop the Daemon
xmclaw stop

# Check Daemon status
xmclaw status
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
# Search memory files
xmclaw memory-search "database config"

# Search a specific agent's memory
xmclaw memory-search "project progress" --agent default
```

### Configuration

```bash
# Display current agent config
xmclaw config-show
```

### Testing

```bash
# Run the full test suite
xmclaw test --action run_all

# Generate tests for a specific module
xmclaw test --action generate --target xmclaw/tools/bash.py

# Run a specific test file
xmclaw test --action run --target tests/test_bash.py
```

### Computer Use

```bash
# Take a screenshot
xmclaw computer-use screenshot

# Click at coordinates
xmclaw computer-use click --x 500 --y 300

# Type text
xmclaw computer-use type --text "hello world"

# Press a key combo
xmclaw computer-use keypress --key ctrl+c

# Scroll
xmclaw computer-use scroll --x 500 --y 300 --scroll-y -200

# Drag
xmclaw computer-use drag --x 100 --y 100 --end-x 300 --end-y 300
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
```

---

## Related

- [Desktop](./DESKTOP.md) — GUI alternative to the CLI
- [Architecture](./ARCHITECTURE.md) — WebSocket protocol and event types
