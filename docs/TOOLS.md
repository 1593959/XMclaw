---
summary: "Tool providers: builtin / browser / lsp / mcp / composite + Tool-Call IR"
read_when:
- Adding or debugging a tool
- Writing a new ToolProvider (MCP server, custom runtime, etc.)
- Understanding how the AgentLoop invokes tools
title: "Tools"
---

# Tools

v2 的工具系统是 **ToolProvider 组合** 架构：每个 provider 是一个
独立的 ABC 实现，`CompositeToolProvider` 按配置把它们合并起来提供给
`AgentLoop`。模型通过 provider 原生的 tool-use API（Anthropic
`tool_use` blocks / OpenAI `tool_calls`）发起调用——v1 的
`<function>...</function>` XML 格式已下线。

> 设计约束见
> [`xmclaw/providers/tool/AGENTS.md`](../xmclaw/providers/tool/AGENTS.md)，
> anti-req #1 / #3 / #4 的落地见
> [`xmclaw/core/ir/toolcall.py`](../xmclaw/core/ir/toolcall.py)。

---

## 1. 接口与 IR

```python
# xmclaw/providers/tool/base.py
class ToolProvider(abc.ABC):
    @abc.abstractmethod
    def list_tools(self) -> list[ToolSpec]: ...

    @abc.abstractmethod
    async def invoke(self, call: ToolCall) -> ToolResult: ...
```

`ToolSpec` / `ToolCall` / `ToolResult` 都是 frozen dataclass，定义在
[`xmclaw/core/ir/toolcall.py`](../xmclaw/core/ir/toolcall.py)：

| 字段                    | 类型                          | 说明                                                                 |
| ----------------------- | ----------------------------- | -------------------------------------------------------------------- |
| `ToolSpec.name`         | `str`                         | 给模型看的工具名（唯一；composite 拒收冲突注册）                     |
| `ToolSpec.description`  | `str`                         | 给模型看的功能描述                                                   |
| `ToolSpec.parameters_schema` | JSON Schema              | 参数校验 schema                                                      |
| `ToolCall.name`         | `str`                         | 翻译器解码产出；**不是** 从自由文本正则出来的                         |
| `ToolCall.args`         | `dict[str, Any]`              | 参数                                                                 |
| `ToolCall.provenance`   | `Provenance`                  | `anthropic` / `openai` / `json_mode` / `synthetic`                   |
| `ToolResult.ok`         | `bool`                        | 是否成功                                                             |
| `ToolResult.content`    | `Any`                         | 工具返回值                                                           |
| `ToolResult.error`      | `str \| None`                 | 失败时的错误信息                                                     |
| `ToolResult.side_effects` | `tuple[str, ...]`           | 工具物化写入的路径/URI——Honest Grader 用来做副作用校验（anti-req #4）|
| `ToolResult.latency_ms` | `float`                       | 执行延迟                                                             |

---

## 2. 内置 provider 与工具

所有 built-in provider 在 `xmclaw/providers/tool/` 下。`CompositeToolProvider`
按 daemon factory 的装配顺序合并它们，重名冲突抛 `ValueError`。

### `builtin.py` — 文件 / 进程 / web / todo

| 工具         | 作用                                           |
| ------------ | ---------------------------------------------- |
| `file_read`  | 读文件内容（受 `tools.allowed_dirs` 限制）     |
| `file_write` | 写/覆盖文件                                    |
| `list_dir`   | 列目录                                         |
| `bash`       | 跑 shell（`shell=False` + argv；WIndows 走 PowerShell 别名） |
| `web_fetch`  | HTTP GET 并返回文本                            |
| `web_search` | 关键词搜索（适配器：DuckDuckGo 默认）          |
| `todo_write` | 覆写当前 session 的 todos；发 `todo_updated` 事件 |
| `todo_read`  | 读当前 session 的 todos                         |

