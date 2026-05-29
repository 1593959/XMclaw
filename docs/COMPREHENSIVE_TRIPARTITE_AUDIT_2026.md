# XMclaw vs OpenClaw vs Hermes — 全面三方源码级架构审计报告

> **审计日期**: 2026-05-29
> **审计方法**: 并行源码走查（XMclaw 本地代码库 + OpenClaw GitHub 源码 + Hermes GitHub 源码）
> **审计深度**: 文件级，关键模块逐行分析
> **报告版本**: v1.0

---

## 执行摘要

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **代码成熟度** | 高（~8万行 Python，大量 docstring 注释） | 极高（~36万行 TS，生产级 362k stars） | 高（~5万行 Python，测试 25k+） |
| **架构清晰度** | ★★★★★ 分层最清晰（core/providers/daemon/skills） | ★★★★☆ 单体 Gateway，插件化极好 | ★★★★☆ 单体 AIAgent，多入口共享核心 |
| **通道数量** | 7（TG/Slack/Discord/Lark/Ding/Email/WeCom） | 25+（含 WhatsApp/Signal/iMessage/QQ 等） | 20+（含 WhatsApp/Signal/Matrix 等） |
| **工具数量** | ~40（含 builtin/browser/LSP/MCP/Composio） | 40+（内置 + 插件扩展） | 40+（内置 + MCP + 插件） |
| **沙箱后端** | 6（Local/Process/Docker/SSH/Daytona/Modal/Vercel） | 3（Docker/SSH/OpenShell） | 7（Local/Docker/SSH/Singularity/Modal/Daytona/Vercel） |
| **记忆系统** | 双栈 V1(sqlite-vec) + V2(LanceDB 图) | 多后端（sqlite-vec/QMD/Honcho） | 多插件（Honcho/mem0/supermemory） |
| **安全层数** | 5（扫描/守卫/审批/审计/撤销） | 8+（配对/策略/沙箱/扫描/钩子/隔离） | 7（审批/扫描/容器/隔离/MCP过滤） |
| **自进化** | ★★★★★ UCB1 + HonestGrader + 版本化 SkillRegistry | ★★☆☆☆ 无内置进化，依赖外部插件 | ★★★★☆ 自主技能创建 + 经验改进 |
| **多智能体** | 管理器层（MultiAgentManager）已存在，但未完全启用 | ★★★★★ Workspace + Bindings + Sub-agent | ★★★★☆ Sub-agent 并行（delegate_task） |
| **前端** | Web UI（Preact+htm 22页）+ TUI（Textual） | Vite React SPA Control UI + WebChat | TUI（Ink React）+ Python TUI Gateway |
| **部署** | pip editable + `xmclaw serve` | npm global / Docker / Nix / systemd / launchd | pip / `hermes gateway start` |
| **协议兼容** | 自有 WS + REST | OpenAI-compatible API | ACP (IDE) + MCP (双向) |

**核心结论**：
- **XMclaw 的差异化优势**：自进化引擎（UCB1 调度 + HonestGrader 多信号评分 + 证据门禁晋升）是全球唯一的数学化技能进化系统；认知架构（Jarvisification）的感知-推理-规划-执行闭环代码完整。
- **XMclaw 的结构性短板**：大量高级模块（CognitiveDaemon、Planner、ReasoningEngine、SelfExperimentLoop）存在 **"代码完成 ↔ 运行时未接线"** 断层；默认配置全 OFF（`autonomy_level=0`）， fresh install 等于普通聊天机器人。
- **OpenClaw 的不可复制优势**：25+ 通道的成熟插件生态、OpenAI-compatible API 表面、企业级沙箱隔离、设备配对安全体系。XMclaw 在通道数量和生态位上差距巨大。
- **Hermes 的不可复制优势**：自主技能创建（从经验生成 SKILL.md）、7 种终端后端（含 serverless）、轨迹压缩 + RL 训练环境、双向 MCP 集成。XMclaw 的技能系统目前只是静态 Markdown 加载，无自创建能力。

---

## 1. 宏观架构对比

### 1.1 技术栈与运行时

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **语言** | Python 3.10+ | TypeScript (Node.js 22+) | Python 3.11+ |
| **构建工具** | uv + setuptools | pnpm workspace | setuptools + uv |
| **Web 框架** | FastAPI + Uvicorn | 自建 Gateway（WS + HTTP） | 无（CLI 为主） |
| **进程模型** | 单进程 asyncio | 单进程 Node.js event loop | 单进程 / ThreadPoolExecutor |
| **配置格式** | JSON + env `XMC__*` | JSON + env | YAML + env |
| **数据存储** | SQLite (WAL) + LanceDB | SQLite + JSONL | SQLite (FTS5) |
| **依赖注入** | 工厂函数手工组装 | 插件注册表 + 服务定位器 | 注册表模式 + `check_fn` 门控 |

### 1.2 目录结构哲学

**XMclaw** — 严格的 DAG 分层：
```
core/       ← 纯业务逻辑，禁止导入 providers/ 和 skills/
providers/  ← I/O 适配器（LLM/Tool/Channel/Memory/Runtime）
daemon/     ← 运行时编排（唯一允许跨层导入的地方）
skills/     ← 用户可扩展能力
```
- 通过 `scripts/check_import_direction.py` 在 CI 中强制执行
- `context/` 独立成包，专供上下文压缩

**OpenClaw** — 功能域驱动：
```
src/agents/     ← Agent 运行时（核心最大包）
src/channels/   ← 通道适配器
src/plugins/    ← 插件系统
src/gateway/    ← WS 网关
src/cli/        ← CLI
extensions/     ← 捆绑的通道插件（每个独立 npm 包）
```
- 无严格的 core/providers 分层，但插件 SDK 定义了清晰的契约边界

**Hermes** — 扁平 + 按角色分包：
```
run_agent.py        ← 核心 AIAgent（~4400 LOC，单体）
cli.py              ← 交互式终端（~11000 LOC）
agent/              ← Agent 内部（prompt_builder, compressor, memory_manager）
tools/              ← 工具实现（每个工具一个文件）
gateway/            ← 消息网关
hermes_cli/         ← CLI 子命令
plugins/            ← 插件（memory, context_engine, model-providers）
```
- 核心引擎极度扁平：`AIAgent` 一个类承载了 XMclaw 中 AgentLoop + HopLoop + HistoryCompression + PromptBuilder 的职责

### 1.3 启动时依赖图复杂度

**XMclaw**（最复杂）：
```
config.json → factory.build_agent_from_config()
    ├── build_llm_from_config()       → 选 provider
    ├── build_tools_from_config()     → CompositeToolProvider 堆叠
    ├── build_memory_from_config()    → MemoryManager + SqliteVecMemory
    ├── build_skill_runtime()         → Local / Process SkillRuntime
    ├── build_agent_from_config()     → AgentLoop 组装
    └── create_app()                  → FastAPI + lifespan + 25+ routers
```
- Lifespan 中还要启动：内存索引器、配置热重载监听器、Cron 定时器、Evolution 代理、JournalWriter、ProfileExtractor、CognitiveDaemon

**OpenClaw**（中等）：
```
openclaw.json → Gateway
    ├── 加载插件注册表
    ├── 启动通道适配器
    └── 绑定 WS 端口
```
- Agent 按需实例化（per-session），非启动时全局单例

**Hermes**（最简单）：
```
hermes chat → AIAgent() 实例化
    ├── 加载 ~/.hermes/config.yaml
    ├── 构建 PromptBuilder
    └── 连接 SessionDB
```
- Gateway 模式：`hermes gateway start` 才启动长期运行的消息网关

---

## 2. Agent Loop / Turn Execution 深度对比

### 2.1 核心类定位

| 项目 | 核心类 | 文件 | 规模 | 设计模式 |
|------|--------|------|------|----------|
| **XMclaw** | `AgentLoop` + `HopLoopMixin` + `HistoryCompressionMixin` | `daemon/agent_loop.py` + `daemon/hop_loop.py` + `daemon/history_compression.py` | ~2580 + ~1532 + ~843 LOC | Mixin 组合，事件驱动 |
| **OpenClaw** | `runEmbeddedAgent()` 函数 + `EmbeddedPiAgent` | `src/agents/pi-embedded-runner/run.ts` | ~2000+ LOC（分散） | 函数式流水线，Session Lane 串行 |
| **Hermes** | `AIAgent` | `run_agent.py` | ~4400 LOC（单体） | 同步单类，回调驱动 |

### 2.2 Turn 生命周期对比

**XMclaw — 最丰富的生命周期事件**
```
run_turn(session_id, user_message)
  ├── Hook: USER_PROMPT_SUBMIT (可改写/阻断)
  ├── Auto-recall: MemoryService.recall() → 前置记忆块
  ├── Publish: USER_MESSAGE → Bus
  ├── Build messages (system + history + user)
  ├── Hop loop (max_hops=5):
  │   ├── Resolve LLM (profile / tier / fallback)
  │   ├── Publish: LLM_REQUEST
  │   ├── llm.complete_streaming() → LLM_CHUNK 事件流
  │   ├── Publish: LLM_RESPONSE
  │   ├── If tool_calls:
  │   │   ├── Pre-tool hook (可 deny / rewrite args)
  │   │   ├── ToolGuardEngine 扫描
  │   │   ├── ApprovalService 审批门
  │   │   ├── ToolProvider.invoke() → ToolResult
  │   │   ├── Publish: TOOL_INVOCATION_FINISHED
  │   │   ├── HonestGrader 评分 → GRADER_VERDICT
  │   │   └── Feed result → next hop
  │   └── If text: return
  ├── Post-turn:
  │   ├── Memory auto-put (事实提取)
  │   ├── Persona render (v2)
  │   ├── SessionStore.save()
  │   └── Cost tracking
  └── Return: AgentTurnResult
```
- **取消机制**: 每 session 一个 `asyncio.Event`，hop 边界检查
- **超时机制**: 每 LLM 调用 300s，每工具调用 180s（`asyncio.wait_for`）
- **并发**: 单 session 串行，但多个 session 可并行

