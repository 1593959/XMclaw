# XMclaw 工具使用（Tool Use）能力深度调研报告

> 调研范围：XMclaw 主仓库 `main` 分支，commit `3ea2df25` 附近  
> 调研日期：2026-06-24  
> 调研者：代码调研员

---

## 一、概述

XMclaw 的 Tool Use 能力是一套**多层抽象、高度模块化**的架构，核心设计理念包括：

1. **Provider-agnostic IR**：所有工具调用均通过 `ToolCall`/`ToolSpec`/`ToolResult` 三个数据类在不同层之间传递，与具体 LLM 提供者（Anthropic/OpenAI 等）的 wire format 解耦。
2. **组合式 ToolProvider 架构**：通过 `CompositeToolProvider` 将多个 `ToolProvider` 实例组合为统一视图。
3. **Hook 系统介入**：在工具调用前后插入 `PreToolUse` / `PostToolUse` 生命周期钩子。
4. **渐进披露（Progressive Disclosure）**：工具列表按相关性过滤 + 描述压缩，控制每轮发送给 LLM 的 token 数量。
5. **结果治理**：工具返回结果经过语义摘要、重复去重、旧结果裁剪三层处理后再进入对话历史。

---

## 二、核心 IR：ToolCall / ToolSpec / ToolResult

**文件**：`xmclaw/core/ir/toolcall.py`（第 1–99 行）

这是整个工具调用链的**类型契约层**。

```python
# xmclaw/core/ir/toolcall.py:30-70
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    read_only: bool = False  # B-7: 读操作可并行

@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    provenance: Provenance
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None

@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    ok: bool
    content: Any
    error: str | None = None
    latency_ms: float = 0.0
    side_effects: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
```

**设计要点**：
- `ToolCall.id` 由 `uuid.uuid4().hex` 生成，用于在消息历史中匹配 `tool` role 的响应消息。
- `ToolResult.side_effects` 是**诚实评分器（Honest Grader）**验证可观测性的依据。
- `metadata` 是 B-295 引入的侧通道，用于 `SkillToolProvider` 传递 `skill_version`。

---

## 三、工具提供者层（ToolProvider Layer）

### 3.1 抽象基类

**文件**：`xmclaw/providers/tool/base.py`（第 1–14 行）

```python
class ToolProvider(abc.ABC):
    @abc.abstractmethod
    def list_tools(self) -> list[ToolSpec]: ...
    @abc.abstractmethod
    async def invoke(self, call: ToolCall) -> ToolResult: ...
```

### 3.2 复合提供者：CompositeToolProvider

**文件**：`xmclaw/providers/tool/composite.py`（第 30–104 行）

- 构造时构建 `name -> child` 路由表，使 `invoke()` 达到 O(1)。
- `invalidate_router()` 支持动态重建路由（第 51–67 行）。
- 支持 `close_session()` 和 `shutdown()` 生命周期钩子。

### 3.3 内置工具：BuiltinTools

**文件**：`xmclaw/providers/tool/builtin.py`（第 244–956 行）

`BuiltinTools` 是一个**多 Mixin 组合类**，包含：
- `BuiltinToolsCanvasMixin`：canvas_create / update / close
- `BuiltinToolsDbMixin`：sqlite_query
- `BuiltinToolsFsMixin`：file_read / write / list_dir / glob / grep / delete
- `BuiltinToolsMemoryMixin`：remember / memory / memory_search
- `BuiltinToolsPersonaMixin`：update_persona / learn_about_user
- `BuiltinToolsPlanModeMixin`：enter_plan_mode / exit_plan_mode
- `BuiltinToolsShellMixin`：bash
- `BuiltinToolsUserMixin`：ask_user_question / todo_write / todo_read
- `BuiltinToolsVoiceMixin`：voice_transcribe / voice_synthesize
- `BuiltinToolsWorktreeMixin`：enter_worktree / exit_worktree

`invoke()` 方法（第 560 行起）是一个巨大的 `if/elif` 分发梯子。

