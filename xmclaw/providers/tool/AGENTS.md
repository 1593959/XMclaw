# AGENTS.md — `xmclaw/providers/tool/`

## 1. 职责

Tool-call execution backends. `base.py` defines `ToolProvider` ABC;
concrete providers handle the built-in tools (`builtin.py`), remote
MCP servers (`mcp_bridge.py`), Playwright browser automation
(`browser.py`), language-server queries (`lsp.py`), and the
composite that unions them (`composite.py`).

The daemon factory assembles a composite from whichever providers
the user enabled in config.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.ir.*` (tool-call IR),
  `xmclaw.security.*` (redaction on results), `xmclaw.utils.*`,
  stdlib, `playwright`, `mcp`, third-party LSP clients.
- ❌ MUST NOT import: `providers/llm/*`, `providers/memory/*`,
  `providers/runtime/*`, `providers/channel/*`,
  `xmclaw.daemon.*`. If two tool providers legitimately need a
  shared helper, lift it into `core/` or `utils/`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_builtin_tools.py`,
  `test_v2_browser_tools.py`, `test_v2_composite_tool.py`,
  `test_v2_lsp_tools.py`, `test_v2_todo_tools.py`.
- Integration: `tests/integration/test_v2_mcp_bridge.py`,
  `test_v2_tool_loop.py`.
- Smart-gate lane: `tools`.
- Manual smoke: `xmclaw chat` → ask the agent to run `pwd` or
  `list files`; watch `events.log` for `tool_call` + `tool_result`.

## 4. 禁止事项

- ❌ Don't return raw tool output to the LLM without passing
  through `security.prompt_scanner.scan_text`. The AgentLoop does
  this, but a new tool provider must not bypass the pipe.
- ❌ Don't shell out through `os.system`. Use `subprocess.run`
  with `shell=False` + an explicit argv list; that's the only
  form the security audit accepts.
- ❌ Don't catch exceptions from the underlying tool and return
  success. Surface them as `ToolResult(error=…)` so the AgentLoop
  can reflect on the failure.
- ❌ Don't cache tool results across turns without an explicit
  `cache_key`. Memoization at this layer hides idempotency bugs in
  downstream skills.

## 5. 关键文件

- `base.py` — `ToolProvider`, `Tool`, `ToolResult` types.
- `composite.py` — the merger; understand this before wiring a
  new provider into the factory.
- `builtin.py` — pwd / ls / cat / write / todo family.
- `mcp_bridge.py` — remote MCP server client (subprocess +
  JSON-RPC).
- `browser.py`, `lsp.py` — optional backends; require extras
  (`playwright install chromium` / LSP server binary).