**OpenClaw — 最严格的串行化**
```
runEmbeddedAgent()
  ├── Acquire session write lock (file-based, 60s timeout)
  ├── Load session history (JSONL)
  ├── Load skills snapshot
  ├── Build system prompt
  ├── Resolve model + auth profile
  ├── Call LLM (streaming)
  ├── If tool_calls:
  │   ├── Policy check (allow/deny)
  │   ├── before_tool_call hooks
  │   ├── Execute in sandbox or host
  │   ├── after_tool_call hooks
  │   └── Feed back
  ├── Reply shaping + filter NO_REPLY
  ├── Write JSONL transcript
  └── Release lock
```
- **关键差异**: Session Lane 保证单写者语义，彻底避免竞态
- **Continuation**: `maxChainLength=10` 的自动 CONTINUE_WORK 检测

**Hermes — 最简洁的同步循环**
```
AIAgent.run_conversation()
  ├── Generate task_id
  ├── Append user msg to history
  ├── Build / reuse cached system prompt
  ├── Preflight compression (>50% context)
  ├── Build API messages (格式转换)
  ├── _interruptible_api_call()
  │   ├── 后台线程执行 HTTP
  │   └── 主线程监控中断事件
  ├── Parse response:
  │   ├── tool_calls → model_tools.handle_function_call()
  │   │   ├── Single tool: 主线程同步执行
  │   │   └── Multiple tools: ThreadPoolExecutor 并发
  │   └── Text: persist + return
  └── Return result dict
```
- **关键差异**: `AIAgent` 是同步类，所有 async 都在外部桥接；`ThreadPoolExecutor` 用于多工具并发
- **Fallback**: 429/5xx/401 时自动切换 fallback_providers

### 2.3 关键差距分析

| 能力 | XMclaw | OpenClaw | Hermes | 差距评估 |
|------|--------|----------|--------|----------|
| **取消/中断** | ✅ asyncio.Event per session | ✅ AbortController | ✅ 中断事件 + 后台线程丢弃 | 持平 |
| **Hook 系统** | ✅ Pre/post tool hooks + prompt submit hook | ✅ before/after_tool_call, agent_end hooks | ✅ Pre/post plugin hooks | 持平 |
| **工具并发** | ❌ 单 session 串行 | ❌ Session Lane 串行 | ✅ ThreadPoolExecutor 并发 | Hermes 领先 |
| **LLM 超时** | ✅ 300s wall-clock | ⚠️ Provider timeout | ✅ 可中断 | XMclaw/Hermes 领先 |
| **Grader 评分** | ✅ HonestGrader（双信号 Iron Rule #1） | ❌ 无内置 | ❌ 无内置 | XMclaw 独一档 |
| **成本追踪** | ✅ CostTracker per turn | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **Tier 路由** | ✅ ModelTierRouter（fast/quality/cheap） | ❌ 无 | ✅ Auxiliary client | XMclaw/Hermes 领先 |
| **工具预算** | ❌ 无迭代预算 | ✅ maxChainLength | ✅ max_turns + subagent budget | OpenClaw/Hermes 领先 |

---

## 3. LLM Provider 抽象对比

### 3.1 架构

**XMclaw**:
```python
class LLMProvider(ABC):
    @abstractmethod
    async def stream(self, messages, tools, *, cancel) -> AsyncIterator[LLMChunk]
    @abstractmethod
    async def complete(self, messages, tools) -> LLMResponse
    @property
    def tool_call_shape(self) -> ToolCallShape  # ANTHROPIC_NATIVE / OPENAI_TOOL / ...
    @property
    def pricing(self) -> Pricing
```
- 实现: `AnthropicLLM`, `OpenAILLM`, `OpenRouterLLM`
- 格式转换: `ToolCallShape` 枚举 + 各 provider 的 translator
- **Prompt caching**: `CACHE_BREAKPOINT_MARKER` 字符串标记（Anthropic + OpenAI 兼容）
- **Thinking blocks**: 2026-05-26 新增，支持 Claude thinking + DeepSeek reasoning_content echo

**OpenClaw**:
- `ModelRegistry` 维护模型能力表（context window, tool support, image support）
- `AuthProfile` 管理多 key + cooldown + failover
- Provider 贡献 cache-aware prompt guidance（prefix/suffix 分割）
- **动态模型发现**: OpenRouter 目录自动拉取

**Hermes**:
- 三种 API mode: `chat_completions` / `codex_responses` / `anthropic_messages`
- `runtime_provider.resolve_runtime_provider()` 自动映射 (provider, model) → (api_mode, key, base_url)
- `auxiliary_client` 辅助 LLM 用于视觉/摘要等 side task
- **Provider 插件**: 简单 OpenAI-compatible provider 只需 `__init__.py` + `plugin.yaml`

### 3.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **Provider 数量** | 3 (Anthropic/OpenAI/OpenRouter) | 10+ (含 Ollama/xAI/DeepSeek 等) | 18+ | XMclaw 落后 |
| **Auth 轮换** | ❌ 无 | ✅ AuthProfile cooldown + failover | ✅ Fallback chain | XMclaw 落后 |
| **Prompt cache** | ✅ CACHE_BREAKPOINT_MARKER | ✅ Provider 贡献分割 | ✅ Anthropic 标记 | 持平 |
| **Thinking 支持** | ✅ Claude + DeepSeek echo | ⚠️ 基础 | ✅ Reasoning content | 持平 |
| **模型发现** | ✅ 静态表 + OpenRouter 自动拉取 (TTL 24h 缓存) | ✅ OpenRouter 自动拉取 | ✅ models.dev 集成 | 已补齐 (B-387) |
| **Context length** | ✅ 三层解析（override→static→ratchet） | ✅ 静态表 | ✅ model_metadata | 持平 |
| **Auxiliary LLM** | ❌ 无 | ❌ 无 | ✅ auxiliary_client | Hermes 领先 |

---

## 4. Tool System 深度对比

### 4.1 注册与发现

**XMclaw**:
- `ToolProvider` ABC: `list_tools()` + `invoke(call)`
- `BuiltinTools`: 手工维护的 `_specs.py` + `_helpers.py`
- `CompositeToolProvider`: 堆叠多个 provider（builtin + skill + MCP + ...）
- **Skill bridge**: `SkillToolProvider` 将注册技能暴露为工具（渐进披露模式）
- **MCP Hub**: 多 server 注册表，64 字符工具名截断 + hash 防碰撞

**OpenClaw**:
- 工具 = first-class + plugin 注册
- `api.registerTool()` 在插件中注册
- 策略过滤（allow/deny）在模型调用前应用
- 沙箱内执行（Docker/SSH/OpenShell）

**Hermes**:
- **AST 自发现**: `discover_builtin_tools()` 扫描 `tools/*.py` 的 AST 找 `registry.register()` 调用
- 每个工具模块自注册：
```python
registry.register(
    name="terminal", toolset="terminal",
    schema={...}, handler=handle_terminal,
    check_fn=check_terminal,  # 可用性检查
)
```
- `check_fn` 缓存：API key 存在 / 服务运行 / 二进制安装
- **Toolsets**: 命名工具包，可按平台预设启用

### 4.2 内置工具对比

| 工具类别 | XMclaw | OpenClaw | Hermes |
|----------|--------|----------|--------|
| **文件系统** | file_read/write/list_dir/delete/glob/grep | read/write/edit/list | read_file/write_file/patch/search_files |
| **Shell** | bash (PowerShell/bash aware) | bash/process | terminal (7 backend) |
| **Web** | web_fetch/web_search (DuckDuckGo) | web_fetch/web_search | web_search/web_extract |
| **Browser** | browser_open/click/fill/screenshot (Playwright) | browser (CDP/noVNC) | browser_tool (10 tools) |
| **LSP** | lsp (语言服务器) | ❌ 无 | ❌ 无 |
| **数据库** | sqlite_query | ❌ 无 | ❌ 无 |
| **Canvas** | canvas_create/update/close | canvas/A2UI | ❌ 无 |
| **Computer Use** | screen_capture/mouse/keyboard (pyautogui) | ❌ 无 | ❌ 无 |
| **Media** | voice_synthesize/transcribe/camera | ❌ 无 | ❌ 无 |
| **日历** | schedule_followup | cron | cron |
| **记忆** | memory_search/get/put/dedup/compact | memory_search/get | session_search/honcho_search |
| **Persona** | update_persona/remember/learn_about_user | ❌ 无 | ❌ 无 |
| **Undo** | undo_list/undo_recent | ❌ 无 | ❌ 无 |
| **Sub-agent** | builtin_subagent | sessions_spawn | delegate_task |
| **MCP** | mcp_hub (stdio + HTTP) | MCP server 插件 | mcp_tool (client+server) |
| **Composio** | composio (7000+ SaaS) | ❌ 无 | ❌ 无 |
| **代码执行** | ❌ 无 | execute_code | execute_code |
| **Canvas/可视化** | ✅ canvas | ✅ canvas/A2UI | ❌ 无 |