- **Plan Mode 门禁**（第 565 行）：`is_blocked_by_plan_mode(call.name)` 在分发前拦截 mutating 工具。
- **Legacy 兼容**（第 664 行）：`memory_search` 等 10 个旧工具名通过 `_LEGACY_MEMORY_TOOL_MAP` 重写到统一的 `memory(action=...)` 再分发。
- **2026-06-18 统一内存工具**（第 752 行）：`memory` 是单一入口，内部按 `action` 字段二次分发。

### 3.4 技能工具桥接：SkillToolProvider

**文件**：`xmclaw/skills/tool_bridge.py`（第 191–2006 行）

这是**技能（Skill）与 ToolProvider 之间的唯一桥梁**。

#### 命名映射
```python
# xmclaw/skills/tool_bridge.py:121-131
def _to_tool_name(skill_id: str) -> str:
    safe = skill_id.replace(".", "__")
    safe = _VALID_NAME.sub("_", safe)
    return f"skill_{safe}"[:64]  # Anthropic 64 字符限制
```

`demo.read_and_summarize` → `skill_demo__read_and_summarize`

#### 披露模式（Disclosure Mode）

| 模式 | 含义 | 触发条件 |
|------|------|----------|
| `inline` | 每个 skill 独立成一个工具 | 默认 |
| `unified` | 只暴露 meta-tool（browse/run/install 等） | 强制或 auto > 20 skills |
| `auto` | skill 数 > 20 时自动切到 unified | 默认 |

#### Meta 工具（ always exposed ）

| 工具名 | 功能 | 行号 |
|--------|------|------|
| `skill_browse` | 语义发现 skill（token overlap + substring） | 466–637 |
| `skill_install` | 从 GitHub/本地安装 skill，支持 MCP server 自动热加载 | 1636–1826 |
| `skill_uninstall` | 卸载 skill | 1703–1865 |
| `skill_status` | 查看注册表状态、加载失败、待重启 | 641–758 |
| `skill_view` | 读取 skill 目录内文件（SKILL.md / skill.py） | 760–959 |
| `skill_run` | 统一调用入口（unified 模式下唯一调用路径） | 1570–1632 |
| `skill_diff` | 对比 SKILL.md 与历史快照 | 963–1186 |
| `skill_rollback` | 从快照回滚 | 1010–1246 |
| `skill_propose` | Agent 自写 skill（写 SKILL.md + .proposed.json 标记） | 1250–1428 |
| `skill_compose` | 顺序执行多个 skill 工作流 | 1432–1566 |

#### `invoke()` 核心流程（第 325–458 行）

1. Meta 工具短路（不查 registry）
2. `skill_run` 解析 `skill_id` 或从缓存映射回 skill_id（O(1)）
3. B-295: variant selector（UCB1 多臂老虎机）选版本
4. 从 registry 获取 skill 并运行：`await skill.run(SkillInput(...))`
5. 记录使用统计 + 版本元数据到 `ToolResult.metadata`

### 3.5 MCP Hub：多 MCP Server 编排

**文件**：`xmclaw/providers/tool/mcp_hub.py`（第 291–531 行）

`MCPHub` 本身是一个 `ToolProvider`，负责管理多个 MCP server：

- 配置来源：`~/.xmclaw/mcpServers.json`（Claude Desktop 兼容格式）+ `daemon/config.json` 的 `mcp_servers` 字典叠加（第 322–359 行）。
- 安全审计（Wave-29，第 200–244 行）：启动前检查 supply-chain risk、command injection、payload delivery、credential theft。
- 参数注入扫描（第 246–274 行）：在调用前扫描 `call.args` 中的 prompt-injection 模式，对 `untrusted` server 拒绝执行。
- 工具名混淆（第 65–84 行）：`server_id__tool_name` 截断到 64 字符，带 `_xNNN` 哈希后缀防碰撞。

### 3.6 MCP Bridge：单 Server 的 JSON-RPC 2.0 通信

**文件**：`xmclaw/providers/tool/mcp_bridge.py`（第 61–456 行）

`MCPBridge` 通过 `asyncio.create_subprocess_exec` 启动 MCP server 子进程，通过 stdin/stdout 进行 JSON-RPC 2.0 通信：

