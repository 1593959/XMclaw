---
summary: "Native PySide6 desktop app usage guide"
read_when:
- Launching or using the desktop app
- Understanding the 6 main views
- Troubleshooting connection or UI issues
title: "Desktop App"
---

# Desktop App

XMclaw's desktop app is a native **PySide6** application. It is not a web page in a frame — it is a real desktop application with system tray support.

---

## Launch

```bash
python -m xmclaw.desktop.app
```

On startup, the app:
1. Checks whether the Daemon is running; starts it if not.
2. Opens a WebSocket connection to `ws://127.0.0.1:8765/agent/default`.
3. Loads agent data: todos, tasks, workspace files, evolution state, and settings.

---

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│  XMclaw              ● connected                             │
├───────┬─────────────────────────────────────────────────────┤
│       │  Dashboard                                         │
│  Nav  │  ┌────────────────────────┬─────────────────────┐  │
│       │  │                        │   Agent State       │  │
│  ─────│  │      Chat area          │   Current thought   │  │
│  Dash │  │                        │   Active tool       │  │
│  WS   │  │                        │   File operation    │  │
│  Evo  │  │                        ├─────────────────────┤  │
│  Mem  │  │                        │   Todos             │  │
│  Logs │  │                        │   Tasks             │  │
│  Comp │  │                        ├─────────────────────┤  │
│  Set  │  │                        │   File operation    │  │
│       │  └────────────────────────┴─────────────────────┘  │
└───────┴─────────────────────────────────────────────────────┘
```

---

## Views

### Dashboard
- **Chat area**: Streaming conversation with user and agent bubbles.
- **Plan mode**: Toggle the "Plan" button to make the agent think before acting.
- **Right panel**: Live agent state, current thought, active tool, file operation, todos, and tasks.

#### Plan mode
1. Click the **Plan** button (it highlights when active).
2. Type a complex request.
3. The agent generates a step-by-step plan and pauses.
4. Reply with confirmation or edits.
5. The agent executes the approved plan.

#### ask_user popup
When the agent needs confirmation, a modal dialog appears:
- Enter your response and click **OK** to proceed.
- Click **Cancel** to abort; the agent receives a cancellation message.

### Workspace
- **File tree**: Browse the agent's working directory.
- **Editor**: Click a file to edit it inline.
- **Save**: Persist changes back to disk.
- **Import**: Copy external files into the workspace.
- **Git toolbar**:
  - `Git Status` — run `git status`
  - `Git Pull` — run `git pull`
  - `Git Commit` — enter a message and commit
  - `Git Push` — run `git push`

### Evolution
Inspect the agent's self-improvement output:
- **Genes**: Active behavioral Genes.
- **Skills**: Auto-generated Skills.
- **Insights**: Extracted lessons and patterns.

Click **Refresh** to update the lists.

### Memory
- **Search box**: Enter keywords to search long-term memory.
- **Results**: Matching file paths and text snippets.

Searched sources:
- `MEMORY.md`
- `PROFILE.md`
- Session logs (`.jsonl`)
- Any other `.md` files in the agent directory

### Tool Logs
- A running list of every tool call the agent has executed.
- Shows tool name, arguments, and result summary.
- **Test panel**: Generate tests for a target file, run a specific test file, or run the full suite (`pytest tests/`).
- Click **Clear logs** to reset the view.

### Computer Use
Remote control your computer directly from the desktop app:
- **Screenshot**: Capture the full desktop and display it in the app.
- **Click / Move**: Set X/Y coordinates and click or move the mouse.
- **Type / Keypress**: Send text or key combinations (e.g., `Ctrl+C`).
- **Scroll**: Scroll the mouse wheel at a given position.
- **Drag**: Drag from start coordinates to end coordinates.

> Safety: `pyautogui.FAILSAFE` is enabled. Move the mouse to a screen corner to abort any ongoing action.

### Settings
| Setting | Description |
|---------|-------------|
| Default LLM provider | `anthropic` or `openai` |
| Model name | e.g. `claude-3-5-sonnet-20241022` |
| API Key | Provider API key |
| Base URL | Custom endpoint (optional) |
| Enable evolution | Toggle autonomous evolution |
| Evolution interval | Minimum minutes between cycles |

Click **Save settings** to persist. Some changes require a Daemon restart.

---

## System tray

- Closing the window minimizes to the system tray instead of quitting.
- Double-click the tray icon to restore the window.
- Right-click the tray icon for **Show** and **Quit** options.

---

## Shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` (in input) | Send message |
| `Ctrl + Enter` | Insert newline in input |

---

## Related

- [CLI](./CLI.md) — Terminal alternative to the desktop app
- [Architecture](./ARCHITECTURE.md) — How the desktop client connects to the Daemon