### 4.3 执行安全

**XMclaw**:
- `GuardedToolProvider`: 预执行 `ToolGuardEngine` 扫描
- Guardians: `FilePathToolGuardian`（敏感路径）、`RuleBasedToolGuardian`（规则匹配）、`ShellEvasionGuardian`（混淆检测）
- `ApprovalService`: 敏感操作人工审批队列
- `UndoCabinet`: 文件操作撤销栈

**OpenClaw**:
- Cascading tool policy: global → agent → channel → sandbox
- `before_tool_call` / `after_tool_call` hooks
- Sandbox 内执行隔离

**Hermes**:
- `tools/approval.py`: ~20 个正则检测破坏性操作
- Container isolation (Docker/Singularity/Modal)
- `Tirith` 预执行扫描（homograph URL、pipe-to-interpreter）
- MCP credential filtering（env 隔离）

### 4.4 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **工具发现** | 手工维护 | 插件注册 | AST 自发现 + 缓存 | Hermes 最优雅 |
| **可用性检查** | ❌ 无 | ❌ 无 | ✅ check_fn | Hermes 领先 |
| **沙箱执行** | ✅ 6 后端 | ✅ 3 后端 | ✅ 7 后端 | Hermes 最全 |
| **撤销机制** | ✅ UndoCabinet | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **审批门** | ✅ ApprovalService REST | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **渐进披露** | ✅ inline/unified/auto | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Composio** | ✅ 7000+ SaaS | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **代码执行** | ❌ 无 | ✅ execute_code | ✅ execute_code | XMclaw 落后 |

---

## 5. Channel / Messaging 深度对比

### 5.1 通道数量与成熟度

| 通道 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| Telegram | ✅ 长轮询 | ✅ grammY | ✅ |
| Slack | ✅ Socket Mode | ✅ Bolt | ✅ |
| Discord | ✅ | ✅ Carbon | ✅ |
| Feishu/Lark | ✅ WS 长连 | ✅ | ✅ |
| DingTalk | ✅ Stream | ✅ | ✅ |
| WeCom | ✅ Outbound webhook | ✅ | ✅ |
| Email | ✅ IMAP+SMTP | ✅ | ✅ |
| WhatsApp | ❌ | ✅ Baileys | ✅ |
| Signal | ❌ | ✅ | ✅ |
| iMessage | ❌ | ✅ | ❌ |
| Matrix | ❌ | ✅ | ✅ |
| Mattermost | ❌ | ✅ | ❌ |
| QQ | ❌ | ✅ | ✅ |
| WebChat | ❌ | ✅ | ❌ |
| 总计 | 7 | 25+ | 20+ |

### 5.2 抽象设计

**XMclaw**:
```python
class ChannelAdapter(ABC):
    name: ClassVar[str]
    async def start(self) -> None
    async def stop(self) -> None
    async def send(self, target: ChannelTarget, payload: OutboundMessage) -> str
    def subscribe(self, handler: Callable[[InboundMessage], Awaitable[None]]) -> None
```
- `ChannelDispatcher` 统一桥接：adapter inbound → `AgentLoop.run_turn` → adapter outbound
- 每个适配器 ~1500 LOC（Feishu 最复杂，含 markdown→card 自动检测）
- 消息格式：纯文本 + `extra` dict（富内容扩展点）

**OpenClaw**:
- 通道 = 插件（`extensions/<channel>/` 独立 npm 包）
- 统一的 `MessageEvent` 归一化层
- Auto-reply pipeline: pairing check → queue → session resolution
- 队列模式: steer/followup/collect/interrupt

**Hermes**:
- `BaseAdapter` ABC: `connect/disconnect/send_message/on_message`
- Session key: `agent:main:{platform}:{chat_type}:{chat_id}`
- 授权层：per-platform allow-all → allowlist → DM pairing → global allow-all → deny

### 5.3 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **通道数量** | 7 | 25+ | 20+ | XMclaw 严重落后 |
| **插件化** | ⚠️ 基础（entry points） | ✅ 完整插件市场 | ✅ 插件 SDK | XMclaw 落后 |
| **DM 配对** | ❌ 无 | ✅ 设备配对 + nonce 签名 | ✅ 配对码 | XMclaw 落后 |
| **队列模式** | ❌ 无 | ✅ steer/followup/collect/interrupt | ❌ 无 | OpenClaw 领先 |
| **自动回复** | ⚠️ 简单 delayed ack | ✅ 完整 auto-reply pipeline | ⚠️ 基础 | OpenClaw 领先 |
| **富内容** | ⚠️ extra dict | ✅ Cards/Canvas/A2UI | ⚠️ 基础 | OpenClaw 领先 |

---

## 6. Memory / Context 管理深度对比

### 6.1 记忆系统架构

**XMclaw — 双栈 + 图结构（最复杂）**

V1 (Workspace Indexing):
- `SqliteVecMemory`: SQLite + sqlite-vec 扩展
- 三层: short / working / long
- TTL + retention caps (max_items/max_bytes per layer)
- 索引: MEMORY.md / USER.md / workspace_paths

V2 (Facts + Relations Graph):
- `MemoryService`: LanceDB backend
- `Fact`: deterministic ID (`kind:scope:hash12(text)`)
- Near-duplicate merge (cosine < 0.15)
- Auto-promote to long_term when `evidence_count >= 3`
- Relation edges: `SAME_TOPIC`, `CONTRADICTS`, `CAUSED_BY`, `SUPERSEDES`
- Entity token extraction
- `KeyInfoExtractor`: LLM-driven 事实提取
- `LLMTopic`: 主题建模

Context Compression:
- `ContextCompressor`: 5 阶段管道（Hermes 移植）
- CJK-aware token estimation (chars/4 for ASCII, chars*2/3 for CJK)
- Threshold: 85% context window
- Protect first N + last 20% ratio

**OpenClaw — 多后端插件**

| 后端 | 存储 | 搜索 |
|------|------|------|
| Builtin (默认) | SQLite + sqlite-vec + Markdown | Hybrid vector + keyword |
| QMD | Local sidecar | Reranking + query expansion |
| Honcho | Dedicated service | Semantic over observations |

- Workspace files: `MEMORY.md`, `memory/YYYY-MM-DD.md`, `DREAMS.md`
- `memory_search` / `memory_get` 工具按需检索
- Config: `hybrid.vectorWeight/textWeight`, `temporalDecay`

**Hermes — 插件化记忆**

- 内置: `~/.hermes/MEMORY.md` + `USER.md` + SQLite FTS5
- `SessionDB`: `messages_fts` + `messages_fts_trigram`（CJK 支持）
- `session_search` 工具搜索历史对话
- 插件: Honcho（跨会话用户建模）、mem0、supermemory、hindsight
- `MemoryManager`  orchestrates provider lifecycle

### 6.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **向量后端** | sqlite-vec + LanceDB | sqlite-vec + QMD + Honcho | SQLite FTS5 + 插件 | XMclaw 领先 |
| **关系图** | ✅ LanceDB 图边 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **去重合并** | ✅ cosine < 0.15 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **主题建模** | ✅ LLMTopic | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **会话搜索** | ⚠️ SessionStore LIKE | ⚠️ 基础 | ✅ FTS5 + trigram | Hermes 领先 |
| **上下文压缩** | ✅ 5 阶段 Hermes 移植 | ✅ 插件化 | ✅ 原生 compressor | 持平 |
| **CJK 感知** | ✅ chars*2/3 | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **记忆渲染** | ✅ v2_renderer  persona | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |

---

## 7. Security / Prompt Injection 深度对比

### 7.1 防御层次

**XMclaw — 5 层防御**

| 层 | 实现 | 文件 |
|---|------|------|
| 1. Prompt Injection Scanner | 70+ 正则 + YAML 规则 + 零宽字符检测 | `security/prompt_scanner.py` |
| 2. Tool Guard | FilePath / RuleBased / ShellEvasion Guardians | `security/tool_guard/` |
| 3. Approval Service | 敏感操作人工审批队列 | `security/approval_service.py` |
| 4. Security Auditor | SQLite 统一审计日志 | `security/auditor.py` |
| 5. Undo Cabinet | 文件操作撤销栈 | `security/undo_cabinet.py` |

- **PolicyMode**: DETECT_ONLY / REDACT / BLOCK
- **Source 抑制**: memory_recall 和 skill_body 的 role-forgery 误报抑制
- **B-273 注入源**: tool_result / agent_profile / memory_recall / web_fetch / sub_agent / channel / skill_body

**OpenClaw — 8+ 层防御**

| 层 | 机制 |
|---|------|
| Gateway auth | Shared-secret / password / Tailscale / device pairing |
| Pairing | Challenge nonce signing; loopback auto-approve |
| DM policies | `dmPolicy="pairing"` 默认 |
| Channel allowlists | `allowFrom` 限制触发者 |
| Tool policy | allow/deny lists; cascading (global→agent→channel→sandbox) |
| Exec approvals | Human-in-the-loop (`tools.exec.ask`) |
| Sandboxing | Docker/SSH/OpenShell 隔离 |
| Prompt injection | Scanner heuristics (`openclaw-prism`); tool guards; hooks |
| Skill install guards | `before_install` hook |
| Session isolation | Per-session lanes |

**Hermes — 7 层防御**