关键容错：
- `_read_loop()`（第 361 行）持续读取子进程 stdout，通过 `_dispatch()` 将响应匹配到 pending 的 `asyncio.Future`。
- 超时或异常时返回结构化 `ToolResult(ok=False, ...)` 而非抛异常，防止一个 MCP server 崩溃拖垮整个 AgentLoop。
- `_content_to_python()`（第 434 行）将 MCP 的 content blocks 转换为 Python 原生值。

### 3.7 自有的 MCP Server：xmclaw/mcp/

**文件**：`xmclaw/mcp/server.py`（第 1–690 行）

XMclaw 自己**也实现了一个 MCP server**，通过 `xmclaw mcp serve` 启动，直接通过 Python 导入暴露 XMclaw 核心服务（无需 HTTP 绕行）。

该 server 暴露了 15 个工具，分为 5 个类别：

| 类别 | 工具名 | 实现行号 | 依赖 |
|------|--------|---------|------|
| Perception | `screen_capture` | 300 | `mss` + `PIL` |
| Perception | `screen_ocr` | 328 | `rapidocr_onnxruntime` |
| Perception | `clipboard_read` / `clipboard_write` | 341 / 349 | `pyperclip` |
| Computer Use | `computer_click` / `type` / `scroll` | 358 / 370 / 380 | `pyautogui` |
| Computer Use | `window_list` / `window_activate` | 392 / 408 | `pygetwindow` |
| Browser | `browser_open` / `snapshot` / `click` / `fill` | 441 / 447 / 453 / 459 | `playwright` |
| IM Send | `im_send` / `im_list_channels` | 467 / 551 | `lark_oapi` / `requests` / `slack_sdk` |
| Memory v2 | `memory_search` / `memory_add_fact` | 563 / 573 | `MemoryService` |
| Health | `xmclaw_health` | 586 | 动态 `__import__` 检查 |

**JSON-RPC 2.0 实现**（第 282 行起）：纯 Python stdlib，无外部 MCP SDK 依赖。

**Node.js 桥接**：`xmclaw/mcp/_bridge.mjs` / `node-bridge/bridge.mjs`（第 1–17 行）是一个极薄的 Node wrapper，通过 `spawn` 启动 Python server，stdin/stdout 直通。

**Echo server**：`xmclaw/mcp/_echo.py` / `_echo.mjs`（第 1–29 行）用于连通性测试，是最小化 MCP 实现。

### 3.8 Agent 间通信：AgentInterTools

**文件**：`xmclaw/providers/tool/agent_inter.py`（第 329–1159 行）

提供 LLM 驱动的 agent 可以调用的**多 agent 协作工具**：

| 工具名 | 功能 | 行号 |
|--------|------|------|
| `list_agents` | 枚举所有可用 agent | 408–431 |
| `chat_with_agent` | 阻塞式向另一个 agent 发消息并等待回复 | 435–472 |
| `submit_to_agent` | 后台派发任务，返回 task_id | 476–525 |
| `fork_session` | 派生一个继承父会话历史的后台任务 | 607–699 |
| `check_agent_task` | 轮询 task_id 状态 | 807–847 |
| `list_agent_tasks` | 列出最近/在飞任务 | 849–931 |
| `stop_agent_task` | 取消在飞任务 | 933–970 |
| `swarm_dispatch` | 分解复杂目标到并行子任务 | 974–999 |

安全设计：
- **B-273**：`chat_with_agent` 和 `submit_to_agent` 的返回结果均经过 `apply_policy()` 的 prompt-injection 扫描，防止恶意子 agent 注入指令。
- **B-307**：`submit_to_agent` 的异步路径也补上了扫描（早期只有同步路径有）。

### 3.9 过滤提供者：FilteredToolProvider

**文件**：`xmclaw/providers/tool/filtered.py`（第 41–87 行）

B-332 修复：cron 的 `enabled_toolsets` 字段终于真正生效。`FilteredToolProvider` 是一个纯包装器，仅暴露 `allowed_names` 中指定的工具，对不在白名单中的工具返回结构化拒绝。

---

## 四、工具描述压缩：ToolDescriptionCompressor

**文件**：`xmclaw/skills/tool_description_compressor.py`（第 1–190 行）

