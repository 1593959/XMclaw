---
summary: "Tool registry, built-in tools, skill generation, and security model"
read_when:
- Adding or debugging tools
- Understanding skill generation and hot reload
- Reviewing security boundaries
title: "Tools"
---

# Tools

XMclaw 的工具系统采用 **注册表 + 热重载** 架构。所有工具都继承自 `Tool` 基类，通过 `ToolRegistry` 统一管理。

---

## Built-in tools

### File operations

| Tool | Purpose | Example use |
|------|---------|-------------|
| `file_read` | Read file contents | Inspect code, configs, logs |
| `file_write` | Write or overwrite a file | Generate new files |
| `file_edit` | Partial text replacement | Surgical code edits |
| `glob` | Pattern-based file search | `**/*.py` |
| `grep` | Content-based file search | Find function definitions |

### System and network

| Tool | Purpose | Example use |
|------|---------|-------------|
| `bash` | Execute shell commands | Run scripts, install deps |
| `browser` | Browser automation | Open pages, click, screenshot |
| `web_search` | Web search | Look up docs, news |
| `web_fetch` | Fetch page content | Retrieve API docs |

### Tasks and collaboration

| Tool | Purpose | Example use |
|------|---------|-------------|
| `todo` | Todo list management | Add, complete, list todos |
| `task` | Task tracking | Create long-running tasks |
| `ask_user` | Human confirmation | Pause for approval on risky ops |
| `agent` | Spawn sub-agents | Delegate focused sub-tasks |
| `skill` | Dynamic skill loading | Invoke generated skills |

### Memory

| Tool | Purpose | Example use |
|------|---------|-------------|
| `memory_search` | Vector memory search | Retrieve past experiences |

### Development and version control

| Tool | Purpose | Example use |
|------|---------|-------------|
| `git` | Git operations | Commit, push, pull, status |
| `computer_use` | Desktop control | Screenshot, click, type, keypress |
| `test` | Test generation and execution | Auto-generate pytest suites |
| `mcp` | MCP server calls | Connect to external tool ecosystems |

---

## Tool call format

The model emits tool calls in this XML-like format:

```xml
<function>tool_name</function>
<arguments>
{
  "param1": "value1",
  "param2": "value2"
}
</arguments>
```

`AgentLoop` parses these blocks and dispatches them to `ToolRegistry.execute()`.

---

## Auto-generated skills

XMclaw's evolution engine can generate new skills from conversation patterns. Generated skills are saved as Python files in `shared/skills/skill_*.py`.

`ToolRegistry` scans this directory on startup and hot-reloads new skills without requiring a Daemon restart.

### Skill file structure

```python
from xmclaw.tools.base import Tool

class MyGeneratedSkill(Tool):
    name = "my_skill"
    description = "Does something useful"
    parameters = {
        "input": {"type": "string", "description": "Input text"}
    }

    async def execute(self, input: str) -> str:
        return f"Result: {input}"
```

### Skill versioning

- Unlimited versions are allowed; each iteration improves the latest version.
- Old versions are automatically cleaned up, keeping only the 2 most recent.

---

## Security model

### BashTool guards

- **Dangerous commands blocked**: `rm -rf /`, `mkfs`, `dd`, etc.
- **Suspicious commands warned**: `git push --force`, `curl | bash`, etc.
- **Common dev commands allowed**: `python`, `git`, `pip`, `pytest`, etc.

### ComputerUse guards

- `pyautogui.FAILSAFE` is enabled by default (move mouse to a corner to abort).
- All coordinate actions require explicit `x` and `y` values.

### Browser guards

- Runs headless by default.
- CDP (remote debugging) is never enabled unless explicitly requested by the user.

---

## Related

- [Architecture](./ARCHITECTURE.md) — How the tool registry fits into the agent loop
- [Evolution](./EVOLUTION.md) — How skills are generated and validated