| 层 | 实现 |
|---|------|
| User authorization | Allowlists, DM pairing |
| Dangerous command approval | `tools/approval.py` ~20 正则 |
| Container isolation | Docker/Singularity/Modal/SSH |
| MCP credential filtering | Env var isolation |
| Context file scanning | `prompt_builder.py` 扫描 AGENTS.md / SOUL.md |
| Memory write scanning | `tools/memory_tool.py` 12 正则 |
| Skills guard | `tools/skills_guard.py` ~90 正则 |
| Tirith pre-exec | Homograph URL / pipe-to-interpreter / terminal injection |
| Tool result sanitization | `<tool_result trust="untrusted">` 语义分隔符 |

### 7.2 Prompt Injection Scanner 细节对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **模式数量** | 70+ 内联正则 + YAML 规则 | 社区插件 `openclaw-prism` | ~90 正则 (skills_guard) + 12 (memory) |
| **语言覆盖** | 英文 + 中文完整对照组 | 英文为主 | 英文为主 |
| **分类体系** | instruction_override / role_forgery / exfiltration / jailbreak / indirect_injection / tool_hijack / c2_promptware / supply_chain | 基础分类 | 基础分类 |
| **去重逻辑** | ✅ 按 span 长度 + category 优先级去重 | ❌ 未知 | ❌ 未知 |
| **Unicode 检测** | ✅ 零宽字符 + bidi override | ⚠️ 基础 | ⚠️ 基础 |
| **Persona 清洗** | ✅ `sanitize_for_prompt()` 完整扫描 | ⚠️ 基础 | ✅ 上下文文件扫描 |
| **模型特化纪律** | ✅ GPT/Claude/Gemini 各自提示 | ❌ 无 | ⚠️ 基础 |
| **平台适配提示词** | ✅ 10 通道渲染指导 | ❌ 无 | ❌ 无 |

### 7.3 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **安全层数** | 5 | 8+ | 7 | XMclaw 落后 |
| **设备配对** | ❌ 无 | ✅ Challenge nonce | ❌ 配对码 | OpenClaw 领先 |
| **Cascading policy** | ⚠️ 简单 allow/deny | ✅ Global→agent→channel→sandbox | ⚠️ 基础 | OpenClaw 领先 |
| **Undo 机制** | ✅ UndoCabinet | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **中文注入防御** | ✅ 19 个中文模式 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **供应链检测** | ✅ curl\|bash / 未固定依赖 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **C2/Promptware** | ✅ brainworm / heartbeat / register | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Tirith 扫描** | ❌ 无 | ❌ 无 | ✅ 预执行命令扫描 | Hermes 领先 |
| **沙箱内执行** | ⚠️ 有后端但默认本地 | ✅ Docker 默认 | ✅ Docker 默认 | XMclaw 落后 |

---

## 8. System Prompt Assembly 深度对比

### 8.1 组装策略

**XMclaw** — Slot-based，缓存友好：
```
Slot 0: DEFAULT_IDENTITY_LINE ("You are XMclaw...")
Slot 0.5: 当前后端 ground-truth
Slot 1: Bootstrap prefix (BOOTSTRAP.md)
Slot 2: Persona files (SOUL/IDENTITY/USER/AGENTS/TOOLS/MEMORY)
Slot 2.5: Provider-family operational guidance
Slot 2.6: Channel-specific rendering guidance
Slot 3: Platform hint (OS / shell / home / Desktop)
Slot 4: Tools digest
```
- `CACHE_BREAKPOINT_MARKER` 分割缓存边界（stable prefix 缓存，mutable tail 不缓存）
- Mtime-based 缓存，bump_prompt_freeze_generation() 全局失效
- `v2_renderer.py`: 从 MemoryService 事实动态渲染 IDENTITY.md / USER.md

**OpenClaw** — Layered，模式驱动：
```
Layer 1: OpenClaw base prompt
Layer 2: Provider contributions
Layer 3: Bootstrap context (AGENTS.md/SOUL.md/...)
Layer 4: Per-turn dynamic text
Layer 5: Model-specific overlays
```
- Prompt modes: `full` / `minimal` / `none`
- Per-file max: 12,000 chars; Total max: 60,000 chars

**Hermes** — Cached + ephemeral 分离：
```
1. Agent identity (SOUL.md / DEFAULT_AGENT_IDENTITY)
2. Tool-aware behavior guidance
3. Honcho static block
4. Optional system message
5. Frozen MEMORY snapshot
6. Frozen USER profile snapshot
7. Skills index
8. Context files (.hermes.md → AGENTS.md → CLAUDE.md → .cursorrules)
9. Timestamp / session ID
10. Platform hint
```
- 设计原则: cached state 与 ephemeral API-call-time additions 分离，保护 prompt caching

### 8.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **身份锚定** | ✅ "You are XMclaw" + ANTI-HALLUCINATION | ✅ 强身份线 | ✅ DEFAULT_AGENT_IDENTITY | 持平 |
| **模型特化覆盖** | ✅ GPT/Claude/Gemini 纪律 | ✅ GPT-5 family overlay | ⚠️ 基础 | XMclaw/OpenClaw 领先 |
| **平台适配** | ✅ 10 通道渲染指导 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **动态渲染** | ✅ v2_renderer 从 MemoryService | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **Prompt 缓存** | ✅ CACHE_BREAKPOINT_MARKER | ✅ Provider 分割 | ✅ 缓存/动态分离 | 持平 |
| **模式选择** | ❌ 无 | ✅ full/minimal/none | ❌ 无 | OpenClaw 领先 |
| **长度限制** | ❌ 无 | ✅ per-file + total max | ❌ 无 | OpenClaw 领先 |

---

## 9. Session Storage 深度对比

### 9.1 架构

**XMclaw**:
- `SessionStore`: SQLite `session_history` 表
- 字段: `session_id` PK, `history_json`, `message_count`, `updated_at`
- `Message` / `ToolCall` frozen dataclasses JSON 序列化
- WAL mode
- Internal session filtering（隐藏反射/自主/session 列表）

**OpenClaw**:
- File-first: `~/.openclaw/agents/<id>/sessions/<sessionId>.jsonl`
- `sessions.json`: session metadata
- Per-session file-based write lock（60s timeout）
- Daily reset at 4:00 AM; idle reset; manual reset

**Hermes**:
- `SessionDB`: SQLite `~/.hermes/state.db`
- 表: `sessions`, `messages`, `messages_fts`, `messages_fts_trigram`
- FTS5 全文搜索 + trigram 用于 CJK
- Atomic writes（15 retries with jitter）
- Session lineage via `parent_session_id`

### 9.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **存储格式** | SQLite JSON blob | JSONL（git-backable） | SQLite + FTS5 | Hermes 搜索最强 |
| **全文搜索** | ❌ 无 | ❌ 无 | ✅ FTS5 + trigram | Hermes 领先 |
| **写锁** | ❌ 无 | ✅ File-based 60s | ✅ Atomic retry | XMclaw 落后 |
| **会话隔离** | ✅ Per-session | ✅ Per-session lane | ✅ Profile 隔离 | 持平 |
| **会话谱系** | ❌ 无 | ⚠️ Sub-agent parent ref | ✅ parent_session_id | Hermes 领先 |
| **自动重置** | ❌ 无 | ✅ Daily + idle | ❌ 无 | OpenClaw 领先 |

---

## 10. Event Bus / Messaging 深度对比

### 10.1 架构

**XMclaw**:
- `InProcessEventBus`: 内存 pub/sub，async subscribers
- `SqliteEventBus`: 持久化到 SQLite 后再 fanout（crash-safe）
- `BehavioralEvent`: frozen dataclass，60+ EventType
- 订阅者: Grader, JournalWriter, ProfileExtractor, EvolutionOrchestrator, SecurityAuditor

**OpenClaw**:
- WebSocket-based typed protocol
- `{type:"req", id, method, params}` / `{type:"res", id, ok, payload}` / `{type:"event", event, payload}`
- Stream events: lifecycle, assistant deltas, tool start/update/end
- Idempotency cache for side-effecting methods

**Hermes**:
- 回调驱动（非独立 bus）
- `AIAgent` 内部事件通过 callback 传播
- Gateway 层: `MessageEvent` 归一化

### 10.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **事件类型** | ✅ 60+ 精细类型 | ✅ 丰富 | ⚠️ 回调驱动 | XMclaw/OpenClaw 领先 |
| **持久化** | ✅ SqliteEventBus | ❌ 内存 | ❌ 内存 | XMclaw 独一档 |
| **协议** | 自有 WS + REST | 类型化 WS | 回调 | OpenClaw 最规范 |
| **流事件** | ✅ LLM_CHUNK / TOOL_* | ✅ assistant/tool/lifecycle | ⚠️ 基础 | XMclaw/OpenClaw 领先 |
| **Idempotency** | ❌ 无 | ✅ Side-effecting cache | ❌ 无 | OpenClaw 领先 |

---

## 11. Skill / Plugin 系统深度对比

### 11.1 架构

**XMclaw**:
- `SkillRegistry`: 版本化内存存储，HEAD 指针，证据门禁晋升
- `EvolutionOrchestrator`: bus-aware 包装器，auto_apply 门控
- `UserSkillsLoader`: 扫描 `~/.xmclaw/skills_user/` + `~/.agents/skills/`
- `PluginLoader`: `importlib.metadata` entry points (`xmclaw.plugins`)
- 技能格式: Python `skill.py` + Markdown `SKILL.md`
- 渐进披露: inline（一工具一技能）/ unified（browse→view→run）/ auto（>20 切换）

**OpenClaw**:
- Skills = `SKILL.md` 文件（需求加载，避免 token 膨胀）
- ClawHub 公共注册表
- Plugins = `openclaw.plugin.json` 声明契约
- 12 类插件能力: tools, channels, providers, hooks, HTTP routes, CLI commands, services, context engines, compaction providers, memory backends, setup/onboarding, MCP servers
- `api.registerTool()` / `api.registerHook()`