**背景**：B-238 的 skill prefilter 已将 irrelevant `skill_*` 工具从 ~404 个降到 ~12 个，但非 skill 工具（bash/file_read/web_search 等）始终发送完整描述。30–50 个内置工具 + 12 个 skill 仍可能达到 20–40K tokens。

**三层压缩策略**：

| 相关性 | 处理方式 | 阈值 |
|--------|----------|------|
| Core tools（白名单） | 永不压缩 | — |
| 高相关（overlap >= 2） | 完整描述 | — |
| 中相关（overlap == 1） | 截断到第一句或 120 字符 | `_TRUNCATED_DESC_MAX_CHARS=120` |
| 低相关（overlap == 0） | 单行 + schema 仅 60 字符 | `_MINIMAL_DESC_MAX_CHARS=60` |

**实现细节**（第 136–187 行）：
- `_extract_tokens()`：正则 `[a-zA-Z0-9_]+|[\u4e00-\u9fff]` 提取英文词 + CJK 单字。
- `_STOPWORDS`：包含中英文停用词（第 54–94 行）。
- `_compress_description()`：优先找句末（`. ` / `。` / `\n\n`），找不到则硬截断加 `...`。

---

## 五、上下文引擎中的工具处理

### 5.1 ContextEngine（ABC）

**文件**：`xmclaw/context/engine.py`（第 83–164 行）

定义了 6 阶段生命周期抽象：`bootstrap → ingest → assemble → compact → after_turn → dispose`。

其中与 Tool Use 相关的可选钩子：`on_tool_invoked(session_id, tool_name, result)`。

### 5.2 工具结果裁剪：tool_result_prune.py

**文件**：`xmclaw/context/tool_result_prune.py`（第 1–384 行）

**三 Pass 算法**（第 226–383 行）：

1. **Pass 1 — Dedup（去重）**：从后向前遍历 `role=tool` 的消息，对内容计算 MD5 前 12 字符哈希。相同内容的旧消息替换为 `[Duplicate tool output — same content as a more recent call]`。

2. **Pass 1.5 — Semantic Summarizer**：对 > 3000 字符的工具结果在裁剪区内调用 `summarize_tool_result()`，只有当摘要长度 < 原始长度 80% 时才接受。

3. **Pass 2 — 1-line Summary**：对保护尾区（`protect_tail_tokens=6000`）之外的工具结果，按工具名生成人类可读的单行摘要。例如：`[bash] ran xmclaw stop && xmclaw start -> exit 0, 23 lines output`

4. **Pass 3 — ToolCall Args Truncation**：对 `role=assistant` 消息中的 `tool_calls` 参数进行 JSON-aware 截断。阈值 500 字符，叶子字符串保留 200 字符头 + `[truncated]`。

### 5.3 工具结果语义摘要：tool_result_summarizer.py

**文件**：`xmclaw/context/tool_result_summarizer.py`（第 1–198 行）

提供**零 LLM 延迟**的按工具类型摘要策略：

| 工具类型 | 策略 | 参数 |
|----------|------|------|
| `file_read` | 代码文件保留头 50 行 + 尾 20 行；其他文件保留头 2000 + 尾 500 字符 | `_CONTENT_HEAD_CHARS=2000`, `_CONTENT_TAIL_CHARS=500` |
| `list_dir` | 保留前 100 条目，其余折叠 | 100 行上限 |
| `bash` | 保留 stdout 头 + stderr 完整（若 stderr 小） | `_BASH_MAX_LINES=30` |
| `web_fetch` | 先 strip HTML tags + collapse whitespace，再 head+tail | `_HTML_TAG_RE` |
| `grep_files` / `sqlite_query` | 保留前 20 匹配/行 | `_QUERY_MAX_MATCHES=20` |
| 默认 | head+tail 截断 | 同 file_read |


---

## 六、Hop Loop：工具调用的核心执行循环

**文件**：`xmclaw/daemon/hop_loop.py`（第 1–2564 行）

这是 AgentLoop 的核心执行逻辑，负责**LLM ↔ 工具之间的往返循环（hop）**。

### 6.1 单工具调用：_invoke_single_tool()

**函数签名**（第 106–334 行）：