`todo_write` / `todo_read` 是状态性工具——AgentLoop 在 invoke 前会把
`session_id` 填到 `ToolCall`，provider 按 session 维护内存中的 todo
列表（不落盘，走 `todo_updated` 事件给 UI 做实时渲染）。

### `browser.py` — Playwright 浏览器自动化

| 工具                 | 作用                                |
| -------------------- | ----------------------------------- |
| `browser_open`       | 打开 URL                            |
| `browser_click`      | 点击元素（CSS selector）            |
| `browser_fill`       | 填表单字段                          |
| `browser_screenshot` | 截屏到文件                          |
| `browser_snapshot`   | 结构化 DOM snapshot                 |
| `browser_eval`       | 页面上下文里执行 JS（调试用）       |
| `browser_close`      | 关当前 context                      |

用前需要 `playwright install chromium`。未装时 provider 仍可 import，
但 `invoke` 会返回 `ToolResult(ok=False, error=...)`。

### `lsp.py` — Language Server

| 工具             | 作用                                     |
| ---------------- | ---------------------------------------- |
| `lsp_hover`      | 某位置的 hover 信息（docstring / 类型）  |
| `lsp_definition` | 跳定义                                   |

需要本地 LSP server 可执行（pyright / rust-analyzer / tsserver）。

### `mcp_bridge.py` — 远程 MCP server

通过 subprocess + JSON-RPC 把一个或多个 MCP server 的工具集
展开到 composite 里。`config.json`：

```json
{
  "mcp_servers": {
    "my-server": {
      "transport": "stdio",
      "command": "my-mcp-bin",
      "args": []
    }
  }
}
```

支持的 `transport`：`stdio`（默认，subprocess）/ `sse` / `ws`。远程
工具 spec 在 bridge 启动时从 server 拉取一次，之后透传 `invoke`。

### `composite.py` — 合并器

daemon factory 的装配顺序决定工具名可见顺序；两个 provider 同名
工具会在构造时 fail-fast。这是新接一个 provider 之前必读的文件
（测试入口：`tests/unit/test_v2_composite_tool.py`）。

---

## 3. 调用链路

AgentLoop 跑完一轮 LLM 后：

1. 从 `LLMResponse.tool_calls`（provider 翻译器已解码成结构化
   `ToolCall`）遍历每个 call。
2. 发 `TOOL_CALL_EMITTED` 事件。
3. 如果是有 session 状态的工具（`todo_write` / `todo_read`），填
   `call.session_id`。
4. 发 `TOOL_INVOCATION_STARTED`。
5. `await tool_provider.invoke(call)` —— composite 路由到具体
   provider。
6. 发 `TOOL_INVOCATION_FINISHED`，payload 含 `latency_ms` + `error`
   + `side_effects`。
7. 结果被 `security.prompt_scanner.scan_text` 扫过（防提示注入）
   后喂回 LLM 消息历史。