**Hermes**:
- Skills = Markdown `SKILL.md`，`~/.hermes/skills/`
- **自主创建**: 复杂任务后自动创建/更新技能
- `skill_manage` / `skill_view` 工具
- `skills_hub` 发现/安装/管理
- **安全扫描**: skills_guard ~90 正则，可疑技能回滚
- **Autoresearch skill**: Git 分支 → 实验 → 评估 → merge/revert
- **Self-Evolution Repo**: 独立仓库 `hermes-agent-self-evolution`，输出 PR

### 11.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **版本化** | ✅ HEAD + promote/rollback + history | ⚠️ 基础 | ⚠️ 基础 | XMclaw 独一档 |
| **证据门禁** | ✅ anti-req #12 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **自主创建** | ❌ 无 | ❌ 无 | ✅ 经验驱动 | Hermes 独一档 |
| **自进化** | ✅ UCB1 + HonestGrader | ❌ 无 | ✅ 自主改进 | XMclaw/Hermes 领先 |
| **技能市场** | ⚠️ 基础 marketplace | ✅ ClawHub | ⚠️ Skills Hub | OpenClaw 领先 |
| **插件生态** | ⚠️ Entry points | ✅ 12 类 + npm | ✅ 注册表 + check_fn | OpenClaw/Hermes 领先 |
| **渐进披露** | ✅ inline/unified/auto | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Autoresearch** | ❌ 无 | ❌ 无 | ✅ Git 实验循环 | Hermes 独一档 |

---

## 12. Multi-Agent 深度对比

### 12.1 架构

**XMclaw**:
- `MultiAgentManager`: 已存在但未完全启用
- `AgentInterTools`: `chat_with_agent` / `submit_to_agent` 工具
- Sub-agent injection source (B-273) 已扫描
- 认知层: `delegate_task` 规划步骤支持 `subagent` action_kind
- 实际状态: 代码存在，但主流程仍是单 AgentLoop

**OpenClaw**:
- **Agent**: identity + SOUL.md + workspace + model config + tool policy
- **Workspace**: `~/.openclaw/workspace`
- **Bindings**: route channels/accounts/peers → specific agents
- **Sub-agents**: `sessions_spawn` 委托任务，隔离 session，可回传
- 每个 agent 有独立 `agentDir`, `sessions.json`, transcripts

**Hermes**:
- `delegate_task` 工具: 生成子 `AIAgent` 实例
- 单任务 / 并行批量（最多 3 并发）
- 子 agent 零父历史，独立终端 session
- 仅最终摘要进入父上下文
- 中断传播: 中断父 → 中断所有子
- 叶节点禁止: `delegate_task`, `clarify`, `memory`, `send_message`
- `max_spawn_depth=1`（默认平级）

### 12.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **多 Agent 路由** | ⚠️ 代码存在未启用 | ✅ Workspace + Bindings | ⚠️ delegate_task | OpenClaw 领先 |
| **子 Agent 隔离** | ⚠️ 基础 | ✅ 独立 session | ✅ 零历史 + 独立终端 | Hermes 领先 |
| **并发子 Agent** | ❌ 无 | ⚠️ 基础 | ✅ ThreadPoolExecutor 3 | Hermes 领先 |
| **中断传播** | ❌ 无 | ⚠️ 基础 | ✅ 父→子传播 | Hermes 领先 |
| **Agent 配置隔离** | ⚠️ 基础 | ✅ 独立 SOUL/模型/策略 | ⚠️ 基础 | OpenClaw 领先 |

---

## 13. Sandbox / Runtime 深度对比

### 13.1 后端对比

| 后端 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| Local | ✅ | ✅ (local user proposed) | ✅ |
| Docker | ✅ | ✅ (default) | ✅ |
| SSH | ✅ | ✅ | ✅ |
| Singularity | ❌ | ❌ | ✅ |
| Modal | ✅ | ❌ | ✅ |
| Daytona | ✅ | ❌ | ✅ |
| Vercel | ✅ | ❌ | ✅ |
| OpenShell | ❌ | ✅ | ❌ |
| Process | ✅ | ❌ | ❌ |

### 13.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **后端数量** | 6 | 3 | 7 | Hermes 最全 |
| **Serverless** | ✅ Modal + Daytona | ❌ | ✅ Modal + Daytona | XMclaw/Hermes 领先 |
| **默认隔离** | ❌ 本地（默认） | ✅ Docker（默认） | ✅ Docker（默认） | XMclaw 落后 |
| **Scope** | agent/session/shared | agent/session/shared | 按工具 | 持平 |
| **Browser 沙箱** | ⚠️ 主机运行 | ✅ 容器内 CDP | ⚠️ 主机运行 | OpenClaw 领先 |

---

## 14. Self-Improvement / Evolution 深度对比

### 14.1 架构

**XMclaw — 数学化进化引擎**
- `OnlineScheduler`: UCB1 bandit over candidates
- `HonestGrader`: 双信号评分（Iron Rule #1）
  - Signal A: deterministic（ran/returned/type_matched/side_effect）
  - Signal B: independent（user_followup/holdout_test/cross_judge）
  - 仅当 ≥2 信号且均达标时才 promote_eligible
- `EvolutionController`: 保守晋升策略（4 阈值）
  - min_plays ≥ 10, min_mean ≥ 0.65, gap_over_head ≥ 0.05, gap_over_second ≥ 0.03
- `SkillRegistry`: 版本化 + HEAD + evidence 门禁
- `EvolutionOrchestrator`: bus-aware，auto_apply 门控
- `ReflectiveMutator`: LLM 驱动的策略变异
- `StrategyBank`: 策略库 + `StrategyDistiller`

**OpenClaw**:
- ❌ 无内置进化系统
- 依赖外部插件或用户手动调优

**Hermes — 经验驱动技能进化**
- **自主技能创建**: 复杂任务后生成 `SKILL.md`
- **技能自改进**: 使用中自动优化
- **Autoresearch**: Git 分支实验循环
- **Self-Evolution Repo**: 独立仓库输出 PR（5 阶段: skill→tool→prompt→code→monitor）
- 无数学化评分/晋升机制

### 14.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **数学化评分** | ✅ UCB1 + 多信号 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **证据门禁** | ✅ anti-req #12 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **自主技能创建** | ❌ 无 | ❌ 无 | ✅ 经验驱动 | Hermes 独一档 |
| **策略变异** | ✅ ReflectiveMutator | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Autoresearch** | ❌ 无 | ❌ 无 | ✅ Git 实验循环 | Hermes 独一档 |
| **RL 训练** | ❌ 无 | ❌ 无 | ✅ Tinker-Atropos | Hermes 独一档 |

---

## 15. Frontend / UI 深度对比

### 15.1 架构

**XMclaw**:
- **Web UI**: Preact + htm，22 页，静态文件通过 FastAPI 提供
- **TUI**: Textual（Python），`xmclaw chat` 交互式 REPL
- **状态**: VAD 死代码，移动端 CSS 问题，5 个页面无导航

**OpenClaw**:
- **Control UI**: Vite + React/TS，三面板布局
- **WebChat**: 独立聊天界面
- **A2UI**: 可视化工作区，Canvas 实时协作

**Hermes**:
- **TUI**: Ink（React for terminal）+ `tui_gateway/` Python JSON-RPC 后端
- **Web**: 无内置 Web UI（纯 CLI/Gateway）
- **IDE**: ACP 适配器（VS Code/Zed/JetBrains）

### 15.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **Web UI** | ⚠️ Preact 基础 | ✅ Vite React SPA | ❌ 无 | OpenClaw 领先 |
| **TUI** | ✅ Textual | ❌ 无 | ✅ Ink React | 持平 |
| **IDE 集成** | ⚠️ ACP stub | ❌ 无 | ✅ ACP 完整 | Hermes 领先 |
| **Canvas/可视化** | ⚠️ 基础 | ✅ A2UI | ❌ 无 | OpenClaw 领先 |
| **移动端** | ❌ 问题多 | ✅ 响应式 | ❌ 无 | OpenClaw 领先 |

---

## 16. Deployment / Ops 深度对比

### 16.1 架构

**XMclaw**:
- `xmclaw serve`: 前台 uvicorn
- `xmclaw start`: 后台子进程 + PID 文件
- `xmclaw stop`: PID 文件 kill
- Config: JSON + env `XMC__*`
- Backup: `xmclaw backup create/restore`
- 无 Docker / Nix / systemd 官方支持

**OpenClaw**:
- `openclaw gateway`: 前台/后台
- `openclaw onboard --install-daemon`: OS 服务安装
- 支持: npm global / Docker / Nix / systemd / launchd / Windows Task
- 热配置重载

**Hermes**:
- `hermes gateway start`: 长期运行
- `hermes chat`: 交互式
- `hermes acp`: IDE 服务器
- Profile 隔离: 每个 profile 独立 `HERMES_HOME`

### 16.2 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **OS 服务** | ❌ 无 | ✅ launchd/systemd/Windows | ❌ 无 | OpenClaw 领先 |
| **Docker** | ⚠️ 有 Dockerfile | ✅ 官方镜像 | ⚠️ 基础 | OpenClaw 领先 |
| **Nix** | ❌ 无 | ✅ | ❌ 无 | OpenClaw 领先 |
| **热重载** | ✅ ConfigFileWatcher | ✅ 热配置 | ⚠️ 基础 | 持平 |
| **备份** | ✅ 内置 backup/restore | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **Profile 隔离** | ⚠️ 基础 | ✅ Per-agent workspace | ✅ Per-profile HERMES_HOME | OpenClaw/Hermes 领先 |