```python
async def _invoke_single_tool(
    call: Any, effective_tools: Any, session_id: str, *,
    tool_timeout_s: float = 180.0,
    hook_engine: Any = None,
    agent_id: str = "main",
    cancel_event: asyncio.Event | None = None,
) -> Any:
```

**执行流程**：
1. **取消检查**（第 134 行）：`cancel_event.is_set()` 时立即返回取消结果。
2. **PreToolUse Hook dispatch**（第 142–177 行）：若 hook 返回 `deny` 或 `continue_=False`，则返回拒绝结果。
3. **工具调用**：`effective_tools.invoke(call_with_sid)`，带超时（默认 180s）和取消事件竞争。
4. **B-17 重试机制**（第 249–304 行）：对瞬态错误自动重试最多 3 次，退避间隔 `(0.5s, 2s, 5s)`。瞬态错误模式定义在 `history_utils.py`（第 14–79 行），覆盖 35 种网络/超时/HTTP 错误。
5. **PostToolUse Hook dispatch**（第 305–332 行）：若返回 `updated_input`，替换结果内容。

### 6.2 Hop Loop 主循环：_run_hop_loop()

**每 hop 的关键步骤**（按 `for hop in range(self._max_hops)` 循环）：

1. **动态超时调整**（第 498 行）：`_eff_timeout = min(上限, 240 + hop*120)`，hop 越深给越多时间。
2. **Cancel 检查**（第 524 行）：用户点击 Stop 时优雅退出。
3. **Steering 用户追加指令**（第 544 行）：从 `_steer_queue` 中 drain 用户中途追加的 guidance。
4. **Todo 陈旧性催促**（第 562 行）：如果 `_hops_since_todo >= 4` 且有待办未完成，注入 system 提示催促更新。
5. **预算检查**（第 583 行）：`cost_tracker.check_budget()`，超预算时返回 `ANTI_REQ_VIOLATION`。
6. **GoalAnchor 注入**（第 606 行）：每 N hops（默认 5）注入系统提示，防止长链漂移。
7. **LLM 请求**（第 642 行）：发布 `LLM_REQUEST` 事件。
8. **流式调用**（第 685 行）：`llm.complete_streaming(...)`，带 `on_chunk` / `on_thinking_chunk` / `on_tool_block` / `on_stream_fallback` 回调。
9. **B-227 分类与重试**（第 708 行）：LLM 调用异常时按原因分类，支持多模型 fallback 链。
10. **B-230 自动续写**（第 1268 行）：当 `stop_reason=max_tokens` 且内容非空时，追加 continuation prompt 并重新调用 LLM（最多 1 次）。
11. **工具调用执行**（第 1543 行起）：
    - **Phase A**：emit `TOOL_CALL_EMITTED` + `TOOL_INVOCATION_STARTED` 事件。
    - **Phase B**：read-only 工具并行执行；write 工具按目标路径分组（同文件串行，不同文件并行）。
    - **Phase C**：逐个处理结果，emit `TOOL_INVOCATION_FINISHED`。
    - **Honest Grader**（第 1894 行）：对 `skill_*` 工具调用发布 `GRADER_VERDICT`。
    - **Prompt Injection 扫描**（第 2038 行）：`apply_policy(tool_msg_content, ...)` 扫描工具输出。
    - **B-Vision**（第 2142 行）：如果工具有截图产出，注入 `images=tuple(...)` 的 user 消息。
12. **终端响应**（第 2227 行）：无 tool_calls 时，保存 assistant 消息到历史，返回 `AgentTurnResult`。

**关键安全机制**：
- **B-273**：工具结果和子 agent 回复均扫描 prompt injection。
- **B-302**：诚实守卫——若 assistant 声称记住了但未调用 memory 工具，注入纠正提示。
- **Hard cap**：单条工具结果 80K 字符上限（第 2076 行），超过则头尾截断。
- **No-progress guard**：连续 5 个 hop 无成功工具调用则终止并请求用户重新表述。

---

## 七、动作分发器：ActionDispatcher

**文件**：`xmclaw/cognition/action_dispatcher.py`（第 118–1427 行）

`ActionDispatcher` 是**CognitiveDaemon 的计划执行引擎**，将 Planner 生成的 `PlanStep` 路由到实际执行器：