完整事件 schema 见 [docs/EVENTS.md](EVENTS.md#eventtype-全集)。

---

## 4. 写一个 ToolProvider

最少骨架：

```python
from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class EchoProvider(ToolProvider):
    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="echo",
            description="Return the input text unchanged.",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name != "echo":
            return ToolResult(call_id=call.id, ok=False,
                              content=None, error=f"unknown tool: {call.name}")
        return ToolResult(
            call_id=call.id, ok=True,
            content=call.args.get("text", ""),
            side_effects=(),  # read-only
        )
```

把 provider 挂到 composite 的方式取决于你想长期持有还是临时注入：
- 长期：改 `xmclaw/daemon/factory.py`，在 `build_tool_provider` 里
  按 config 条件 append。
- 临时（测试 / 单机脚本）：直接 `CompositeToolProvider([EchoProvider(), ...])`。

### 强制项（AGENTS.md §4）

- ❌ **不得**把原始工具输出直接返给 LLM——必须经
  `security.prompt_scanner.scan_text`（AgentLoop 已经在主路径上做；
  新 provider 不要绕过）。
- ❌ **不得**用 `os.system`/`shell=True`——用 `subprocess.run(argv,
  shell=False)`。
- ❌ **不得**吞异常然后返 `ok=True`——错误必须 surface 成
  `ToolResult(error=...)`，`ok=False`。
- ❌ **不得**跨 turn 缓存结果（没有 explicit `cache_key`）。
- ✅ 跟 composite 的唯一协议是 `list_tools() + async invoke()`——保
  持纯函数尽可能容易。

### 测试入口

| 路径                                                                                                             | 覆盖                |
| ---------------------------------------------------------------------------------------------------------------- | ------------------- |
| [`tests/unit/test_v2_builtin_tools.py`](../tests/unit/test_v2_builtin_tools.py)                                 | builtin 工具正确性  |
| [`tests/unit/test_v2_browser_tools.py`](../tests/unit/test_v2_browser_tools.py)                                 | Playwright 工具     |
| [`tests/unit/test_v2_lsp_tools.py`](../tests/unit/test_v2_lsp_tools.py)                                         | LSP 工具            |
| [`tests/unit/test_v2_composite_tool.py`](../tests/unit/test_v2_composite_tool.py)                               | composite 合并 + 冲突 |
| [`tests/unit/test_v2_todo_tools.py`](../tests/unit/test_v2_todo_tools.py)                                       | todo 状态 + 事件    |
| [`tests/integration/test_v2_mcp_bridge.py`](../tests/integration/test_v2_mcp_bridge.py)                         | MCP bridge 协议     |
| [`tests/integration/test_v2_tool_loop.py`](../tests/integration/test_v2_tool_loop.py)                           | AgentLoop 端到端    |

都归属 `tools` smart-gate lane（见
[`scripts/test_lanes.yaml`](../scripts/test_lanes.yaml)）。新 provider
上线时把它的测试文件加到 `tools` lane 下。

---

## 5. 安全模型

| 层             | 防线                                                                                                                 |
| -------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Bash**       | `shell=False` + 明确 argv；没有 shell 元字符 parsing；`tools.allowed_dirs` 限制 `cwd`                               |
| **File I/O**   | `allowed_dirs` 作用到 `file_read` / `file_write` / `list_dir` 的路径参数；尝试穿越 → `ToolResult(ok=False)`         |
| **Browser**    | 默认 headless；CDP / remote debugging **默认关**（不允许运行时通过 tool 参数开）                                     |
| **MCP**        | 每个远程 server 独立 subprocess，stdin/stdout 只走 JSON-RPC，不继承环境变量除非 config 声明                         |
| **Prompt-injection** | 所有 `ToolResult.content` 在进 LLM 消息前过 `security.prompt_scanner`，检测到 → emit `PROMPT_INJECTION_DETECTED` |
| **Redaction**  | `ToolResult` 结构 log 时，`api_key` / `token` / `password` 字段走 `utils.redact`                                    |

---

## 6. 相关文件

- [`xmclaw/providers/tool/`](../xmclaw/providers/tool/) — 所有 provider 实现
- [`xmclaw/providers/tool/AGENTS.md`](../xmclaw/providers/tool/AGENTS.md) — 依赖规则与禁止项
- [`xmclaw/core/ir/toolcall.py`](../xmclaw/core/ir/toolcall.py) — `ToolSpec` / `ToolCall` / `ToolResult`
- [`xmclaw/providers/llm/translators/`](../xmclaw/providers/llm/translators/) — LLM 响应→`ToolCall` 翻译器
- [docs/EVENTS.md](EVENTS.md) — 工具相关事件 payload
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — 组件拓扑
- [docs/V2_DEVELOPMENT.md](V2_DEVELOPMENT.md) §1 / §6 — anti-req #1/#3/#4 在 IR 层的落地