---

## 17. 综合差距矩阵

### 17.1 按维度评分（5分制）

| 维度 | XMclaw | OpenClaw | Hermes | XMclaw 差距 |
|------|--------|----------|--------|-------------|
| Agent Loop 丰富度 | 5 | 4 | 4 | +1 |
| LLM Provider 生态 | 3 | 5 | 5 | -2 |
| Tool 系统 | 5 | 4 | 5 | 0 |
| 通道数量 | 2 | 5 | 4 | -3 |
| 记忆系统 | 5 | 4 | 3 | +1 |
| 安全防御 | 4 | 5 | 4 | -1 |
| Prompt 组装 | 5 | 4 | 3 | +1 |
| Session 管理 | 3 | 4 | 4 | -1 |
| Event Bus | 5 | 4 | 2 | +1 |
| 技能/插件 | 4 | 5 | 5 | -1 |
| 多智能体 | 2 | 5 | 4 | -3 |
| 沙箱执行 | 4 | 3 | 5 | -1 |
| 自进化 | 5 | 1 | 4 | +1 |
| 前端 UI | 3 | 5 | 3 | -2 |
| 部署运维 | 2 | 5 | 3 | -3 |
| **总分** | **55** | **63** | **58** | **-8 vs OC, -3 vs Hermes** |

### 17.2 不可复制的差异化优势（XMclaw 独有）

1. **UCB1 + HonestGrader 数学化进化**: 全球唯一带双信号评分和证据门禁的技能晋升系统
2. **认知架构完整代码**: PerceptionBus → AttentionFilter → ReasoningEngine → Planner → ActionDispatcher 全实现
3. **中文原生**: 19 个中文注入模式、CJK token 估计、中文 persona 模板
4. **UndoCabinet**: 文件操作撤销栈
5. **渐进披露技能系统**: inline/unified/auto 三种模式
6. **V2 记忆图**: LanceDB 关系边 + 实体提取 + 自动升级

### 17.3 最致命的结构性短板

1. **"代码完成 ↔ 运行时未接线"**: CognitiveDaemon、Planner、ReasoningEngine 等模块代码完整但默认不运行
2. **默认全 OFF**: `autonomy_level=0`, `evolution.enabled=false`（虽然 v1.1 改为 true，但 auto_apply 仍为 false）
3. **通道数量严重不足**: 7 vs 25+，缺少 WhatsApp/Signal/iMessage/QQ 等主流通道
4. **无设备配对安全**: OpenClaw 的 challenge nonce 签名体系缺失
5. **无 OpenAI-compatible API**: 生态位关键缺失，现有客户端无法直接调用
6. **无 Docker 默认隔离**: 沙箱后端存在但默认本地执行
7. **IDE 集成薄弱**: ACP 只有 stub，无完整 IDE 适配器
8. **无自主技能创建**: Hermes 的经验驱动技能生成能力缺失

---

## 18. 优先级建议

### P0 — 阻塞性差距（6 个月内必须关闭）

| # | 差距 | 目标 | 工作量 | 依赖 |
|---|------|------|--------|------|
| 1 | **接线 CognitiveDaemon** | 将感知-推理-规划-执行闭环真正跑起来 | 2-3 周 | 无 |
| 2 | **默认配置全开** | `autonomy_level=50`, `evolution.auto_apply=true`（或至少文档引导） | 1 天 | 无 |
| 3 | **OpenAI-compatible API** | `/v1/chat/completions` 兼容层，让现有客户端零改动接入 | 2-3 周 | FastAPI 已有 |
| 4 | **增加通道** | WhatsApp (Baileys) + Signal + iMessage，至少达到 15 个 | 4-6 周 | ChannelAdapter 抽象已就绪 |
| 5 | **设备配对安全** | Challenge nonce 签名 + loopback auto-approve | 2 周 | 无 |

### P1 — 重大竞争力差距（6-12 个月）

| # | 差距 | 目标 | 工作量 |
|---|------|------|--------|
| 6 | **Docker 默认隔离** | 工具执行默认在 Docker 容器内 | 2-3 周 |
| 7 | **ACP 完整实现** | VS Code / Zed / JetBrains IDE 适配器 | 4-6 周 |
| 8 | **自主技能创建** | 复杂任务后自动提取 SKILL.md（对标 Hermes） | 4-6 周 |
| 9 | **LLM Provider 扩展** | Ollama / xAI / DeepSeek 原生支持 + auth 轮换 | 2-3 周 |
| 10 | **部署运维** | Docker 官方镜像 + systemd/launchd 单元文件 + Nix flake | 3-4 周 |

### P2 — 差异化增强（12 个月以上）

| # | 差距 | 目标 | 工作量 |
|---|------|------|--------|
| 11 | **RL 训练环境** | Tinker-Atropos 集成或自建轨迹压缩 + 训练数据生成 | 2-3 月 |
| 12 | **Web UI 重写** | Vite + React/TS 三面板（对标 OpenClaw） | 2-3 月 |
| 13 | **多 Agent 路由** | Workspace + Bindings + Sub-agent 完整启用 | 2-3 月 |
| 14 | **技能市场** | ClawHub 对接或自建市场 | 1-2 月 |
| 15 | **语音能力** | TTS + STT + 语音唤醒（对标 OpenClaw） | 2-3 月 |

---



---

## 19. 数据流补充 — OpenClaw 与 Hermes

> 原报告 4.1/4.2 仅覆盖 XMclaw。本章节补充 OpenClaw 与 Hermes 的核心数据流。

### 19.1 OpenClaw 数据流

`
User message (any channel)
    │
    ▼
Gateway WS handler
    │
    ├── Auth: PairingAuthMiddleware / DeviceAuth / Tailscale
    │
    ▼
Channel Adapter normalize → MessageEvent
    │
    ▼
Auto-reply pipeline: steer / followup / collect / interrupt
    │
    ▼
Session Lane: acquire file-based write lock (60s timeout)
    │
    ▼
Agent resolution: bindings → workspace → agent config
    │
    ▼
runEmbeddedAgent()
    │
    ├── Load session history (JSONL)
    ├── Load skills snapshot (demand-loaded)
    ├── Build system prompt (layered: base + provider + bootstrap + dynamic + overlay)
    ├── Resolve model + auth profile (with cooldown/failover)
    │
    ▼
    LLM streaming call
    │
    ├── If tool_calls:
    │   ├── Cascading policy check (global→agent→channel→sandbox)
    │   ├── before_tool_call hooks
    │   ├── Execute in sandbox (Docker/SSH/OpenShell) or host
    │   ├── after_tool_call hooks
    │   └── Feed back → loop (maxChainLength=10)
    │
    ├── If CONTINUE_WORK detected: auto-append continue message
    │
    └── Reply shaping + NO_REPLY filter
    │
    ▼
Write JSONL transcript
Release session lock
    │
    ▼
Outbound via ChannelAdapter
`

**关键差异**：
- **Session Lane 串行化**：OpenClaw 使用文件锁保证单写者，彻底避免竞态；XMclaw 用 asyncio.Lock 仅保证同进程内串行
- **Demand-loaded skills**：SKILL.md 按需加载，避免 token 膨胀；XMclaw 在 turn 开始时加载全部可用技能
- **Cascading policy**：四层策略叠加（global→agent→channel→sandbox）；XMclaw 只有单层 allow/deny

### 19.2 Hermes 数据流

`
User input (CLI / Gateway / ACP)
    │
    ▼
AIAgent.run_conversation()
    │
    ├── Generate task_id
    ├── Append user msg to history
    ├── Build / reuse cached system prompt
    ├── Preflight compression (>50% context)
    ├── Build API messages (format conversion)
    │
    ▼
_interruptible_api_call()
    │
    ├── Background thread: HTTP request to LLM
    ├── Main thread: monitor interrupt event
    │
    ▼
Parse response
    │
    ├── tool_calls → model_tools.handle_function_call()
    │   ├── Single tool: synchronous execution (main thread)
    │   ├── Multiple tools: ThreadPoolExecutor concurrent (max 3)
    │   ├── Fallback chain on 429/5xx/401
    │   └── Feed results back
    │
    └── Text: persist + return
    │
    ▼
Post-turn: update session_db, emit events
`

**关键差异**：
- **同步核心 + 并发工具**：Hermes 的 AIAgent 是同步类，工具并发通过 ThreadPoolExecutor；XMclaw 全 async，但工具串行
- **可中断 API 调用**：专门的 _interruptible_api_call 机制；XMclaw 用 syncio.Event + syncio.wait_for
- **Fallback chain**：Provider 自动切换；XMclaw 需要显式配置多个 profile

---

## 20. 关键抽象补充 — OpenClaw 与 Hermes

### 20.1 OpenClaw 核心抽象

| 抽象 | 文件 | 说明 |
|------|------|------|
| EmbeddedPiAgent | src/agents/pi-embedded-runner/ | Agent 运行时核心，管理 session、history、LLM 调用 |
| ModelRegistry | src/models/ | 模型能力表（context window, tool support, image support, pricing） |
| AuthProfile | src/auth/ | 多 API key 管理 + cooldown + failover |
| MessageEvent | src/channels/ | 统一消息事件归一化层 |
| SessionLane | src/agents/session/ | 文件锁保证 session 单写者 |
| ToolPolicy | src/agents/pi-tools.policy.ts | Cascading allow/deny 策略 |
| Sandbox | src/agents/sandbox.ts | Docker/SSH/OpenShell 执行后端 |
| PluginAPI | src/plugin-sdk/ | 12 类插件能力注册接口 |
| Gateway | src/gateway/ | WebSocket + HTTP 网关，类型化协议 |