| `action_kind` | 路由目标 | 行号 |
|---------------|----------|------|
| `llm_turn` | `AgentLoop.run_turn()` | 696–802 |
| `skill_invoke` | `SkillRegistry.get(skill_id).run()` | 804–909 |
| `tool_call` | `ToolProvider.invoke()` | 911–1012 |
| `wait_for_percept` | 立即返回 `pending=True` | 1014–1042 |
| `subagent` | `parallel_subagents` 工具 | 1044–1130 |

**关键设计**：
- **Epic #26 Phase B**：`prior_results` 字典支持 `{{step_id.field}}` 模板替换（第 714–753 行），让后续 step 引用前面 step 的输出。
- **成本预算门控**（第 293–360 行）：每步执行前检查 `cost_tracker.spent_usd`，超预算时返回 `PLAN_BUDGET_EXCEEDED` 并终止计划。
- **Plan 生命周期事件**：`PLAN_STARTED` / `PLAN_STEP_STARTED` / `PLAN_STEP_COMPLETED` / `PLAN_STEP_FAILED` / `PLAN_COMPLETED` / `PLAN_FAILED`（第 247–566 行）。
- **B-273 工具调用路径扫描**：`_route_tool_call()` 中对工具结果扫描 prompt injection（第 974–999 行）。
- **Stub 回退**：当对应执行器未 wired（测试/纯认知模式）时，返回 `route="stub"` 的 `StepExecutionResult`，默认 `ok=False`（Epic #27 sweep #3）。

---

## 八、钩子系统：Hook Engine

**文件**：`xmclaw/core/hooks/engine.py`（第 92–225 行）

### 8.1 生命周期事件枚举

**文件**：`xmclaw/core/hooks/events.py`（第 25–76 行）

```python
class HookEvent(str, Enum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_LLM = "PreLLM"
    POST_LLM = "PostLLM"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    TOOL_BLOCKED = "ToolBlocked"
    TOOL_FAILED = "ToolFailed"
    SUBAGENT_START = "SubagentStart"
    MEMORY_WRITE = "MemoryWrite"
    ...
```

### 8.2 HookEngine  dispatch

```python
async def dispatch(self, event: HookEvent, *, session_id, agent_id, payload, hop=-1) -> DispatchOutcome:
    # 1. 检查 feature flag hooks.enabled
    if not default_engine().is_enabled("hooks.enabled", default=True):
        return DispatchOutcome(event=event)
    
    # 2. 按 event + matchers 过滤
    matching = [s for s in self._specs if s.event == event.value and _matches(s, payload)]
    
    # 3. 并发执行所有匹配 hook（asyncio.gather）
    results = await asyncio.gather(*(_run_one(s) for s in matching))
    
    # 4. 合并决策：deny > ask > allow
    outcome.decision = merge_decisions(results)
    # 5. continue_=False 时阻断生命周期
    for r in results:
        if not r.continue_:
            outcome.continue_ = False
            outcome.block_reason = r.reason
```

### 8.3 五种 Runner

**文件**：`xmclaw/core/hooks/runners.py`（第 125–434 行）

| Runner | 类型 | 信任要求 | 用途 |
|--------|------|----------|------|
| `CommandRunner` | shell 命令 | workspace_trust == "trusted" | 最灵活，外部脚本 |
| `FunctionRunner` | Python `module:function` | workspace_trust == "trusted" | 性能敏感，无子进程开销 |
| `HttpRunner` | POST JSON 到 URL | 无需信任 | 外部服务审批 |
| `PromptRunner` | 向 daemon LLM 提问 | 无需信任 | 最慢，LLM 驱动决策 |
| `AgentRunner` | fire-and-forget sub-agent | 无需信任 | 信息性，不阻塞 |


---

## 九、与外部框架的依赖关系

### 9.1 无 LangChain 依赖

XMclaw 的 Tool Use 架构**完全自研**，与 LangChain 的 `BaseTool` / `Tool` 等类无继承或调用关系。设计哲学在注释中有明确体现：

> "Anti-req #14: MCP as first-class protocol. Concrete implementation, not a stub."
> — `mcp_bridge.py`