### 20.2 Hermes 核心抽象

| 抽象 | 文件 | 说明 |
|------|------|------|
| AIAgent | 
un_agent.py | 单体核心类，承载对话循环、prompt 构建、压缩、API 调用 |
| PromptBuilder | gent/prompt_builder.py | 多阶段 prompt 组装（identity + tools + memory + context） |
| ContextCompressor | gent/context_compressor.py | 5 阶段压缩管道 |
| ToolRegistry | 	ools/registry.py | AST 自发现 + check_fn 缓存 |
| SessionDB | session_db.py | SQLite + FTS5 + trigram |
| MCPClient / MCPServer | 	ools/mcp_tool.py | 双向 MCP 集成 |
| BaseAdapter | gateway/adapters/base.py | 通道适配器基类 |
| ACPServer | cp_adapter/server.py | IDE 集成 JSON-RPC |
| TirithScanner | security/tirith.py | 预执行命令安全扫描 |
| Autoresearch | skills/autoresearch.py | Git 分支实验循环 |

---

## 21. CLI 命令体系对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **CLI 框架** | Typer | Commander.js | argparse + 手工子命令分发 |
| **命令数量** | ~15 (serve/start/stop/chat/doctor/onboard/ackup/config/evolution/security/skills/gents/memory/cron/eval) | ~10 (gateway/chat/gents/channels/skills/config/logs/update) | ~12 (chat/gateway/cp/config/skills/memory/eval/	rain/export/import) |
| **交互模式** | xmclaw chat → WebSocket REPL + TUI | openclaw chat → 连接 Gateway | hermes chat → 直接 AIAgent |
| **Daemon 控制** | serve/start/stop/
estart | gateway (fg/bg) + OS service install | gateway start/stop |
| **诊断工具** | xmclaw doctor — 离线全量诊断 | openclaw doctor — 配置检查 | hermes doctor — 依赖检查 |
| **首次引导** | xmclaw onboard — 交互式 wizard | openclaw onboard — 配置生成 | hermes onboard — 配置生成 |
| **备份恢复** | xmclaw backup create/restore/list | ❌ 无内置 | ❌ 无内置 |
| **配置热重载** | xmclaw config reload | 自动（5s poll） | hermes config reload |
| **技能管理** | xmclaw skills list/promote/rollback | openclaw skills install/uninstall | hermes skills list/install/create |
| **Agent 管理** | xmclaw agents list/create/delete | openclaw agents create/delete | ❌ 无 |
| **记忆管理** | xmclaw memory search/put/dedup/compact | ❌ 无 | hermes memory search |
| **评估** | xmclaw eval run (SWE-bench/terminal) | ❌ 无 | hermes eval |
| **安全审计** | xmclaw security audit | ❌ 无 | ❌ 无 |
| **Evolution** | xmclaw evolution status/propose/apply | ❌ 无 | ❌ 无 |

**XMclaw 优势**：doctor、ackup、security audit、evolution 是独有命令，诊断和运维能力最强。
**XMclaw 短板**：无 install-service 一键部署命令；无 update 自升级命令。

---

## 22. 配置管理深度对比

### 22.1 配置来源与优先级

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **主配置文件** | daemon/config.json | openclaw.json | ~/.hermes/config.yaml |
| **用户配置** | ~/.xmclaw/config.json | ~/.openclaw/openclaw.json | ~/.hermes/config.yaml |
| **环境变量** | XMC__* (双下划线嵌套) | OPENCLAW_* | HERMES_* |
| **Secret 解析** | ${secret:NAME} 占位符 | ❌ 无 | ❌ 无 |
| **配置验证** | ❌ 无 Schema 验证 | ✅ JSON Schema | ⚠️ 基础 |
| **热重载** | ✅ 5s poll mtime | ✅ 5s poll mtime | ⚠️ 基础 |
| **类型安全** | ❌ dict 裸访问 | ✅ TypeScript 类型 | ⚠️ YAML 解析 |
| **分层覆盖** | 文件 → env | 文件 → env | 文件 → env |

### 22.2 热重载实现

**XMclaw**:
`python
# xmclaw/daemon/config_watcher.py
class ConfigFileWatcher:
    def __init__(self, path: Path, cfg: dict):
        self._path = path
        self._cfg = cfg
        self._last_mtime = 0

    async def run(self):
        while True:
            await asyncio.sleep(5)
            mtime = self._path.stat().st_mtime
            if mtime > self._last_mtime:
                self._last_mtime = mtime
                new_cfg = json.loads(self._path.read_text())
                self._cfg.clear()
                self._cfg.update(new_cfg)
                # Publish CONFIG_RELOADED to bus
`

**OpenClaw**:
- 类似 poll 机制，但通过插件 API 暴露 onConfigChange hook
- 每个插件可以订阅配置变更并自更新

**Hermes**:
- 配置在启动时加载，热重载支持有限
- 部分模块（如 prompt builder）支持运行时刷新

### 22.3 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **Secret 管理** | ✅ ${secret:NAME} | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Schema 验证** | ❌ 无 | ✅ JSON Schema | ⚠️ 基础 | XMclaw 落后 |
| **配置类型** | ⚠️ dict | ✅ TS 类型 | ⚠️ YAML | OpenClaw 领先 |
| **配置文档** | ✅ example.json 注释丰富 | ✅ 自动生成 | ⚠️ 基础 | XMclaw/OpenClaw 领先 |
| **Profile 隔离** | ⚠️ 基础 | ✅ Per-agent workspace | ✅ Per-profile HERMES_HOME | OpenClaw/Hermes 领先 |

---

## 23. 错误处理 / 恢复机制对比

### 23.1 错误分类与处理策略

| 错误类型 | XMclaw | OpenClaw | Hermes |
|----------|--------|----------|--------|
| **LLM 超时** | syncio.wait_for(300s) + 取消 | Provider timeout + AbortController | 可中断后台线程 |
| **LLM 429/5xx** | 无自动重试 | 无自动重试 | ✅ Fallback provider 自动切换 |
| **Tool 超时** | syncio.wait_for(180s) | 无显式超时 | 线程 join timeout |
| **Tool 失败** | ToolResult.error + 继续 | 同 | 同 |
| **Sandbox 崩溃** | ❌ 无恢复 | ✅ 自动重启容器 | ✅ 自动重启容器 |
| **Session 损坏** | ❌ 无检测 | JSONL parse error → 重置 | SQLite corruption → 重建 FTS |
| **内存溢出** | ❌ 无保护 | ⚠️ 基础 | ✅ ContextCompressor 自动触发 |
| **Config 损坏** | ❌ 启动失败 | ⚠️ 基础 | ⚠️ 基础 |
| **Bus 崩溃** | SqliteEventBus 持久化 | ❌ 内存丢失 | ❌ 内存丢失 |
| **Daemon 崩溃** | ❌ 无自动重启 | OS service 自动重启 | ❌ 无 |

### 23.2 恢复机制

**XMclaw**:
- SessionStore WAL mode: 崩溃后数据不丢
- SqliteEventBus: 事件持久化，崩溃后可回放
- UndoCabinet: 文件操作撤销
- 无自动重启、无 fallback provider

**OpenClaw**:
- OS service (systemd/launchd) 自动重启
- Session Lane 文件锁: 崩溃后锁自动释放（进程退出）
- JSONL append-only: 部分写入可截断恢复

**Hermes**:
- allback_providers: LLM 失败时自动切换
- max_turns + max_spawn_depth: 防止无限递归
- SessionDB atomic writes (15 retries)
- ContextCompressor: 内存不足时自动压缩

### 23.3 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **Fallback provider** | ❌ 无 | ❌ 无 | ✅ 自动切换 | Hermes 领先 |
| **自动重启** | ❌ 无 | ✅ OS service | ❌ 无 | OpenClaw 领先 |
| **Undo 机制** | ✅ UndoCabinet | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **事件持久化恢复** | ✅ SqliteEventBus | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **Sandbox 崩溃恢复** | ❌ 无 | ✅ 自动重启 | ✅ 自动重启 | XMclaw 落后 |
| **内存保护** | ⚠️ 基础 | ⚠️ 基础 | ✅ Compressor | Hermes 领先 |
| **配置损坏恢复** | ❌ 无 | ⚠️ 基础 | ⚠️ 基础 | 持平 |

---

## 24. 可观测性 / 日志 / 监控对比

### 24.1 日志体系

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **日志格式** | JSON structured (structlog) | Plain text + JSON option | Plain text |
| **日志级别** | DEBUG/INFO/WARNING/ERROR | DEBUG/INFO/WARN/ERROR | DEBUG/INFO/WARNING/ERROR |
| **日志目标** | File + stderr | File + stderr | File + stderr |
| **日志轮转** | ✅ 按大小轮转 | ✅ 按天轮转 | ❌ 无 |
| **Correlation ID** | ✅ per-turn correlation_id | ❌ 无 | ❌ 无 |
| **Cost tracking** | ✅ CostTracker per turn | ⚠️ 基础 | ⚠️ 基础 |
| **Metrics** | ⚠️ 基础（事件计数） | ❌ 无 | ❌ 无 |
| **Tracing** | ⚠️ 基础（EventBus 时间戳） | ❌ 无 | ❌ 无 |
| **Dashboard** | ✅ dashboard.html (22 页 Preact) | ✅ Control UI analytics | ❌ 无 |
| **Health check** | ✅ /health + /ready | ✅ /health | ❌ 无 |

### 24.2 事件可观测性

**XMclaw**:
- BehavioralEvent 包含 correlation_id, parent_id, 	s
- 可以通过 bus 回放完整会话历史
- dashboard.html 实时显示：session 列表、消息流、工具调用、成本、事件流

**OpenClaw**:
- Stream events over WebSocket: lifecycle, assistant deltas, tool updates
- 无持久化事件存储，仅内存流

**Hermes**:
- Callback-driven events，无独立事件系统
- session_db 记录消息，但无结构化事件

### 24.3 差距

| 能力 | XMclaw | OpenClaw | Hermes | 评估 |
|------|--------|----------|--------|------|
| **结构化日志** | ✅ JSON | ⚠️ 可选 | ❌ 纯文本 | XMclaw 领先 |
| **Correlation ID** | ✅ 全链路 | ❌ 无 | ❌ 无 | XMclaw 独一档 |
| **成本追踪** | ✅ per-turn | ⚠️ 基础 | ⚠️ 基础 | XMclaw 领先 |
| **Dashboard** | ✅ Preact 22页 | ✅ Vite React | ❌ 无 | XMclaw/OpenClaw 领先 |
| **事件持久化** | ✅ SqliteEventBus | ❌ 内存 | ❌ 内存 | XMclaw 独一档 |
| **Metrics/Tracing** | ⚠️ 基础 | ❌ 无 | ❌ 无 | XMclaw 领先 |
| **Prometheus 导出** | ❌ 无 | ❌ 无 | ❌ 无 | 三方均缺失 |

---

## 25. 国际化 (i18n) 对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **系统语言** | 中文优先（ persona、日志、CLI 输出） | 英文 | 英文 |
| **i18n 框架** | ✅ xmclaw/utils/i18n.py (gettext) | ❌ 无 | ❌ 无 |
| **支持语言** | zh-CN / en | en only | en only |
| **Persona 本地化** | ✅ SOUL.md / IDENTITY.md 中文版 | ❌ 英文 | ❌ 英文 |
| **通道本地化** | ✅ 各通道消息模板本地化 | ❌ 无 | ⚠️ 基础 |
| **CLI 本地化** | ✅ 中文命令帮助 | ❌ 英文 | ❌ 英文 |
| **Date/Time 本地化** | ⚠️ 基础 | ⚠️ 基础 | ⚠️ 基础 |
| **RTL 支持** | ❌ 无 | ❌ 无 | ❌ 无 |

**XMclaw 独一档**：唯一完整支持中文的系统 persona、CLI 输出、日志、通道消息。这是中文市场的核心差异化优势。

---

## 26. 测试策略对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **测试框架** | pytest | Jest + Vitest | pytest |
| **测试数量** | ~200 单元测试 | ~500 单元测试 | ~250 单元测试 |
| **测试覆盖率** | ⚠️ ~60%（security/core 高，daemon 低） | ⚠️ ~50% | ✅ ~75% |
| **集成测试** | ✅ 	ests/integration/ | ✅ E2E tests | ✅ 	ests/integration/ |
| **E2E 测试** | ⚠️ 基础 | ✅ Playwright | ⚠️ 基础 |
| **混沌测试** | ✅ 	ests/chaos/ | ❌ 无 | ❌ 无 |
| **性能测试** | ✅ 	ests/bench/ | ❌ 无 | ⚠️ 基础 |
| **契约测试** | ❌ 无 | ✅ Plugin SDK 契约 | ❌ 无 |
| **Mock 策略** | _ScriptedLLM, InProcessEventBus | MSW (API mock) | _MockLLM |
| **CI/CD** | ✅ GitHub Actions (lint + test) | ✅ GitHub Actions | ✅ GitHub Actions |
| **Pre-commit** | ✅ ruff + mypy | ✅ ESLint + Prettier | ✅ ruff |
| **类型检查** | ✅ mypy (渐进式) | ✅ TypeScript 严格 | ✅ mypy |

### 26.1 XMclaw 测试短板

| 模块 | 测试状态 | 缺失 |
|------|----------|------|
| session_store.py | ⚠️ 2 失败 | save() 未被 AgentLoop 调用 |
| channel_dispatcher.py | ❌ 无 | platform guidance wiring 无覆盖 |
| cognitive_daemon.py | ❌ 无 | 未接入主流程 |
| actory.py | ❌ 无 | 默认值配置无覆盖 |
| prompt_scanner.py | ✅ 覆盖 | 保持 |
| gent_loop.py | ⚠️ 部分 | 工具并发、成本追踪无覆盖 |

---

## 27. 版本管理 / 发布流程对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **版本号** | PEP 440 (v1.1.0) | SemVer (v0.9.0) | SemVer (v0.5.0) |
| **版本源** | pyproject.toml | package.json | pyproject.toml |
| **Changelog** | ✅ CHANGELOG.md (Keep a Changelog) | ✅ CHANGELOG.md | ⚠️ CHANGELOG.md |
| **Git 标签** | ✅ 带签名 | ✅ | ✅ |
| **发布渠道** | PyPI (计划) | npm registry | PyPI |
| **自动发布** | ❌ 手动 | ❌ 手动 | ❌ 手动 |
| **Nightly** | ❌ 无 | ❌ 无 | ❌ 无 |
| **Docker 镜像** | ⚠️ Dockerfile 存在 | ✅ 官方镜像 | ⚠️ 基础 |
| **签名验证** | ❌ 无 | ❌ 无 | ❌ 无 |
| **SBOM** | ❌ 无 | ❌ 无 | ❌ 无 |

---

## 28. 文档体系对比

| 维度 | XMclaw | OpenClaw | Hermes |
|------|--------|----------|--------|
| **README** | ✅ 详细（中文 + 英文） | ✅ 详细（英文） | ✅ 详细（英文） |
| **Architecture docs** | ✅ docs/architecture/ | ✅ docs/ | ✅ docs/ |
| **API 文档** | ⚠️ 代码内 docstring | ⚠️ 基础 | ⚠️ 基础 |
| **Developer guide** | ✅ CONTRIBUTING.md | ✅ CONTRIBUTING.md | ✅ CONTRIBUTING.md |
| **Security docs** | ✅ SECURITY.md | ✅ SECURITY.md | ✅ SECURITY.md |
| **Persona docs** | ✅ SOUL.md / IDENTITY.md / AGENTS.md | ✅ AGENTS.md / SOUL.md | ✅ SOUL.md / AGENTS.md |
| **Code comments** | ✅ 极丰富（几乎每个函数） | ✅ 丰富 | ✅ 丰富 |
| **ADR** | ✅ docs/architecture/ | ⚠️ 基础 | ⚠️ 基础 |
| **Changelog** | ✅ CHANGELOG.md | ✅ CHANGELOG.md | ✅ CHANGELOG.md |
| **Inline help** | ✅ CLI --help 完善 | ✅ CLI --help 完善 | ✅ CLI --help 完善 |

**XMclaw 优势**：中文文档最完整；代码注释密度最高（几乎每个函数都有中英文 docstring）；架构文档（docs/architecture/）包含详细 ADR。
**XMclaw 短板**：API 文档未生成（无 OpenAPI/Swagger 静态站点）；无在线文档站点。

---

## 附录 A: 关键文件索引

### XMclaw
| 模块 | 关键文件 | 行数 |
|------|----------|------|
| AgentLoop | `xmclaw/daemon/agent_loop.py` | 2580 |
| HopLoop | `xmclaw/daemon/hop_loop.py` | 1532 |
| Factory | `xmclaw/daemon/factory.py` | 2148 |
| App | `xmclaw/daemon/app.py` | 2457 |
| Prompt Scanner | `xmclaw/security/prompt_scanner.py` | ~850 |
| Context Compressor | `xmclaw/context/compressor.py` | 1200 |
| Skill Registry | `xmclaw/skills/registry.py` | 732 |
| Memory Service V2 | `xmclaw/memory/v2/service.py` | 370+ |
| Cognitive Daemon | `xmclaw/cognition/cognitive_daemon.py` | 1075 |
| HTN Planner | `xmclaw/cognition/planner.py` | 1050 |
| Reasoning Engine | `xmclaw/cognition/reasoning.py` | 786 |

### OpenClaw
| 模块 | 关键文件 |
|------|----------|
| Agent Loop | `src/agents/pi-embedded-runner/run.ts` |
| System Prompt | `src/agents/system-prompt.ts` |
| Tool Policy | `src/agents/pi-tools.policy.ts` |
| Sandbox | `src/agents/sandbox.ts` |
| Gateway | `src/gateway/` |
| Plugin SDK | `src/plugin-sdk/` |

### Hermes
| 模块 | 关键文件 | 行数 |
|------|----------|------|
| AIAgent | `run_agent.py` | ~4400 |
| CLI | `cli.py` | ~11000 |
| Prompt Builder | `agent/prompt_builder.py` | ~800 |
| Context Compressor | `agent/context_compressor.py` | ~1230 |
| MCP Tool | `tools/mcp_tool.py` | ~1050 |
| Tool Registry | `tools/registry.py` | ~600 |
| Gateway | `gateway/run.py` | ~800 |
| ACP Adapter | `acp_adapter/server.py` | ~1200 |

---

*报告完成。建议每季度根据三方的最新 release 更新一次本审计。*