> "Anti-req #1 (Scheduler must not trust text that *describes* a tool call): `ToolCall` is a structured dataclass."
> — `toolcall.py`

### 9.2 外部依赖清单

| 依赖 | 用途 | 是否必须 |
|------|------|----------|
| `mss` | `screen_capture` 截图 | 可选（`xmclaw[computer-use]`） |
| `rapidocr_onnxruntime` | `screen_ocr` 中文 OCR | 可选 |
| `pyautogui` | `computer_click/type/scroll` | 可选 |
| `pygetwindow` | `window_list/activate` | 可选 |
| `playwright` | `browser_open/snapshot/click/fill` | 可选 |
| `lark_oapi` | 飞书 IM 发送 | 可选 |
| `slack_sdk` | Slack IM 发送 | 可选 |
| `pyperclip` | 剪贴板读写 | 可选 |
| `httpx` | Hook HTTP runner | 可选（Hook 用 HTTP 时） |
| Node.js (`npx`/`node`) | 启动 Node.js 编写的 MCP server | 仅使用 MCP 时需要 |

### 9.3 MCP 协议兼容性

- 实现的是 **MCP 2024-11-05 spec**（`xmclaw/providers/tool/mcp_bridge.py:48`）。
- 与 Claude Desktop / Claude Code 的 `mcpServers.json` 格式兼容（`mcp_hub.py:9`）。
- 支持 `stdio` 传输（已实现）和 `sse`/`streamableHttp`（schema 已识别，执行端跳过）。

---

## 十、总结：XMclaw 工具使用架构图

```
+---------------------------------------------------------------------+
|                         LLM Provider Layer                          |
|  (Anthropic / OpenAI / MiniMax / DeepSeek / Zhipu / Moonshot ...)   |
|                                                                     |
|  +-------------+    +-------------+    +-------------+            |
|  |  complete   |--->|  streaming  |--->|  on_tool    |            |
|  |  (sync)     |    |  (async)    |    |  _block     |            |
|  +-------------+    +-------------+    +-------------+            |
+---------------------------------------------------------------------+
                              |
                              v produces ToolCall (core/ir/toolcall.py)
+---------------------------------------------------------------------+
|                      Hop Loop (daemon/hop_loop.py)                  |
|                                                                     |
|  for hop in range(max_hops):                                       |
|    1. 动态超时 (_hop_timeout)                                       |
|    2. Cancel 检查 / Steering 指令 / Todo 催促 / 预算检查              |
|    3. GoalAnchor 注入                                               |
|    4. LLM call (complete_streaming)                                |
|    5. B-227 分类重试 / B-230 自动续写                                |
|    6. 若 response.tool_calls:                                      |
|         a. emit TOOL_CALL_EMITTED + TOOL_INVOCATION_STARTED         |
|         b. Phase 11: 能力提示 (vision -> 换模型)                    |
|         c. Phase B: 并行执行 (read-only 并发 / write 按路径串行)   |
|              +-------------------------------------+                |
|              |  _invoke_single_tool() per call     |                |
|              |  +-- PreToolUse Hook (允许/拒绝)      |                |
|              |  +-- ToolProvider.invoke()            |                |
|              |  +-- PostToolUse Hook (修改结果)      |                |
|              |  +-- B-17 重试 (瞬态错误 3x)          |                |
|              +-------------------------------------+                |
|         d. Phase C: 逐个处理结果                                     |
|              +-- Honest Grader -> GRADER_VERDICT                    |
|              +-- Prompt Injection 扫描 (B-273)                       |
|              +-- 80K 字符硬截断                                      |
|              +-- B-Vision: 截图注入 user 消息                        |
|         e. 追加 tool result 到 messages，继续下一轮 hop            |
|    7. 若无 tool_calls: 终端响应，保存历史，返回 AgentTurnResult       |
+---------------------------------------------------------------------+
                              |
                              v routes via name
+---------------------------------------------------------------------+
|              CompositeToolProvider (providers/tool/composite.py)      |
|                                                                     |
|  +----------------+  +----------------+  +----------------+        |
|  |  BuiltinTools  |  | SkillToolProv  |  |   MCPHub       |        |
|  |  (builtin.py)  |  | (tool_bridge)  |  | (mcp_hub.py)   |        |
|  |                |  |                |  |                |        |
|  | file_read/write|  | skill_browse   |  | server1__toolA |        |
|  | bash           |  | skill_run      |  | server2__toolB |        |
|  | web_search     |  | skill_install  |  |                |        |
|  | memory         |  | skill_diff     |  |                |        |
|  | todo_write     |  | skill_propose  |  |                |        |
|  | ask_user       |  | skill_compose  |  |                |        |
|  | ...            |  | ...            |  |                |        |
|  +----------------+  +----------------+  +----------------+        |
|  +----------------+  +----------------+                              |
|  | AgentInterTools|  | FilteredToolProvider (可选)               |
|  | (agent_inter)  |  | (filtered.py)  <- cron enabled_toolsets   |
|  | chat_with_agent|  +----------------+                              |
|  | submit_to_agent|                                                |
|  +----------------+                                                |
+---------------------------------------------------------------------+
                              |
                              v results
+---------------------------------------------------------------------+
|                    Context Management / Post-processing               |
|                                                                     |
|  +--------------------+  +--------------------+  +----------------+ |
|  | tool_result_prune  |  | tool_result_summar|  | ContextEngine  | |
|  | (prune_old_tool_   |  | izer (summarize_  |  | (engine.py)    | |
|  |  results)          |  | tool_result)       |  | 6-stage ABC    | |
|  |  - Pass 1 Dedup    |  |  - 零 LLM 延迟     |  |                | |
|  |  - Pass 1.5 Semantic|  |  - 按工具类型策略  |  |                | |
|  |  - Pass 2 1-line   |  |  - head+tail 截断  |  |                | |
|  |  - Pass 3 Args截断 |  |                    |  |                | |
|  +--------------------+  +--------------------+  +----------------+ |
|                                                                     |
|  +--------------------+  +--------------------+                     |
|  | ActionDispatcher   |  | HookEngine         |                     |
|  | (action_dispatcher)|  | (core/hooks/)      |                     |
|  | llm_turn/skill_    |  | PreToolUse/PostTool|                     |
|  | invoke/tool_call/  |  | Use/TOOL_BLOCKED   |                     |
|  | wait_for_percept/  |  | 5 runners: command/|                     |
|  | subagent           |  | function/http/     |                     |
|  | prior_results {{}}  |  | prompt/agent       |                     |
|  +--------------------+  +--------------------+                     |
+---------------------------------------------------------------------+
```

---

## 十一、关键设计决策总结

| 设计点 | 决策 | 理由 |
|--------|------|------|
| 自研 IR 而非复用 LangChain | `ToolCall`/`ToolSpec`/`ToolResult` 三件套 | 类型安全、与提供者解耦、支持 `side_effects` 等自定义字段 |
| 组合而非继承 | `CompositeToolProvider` 聚合多个 `ToolProvider` | 动态增减、O(1) 路由、名字冲突显式报错 |
| 技能命名空间 | `skill_demo__foo`（`.` → `__`） | Anthropic 64 字符限制，避免与内置工具冲突 |
| 渐进披露 | inline / unified / auto 三种模式 | 小库时直接调用快，大库时省 context tokens |
| read-only 并行 | `read_only=True` 的 ToolSpec 并行执行 | 加速无冲突的批量查询 |
| write 串行分组 | 按目标 `path` 分组 | 同文件操作串行保安全，不同文件并行提效率 |
| 瞬态错误重试 | 3 次退避 `(0.5, 2, 5)` 秒 | 覆盖网络抖动、DNS 失败、HTTP 503 等真实场景 |
| 工具结果硬截断 | 80K 字符 | 防止单个工具输出炸毁上下文窗口 |
| Prompt Injection 扫描 | 工具结果 + 子 agent 回复双路径 | 防止工具本身或恶意 agent 注入指令 |
| Honest Grader | 每 tool call 后评估可观测性 | 为 EvolutionAgent 提供 (skill_id, version) 级别的反馈 |

---

> 报告完。如需进一步深入某个模块（如 MCP 协议实现细节、Hook 引擎配置语法、或 EvolutionAgent 的 skill 进化循环），请指示。
