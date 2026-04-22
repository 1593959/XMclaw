# XMclaw 竞品差距分析与开发路线图

> **文档版本**: v1.1（2026-04-22 修订）
> **分析日期**: 2026-04-22
> **对标项目**: OpenClaw (github.com/openclaw/openclaw)、HermesAgent (github.com/NousResearch/hermes-agent)、QwenPaw/CoPaw (github.com/agentscope-ai/QwenPaw)
> **目标**: 集百家之长，避百家之短，明确 XMclaw 距离成熟产品的差距与优先级。
>
> **本文档定位**："为什么"——差距全景、竞品架构深度分析、分阶段策略。
> **配套执行清单**：[DEV_ROADMAP.md](DEV_ROADMAP.md)——"做什么"，带 file:line 证据的 17 Epic + 9 Milestone 工程拆解。两文**必读互补**，不重复。
>
> **修订说明（v1.1）**：
> - OpenClaw Stars 更新为 **362k+**（`gh api` 2026-04-22 实拉，原 250k 数据陈旧）。
> - 前端部分已按"全部删除、终端优先测试"现状订正（见 §3.3 脚注）。
> - Path / 格式 / Milestone 冲突解决详见 [DEV_ROADMAP.md §3 和 §7](DEV_ROADMAP.md)。

---

## 1. 执行摘要

XMclaw 在 **自主进化（Self-Evolution）** 领域具备独特的技术深度——UCB1 在线调度器、HonestGrader 地面真值评分、保守晋升策略——这是三款竞品均不具备的原生能力。然而，在 **产品化成熟度** 上，XMclaw 与竞品存在显著差距：

| 维度 | XMclaw 现状 | 竞品水平 | 差距等级 |
|------|------------|---------|---------|
| 消息通道 | 4 个 (TG/Slack/Discord/Lark) | 20+ (OpenClaw) / 6+ (Hermes) | 🔴 高 |
| 前端 UI | Hermes WebUI 适配层（Mock 大量 API） | 原生 Vite SPA / TUI / Canvas | 🔴 高 |
| 多智能体 | 单 AgentLoop | 多 Workspace 路由 + 子智能体并行 | 🔴 高 |
| 执行沙箱 | 无（仅 opt-in 目录限制） | Docker / SSH / Daytona / Modal | 🔴 高 |
| 工具生态 | 6 个基础工具 | 40+ (Hermes) / 20+ (QwenPaw) | 🟡 中高 |
| 安全体系 | 权限模式 + Ed25519 配对 | 工具守卫 + 文件守卫 + 技能扫描 + Shell 逃逸检测 | 🟡 中高 |
| 技能市场 | 本地 Markdown 加载 | ClawHub / SkillsHub / 自改进循环 | 🟡 中高 |
| 记忆系统 | FileMemoryIndex + sqlite-vec | Honcho + FTS5 + 跨会话召回 + Dreaming | 🟡 中 |
| 部署运维 | `xmclaw serve` / `xmclaw start` | Docker / Nix / systemd / launchd / Windows Service | 🟡 中 |
| API 兼容 | 自有 WS + REST | OpenAI-compatible / ACP / MCP | 🟡 中 |
| 语音能力 | 无 | TTS + STT + 语音唤醒 | 🟢 低（可选）|
| 轨迹/RL | 基础事件日志 | 轨迹压缩 + Tinker-Atropos RL 环境 | 🟢 低 |

**结论**：XMclaw 的 **核心引擎（进化 + 调度 + 评分）** 是差异化优势，但 **产品外壳（通道 + UI + 安全 + 部署）** 距离用户可开箱即用的成熟产品还有 2~3 个里程碑的差距。

---

## 2. 竞品架构深度对比

### 2.1 OpenClaw — 通道与插件之王

**技术栈**: Node.js 22+ / TypeScript / pnpm monorepo / SQLite + sqlite-vec
**Stars**: **362,000+**（2026-04-22 `gh api` 实拉）
**核心设计哲学**: "Gateway 只是控制平面，产品才是助手本身。"

#### 2.1.1 架构亮点

1. **单一 Gateway Daemon**: 一个 Node.js 长进程拥有所有消息面、会话和控制平面状态。默认绑定 `127.0.0.1:18789`。
2. **WebSocket 控制平面**: 类型化 RPC（`req`/`res` + 服务端推送 `event`），首帧必须为 `connect`，支持共享密钥认证、Tailscale 头、可信代理模式。
3. **插件架构**: 核心极简，功能通过 `extensions/` 中的捆绑插件或外部 npm 包添加。插件声明 `openclaw.plugin.json` 元数据。
4. **多智能体路由**: 通过 `bindings` 配置将入站消息按通道/账户/对等方/Guild/团队映射到隔离的 Agent，每个 Agent 拥有独立工作区、认证配置文件和会话存储。
5. **会话隔离**: 会话按 JSONL 转录本组织。DM 可共享或隔离（`session.dmScope`）。群组/房间/Cron 作业获得隔离会话。
6. **沙箱执行**: 工具执行可在 Docker 容器（默认）、SSH 远程或 OpenShell 托管环境中运行，支持 per-Agent 或 per-Session 作用域。
7. **热配置重载**: Gateway 监视配置文件，可热应用安全更改或重启以应用结构性更改。

#### 2.1.2 与 XMclaw 的关键差异

| 能力 | OpenClaw | XMclaw |
|------|----------|--------|
| 通道数量 | 25+ (WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Teams, Matrix, Feishu, LINE, Mattermost, Nostr, Zalo, QQ, WebChat...) | 4 (Telegram, Slack, Discord, Lark) |
| 沙箱后端 | Docker / SSH / OpenShell | 无 |
| 浏览器自动化 | 隔离的 Chrome/Brave/Chromium CDP 控制 | 可选 Playwright，非核心 |
| 实时 Canvas | A2UI 可视化工作区，能力令牌作用域 | 无 |
| 语音 | macOS/iOS 唤醒词 + Android 连续语音 + ElevenLabs TTS | 无 |
| 记忆后端 | SQLite + sqlite-vec (默认) / QMD / LanceDB / Honcho | FileMemoryIndex + sqlite-vec |
| 技能注册表 | ClawHub (`clawhub.ai`) 公共市场 | 本地磁盘加载 |
| API 表面 | OpenAI-compatible (`/v1/chat/completions`, `/v1/embeddings`) | 自有 `/api/v2/*` |
| 前端 | Vite 构建的 Control UI (`ui/`) | Hermes WebUI 适配层 |
| 部署 | npm global / Docker / Nix / systemd / launchd | pip editable / `xmclaw serve` |

#### 2.1.3 XMclaw 应借鉴之处

- **通道即插件**的抽象：OpenClaw 的 `extensions/` 目录让添加新通道只需实现标准化接口，无需修改 Gateway 核心。XMclaw 的 `ChannelAdapter` 基类已具备此雏形，但缺少插件化的发现/加载机制。
- **多智能体路由**：XMclaw 当前是单 AgentLoop 单会话，用户无法在同一 Gateway 上运行多个隔离的 Agent。
- **OpenAI 兼容 API**：这是生态位关键——让现有 OpenAI 客户端无需修改即可调用 XMclaw。
- **沙箱执行**：XMclaw 的 `block` 权限模式只是置空 `_tools`，不是真正的隔离。OpenClaw 的 Docker 沙箱让非主会话在隔离容器中运行，这是企业级安全的基础。

---

### 2.2 HermesAgent — 自改进与终端后端之王

**技术栈**: Python 3.11+ / setuptools / uv / rich / prompt_toolkit  
**Stars**: 新兴（Nous Research 背书）  
**核心设计哲学**: "The self-improving AI agent — creates skills from experience, improves them during use."

#### 2.2.1 架构亮点

1. **闭环学习**: Agent 在复杂任务后自主创建技能，技能在使用中自我改进，定期提示自身持久化知识，搜索自身过往对话，跨会话构建用户模型。
2. **40+ 工具 + 工具集系统**: `toolsets.py` 定义工具集，`toolset_distributions.py` 管理工具集分发。工具按领域分组（开发、研究、创意、MLOps 等）。
3. **六种终端后端**: 本地、Docker、SSH、Daytona、Singularity、Modal。Daytona 和 Modal 提供无服务器持久化——Agent 环境在空闲时休眠，按需唤醒。
4. **子智能体并行**: 生成隔离的子智能体用于并行工作流。编写 Python 脚本通过 RPC 调用工具，将多步管道压缩为零上下文成本的单轮。
5. **轨迹压缩**: `trajectory_compressor.py` 压缩对话轨迹，用于训练下一代工具调用模型。
6. **RL 训练环境**: `tinker-atropos/` 子模块提供 Atropos RL 环境，支持批量轨迹生成。
7. **记忆多样性**: Honcho 方言式用户建模、FTS5 会话搜索、LLM 摘要化跨会话召回。
8. **ACP (Agent Client Protocol)**: `acp_adapter/` + `acp_registry/` 让 Hermes 可作为 ACP 客户端或服务器运行，与 Codex CLI 等外部工具集成。
9. **TUI + Web UI + Gateway**: `tui_gateway/` 提供终端 UI，`web/` 提供 Web 界面，`gateway/` 提供消息网关。

#### 2.2.2 与 XMclaw 的关键差异

| 能力 | HermesAgent | XMclaw |
|------|-------------|--------|
| 自改进技能 | ✅ 从经验创建 + 使用中改进 | ✅ UCB1 调度 + HonestGrader（更数学化） |
| 终端后端 | 6 种（含 serverless） | 本地进程 |
| 工具数量 | 40+ | 6 |
| 子智能体 | 支持并行子 Agent | 无 |
| 轨迹压缩 | 专用模块 | 基础事件日志 |
| RL 训练 | Tinker-Atropos 集成 | 无 |
| 语音 | Edge TTS + faster-whisper STT | 无 |
| MCP | 完整集成 | MCPBridge（stub） |
| ACP | 客户端 + 服务器 | 无 |
| 消息通道 | Telegram, Discord, Slack, WhatsApp, Signal, Email | 4 个 |
| 迁移工具 | 自动从 OpenClaw 迁移 | 无 |
| 图像生成 | fal-client / OpenAI DALL-E 集成 | 无 |
| 网络搜索 | Exa / Firecrawl / parallel-web | DuckDuckGo |

#### 2.2.3 XMclaw 应借鉴之处

- **工具集（Toolset）系统**：XMclaw 的工具是扁平列表，Hermes 的工具按领域分组，用户可按场景启用/禁用整组工具。
- **终端后端多样性**：Daytona/Modal 的无服务器后端让 Agent 可在云端低成本运行。XMclaw 目前只能本地运行。
- **子智能体并行**：XMclaw 的 AgentLoop 是串行的，复杂任务无法分解为并行工作流。
- **轨迹压缩 + RL**：XMclaw 的事件流是原始日志，没有压缩和训练数据提取能力。
- **技能自创建**：XMclaw 的进化是参数调优（调度策略），Hermes 是代码生成（从经验创建新技能文件）。

---

### 2.3 QwenPaw (CoPaw) — 安全与多智能体运维之王

**技术栈**: Python 3.10+ / AgentScope Runtime / FastAPI / Vite TS SPA / APScheduler  
**Stars**: 15,700+  
**核心设计哲学**: "Co Personal Agent Workstation — 温暖的'小肉球'。"

#### 2.3.1 架构亮点

1. **多层安全体系**:
   - `ToolGuardEngine`: 规则 + 文件路径 + Shell 逃逸三层守卫
   - `SkillScanner`: YAML 正则签名检测提示注入、命令注入、硬编码密钥、数据外泄
   - `FilePathToolGuardian`: 限制访问 `~/.ssh`、系统目录等敏感路径
   - `ShellEvasionGuardian`: 检测混淆的 Shell 命令
   - `secret_store.py`: OS keyring + AES 加密密钥存储
   - Web Auth: 可选的 `AuthMiddleware`

2. **零停机重载**: `MultiAgentManager` 创建新 Workspace → 原子交换 → 优雅停止旧实例。服务在升级时零中断。

3. **多智能体管理**: `Workspace` 封装单个 Agent 运行时，`ServiceManager` 按优先级初始化组件。支持跨 Agent 通信工具（`delegate_external_agent`）。

4. **任务模式（Mission Mode）**: 两阶段自主编码：PRD 生成 → 受控执行循环，工具组限制（主 Agent 委托给子 Agent，禁用实现级工具）。

5. **心跳机制**: `HEARTBEAT.md` 定时执行，支持活跃时段过滤，可分发结果到"最后"通道。

6. **备份/恢复**: 编排式停止 → 原子目录交换 → 后台重启。

7. **控制台插件系统**: 前后端插件通过清单驱动加载，前端 JS 通过 `/api/plugins/{id}/files/...` 提供。

8. **技能中心多源**: 统一导入 ClawHub、GitHub、LobeHub、ModelScope、SkillsMP，带重试/退避/缓存。

9. **统一队列管理器**: 每 (通道, 会话, 优先级) 一个 `asyncio.Queue`，批量 draining 和合并。

#### 2.3.2 与 XMclaw 的关键差异

| 能力 | QwenPaw | XMclaw |
|------|---------|--------|
| 安全层数 | 5+ 层（工具守卫 + 文件守卫 + 技能扫描 + Shell 逃逸 + 密钥加密） | 2 层（权限模式 + Ed25519 配对） |
| 零停机重载 | ✅ 原子 Workspace 交换 | ❌ 需重启进程 |
| 多智能体 | ✅ 完全独立的 Workspace | ❌ 单 AgentLoop |
| 任务模式 | ✅ PRD → 执行的两阶段 | ❌ 无 |
| 心跳 | ✅ HEARTBEAT.md + 活跃时段 | ❌ 基础 Cron |
| 备份/恢复 | ✅ 编排式原子恢复 | ❌ 无 |
| 插件系统 | ✅ 前后端插件 | ❌ Phase 2 stub |
| 技能中心 | ✅ 多源统一导入 | ❌ 本地磁盘 |
| 队列管理 | ✅ 统一队列 + 批量合并 | ❌ 简单 pub/sub |
| 自动继续 | ✅ 检测文本-only 响应并提示 | ❌ 无 |
| 媒体回退 | ✅ 主动剥离多模态块 | ❌ 无 |

#### 2.3.3 XMclaw 应借鉴之处

- **安全体系**: XMclaw 的 `block` 模式是"不加载工具"，不是"拦截危险调用"。QwenPaw 的 `ToolGuardEngine` 在 `_acting` 阶段拦截，支持 deny/guard/approve 三态流转。
- **零停机重载**: XMclaw 的配置热重载 (`ConfigReloader`) 只重载配置，代码变更需重启。QwenPaw 的 Workspace 原子交换可实现代码热更新。
- **统一队列**: XMclaw 的 `InProcessEventBus` 是简单 pub/sub，高并发下无反压。QwenPaw 的队列管理器实现了优先级、批量合并和背压。
- **备份/恢复**: XMclaw 无备份机制，配置/记忆丢失风险高。

---

## 3. XMclaw 差距详细分析

### 3.1 架构层差距

#### 3.1.1 单进程 vs 多 Workspace 架构

**现状**: XMclaw 的 `create_app()` 构建单个 FastAPI app，挂载单个 `AgentLoop`、单个 `ChannelManager`、单个 `SkillRegistry`。所有用户共享同一套运行时。

**竞品**: OpenClaw 的 Gateway 支持多 Agent 绑定路由；QwenPaw 的 `MultiAgentManager` 维护 `Dict[str, Workspace]`，每个 Workspace 完全独立；Hermes 支持子 Agent 并行。

**影响**: 无法在同一实例上为不同用户/项目/团队运行隔离的 Agent。企业场景下这是致命缺陷。

**建议**: 引入 `Workspace` 概念，将 `AgentLoop`、`ChannelManager`、`MemoryManager`、`SkillRegistry` 封装为 Workspace 实例。Gateway 负责请求路由。

#### 3.1.2 事件总线 vs 队列管理

**现状**: `InProcessEventBus` 是纯内存 pub/sub，无持久化、无优先级、无背压。高并发或长时间任务可能导致内存溢出。

**竞品**: QwenPaw 的 `UnifiedQueueManager` 为每 (通道, 会话, 优先级) 维护独立 `asyncio.Queue`，支持批量 draining 和合并。OpenClaw 的 Gateway 使用带 idempotency key 的 RPC 模式。

**影响**: 生产环境下面临消息丢失和内存风险。

**建议**: 将事件总线升级为分层队列系统，或至少为通道层引入持久化队列（SQLite/RabbitMQ/Redis 可选）。

#### 3.1.3 配置热重载 vs 零停机重载

**现状**: `ConfigReloader` 检测文件变化后重建 `AgentLoop` 和工具集，但 FastAPI 进程本身不重启。代码变更（如技能文件修改）仍需重启。

**竞品**: QwenPaw 的零停机重载创建新 Workspace → 原子交换 → 旧实例后台清理。OpenClaw 的 `gateway.reload.mode: hybrid` 可热应用安全配置或重启应用结构性变更。

**影响**: 生产部署时更新技能或配置会导致服务中断。

**建议**: 参考 QwenPaw 的 Workspace 原子交换模式，实现配置 + 代码的零停机重载。

---

### 3.2 通道层差距

#### 3.2.1 通道数量与覆盖

| 通道 | XMclaw | OpenClaw | Hermes | QwenPaw |
|------|--------|----------|--------|---------|
| Telegram | ✅ | ✅ | ✅ | ✅ |
| Discord | ✅ | ✅ | ✅ | ✅ |
| Slack | ✅ | ✅ | ✅ | ✅ |
| Lark/Feishu | ✅ | ✅ | ❌ | ✅ |
| WhatsApp | ❌ | ✅ | ✅ | ❌ |
| Signal | ❌ | ✅ | ✅ | ❌ |
| iMessage/BlueBubbles | ❌ | ✅ | ❌ | ❌ |
| Google Chat | ❌ | ✅ | ❌ | ❌ |
| Teams | ❌ | ✅ | ❌ | ❌ |
| Matrix | ❌ | ✅ | ❌ | ✅ |
| LINE | ❌ | ✅ | ❌ | ❌ |
| Mattermost | ❌ | ✅ | ❌ | ❌ |
| Nostr | ❌ | ✅ | ❌ | ❌ |
| Zalo | ❌ | ✅ | ❌ | ❌ |
| QQ | ❌ | ✅ | ❌ | ✅ |
| WeChat | ❌ | ✅ | ❌ | ✅ |
| Email | ❌ | ❌ | ✅ | ❌ |
| MQTT | ❌ | ❌ | ❌ | ✅ |
| SIP/Voice | ❌ | ❌ | ❌ | ✅ |
| WebChat | ❌ | ✅ | ❌ | ✅ (Console) |

**差距**: XMclaw 仅覆盖 4 个通道，缺少 WhatsApp、Signal、Email 等关键通道。WhatsApp 是全球最大的消息平台，缺失意味着覆盖大量用户不可能。

**建议**: 按优先级补充通道：
1. **P0**: WhatsApp（Baileys 库）、Email（IMAP/SMTP）、WebChat（内置 Web UI 聊天）
2. **P1**: Signal（signal-cli）、iMessage（BlueBubbles API）、Matrix（matrix-nio）
3. **P2**: Teams、Google Chat、LINE、Mattermost

#### 3.2.2 通道安全策略

**现状**: XMclaw 的通道适配器没有 DM 配对、允许列表、提及门控等安全机制。任何知道 Bot Token 的人都可以与 Agent 交互。

**竞品**: OpenClaw 的 `dmPolicy`（pairing/allowlist/open）+ 配对码审批。QwenPaw 的 `BaseChannel` 内置 allowlist/mention 策略。Hermes 有平台特定的 DM 安全。

**影响**: 公开部署时面临未授权访问风险。

**建议**: 在 `BaseChannelAdapter` 中增加安全策略钩子：
- `dmPolicy`: `open` / `pairing` / `allowlist`
- 配对码生成与审批端点
- 提及门控（群组中仅当 @bot 时响应）

---

### 3.3 前端层差距

#### 3.3.1 前端架构

**现状（2026-04-22 修订）**: 前端全部已删除。原来挂在 `daemon/app.py` 的 `/ui/` StaticFiles mount、Hermes WebUI 代码、`xmclaw_adapter.js` 适配层均已移除。当前为**终端优先测试**阶段——见 [DEV_ROADMAP.md §1.2 架构决策对照表](DEV_ROADMAP.md#12-架构决策对照)"WebUI 已删除"行。

> **历史上下文（仅供参考）**: 删除前曾用 Hermes WebUI 代码 + `xmclaw_adapter.js` 适配层，适配层 Mock 了大量缺失 API（profiles、workspaces、memory file browser、onboarding）——这些 Mock 正是后续原生 UI 要填实的清单。

**竞品**:
- OpenClaw: Vite + React/TS 构建的 Control UI，与 Gateway 深度集成。
- Hermes: TUI (`tui_gateway/`，基于 `prompt_toolkit` + `rich`) + Web UI (`web/`，React SPA)。
- QwenPaw: Vite + TS SPA (`console/`)，与 FastAPI 后端原生对接。

**差距**:
1. **Mock API 问题**: 适配层对 file browser、workspaces、profiles、memory 返回空数据，用户看到空白面板。
2. **无原生前端**: 借用 Hermes 的 UI 意味着视觉风格、交互模式、甚至部分功能都不属于 XMclaw。
3. **无 TUI**: 竞品都有终端 UI，方便 SSH/无浏览器场景。
4. **无 onboarding**: 新用户首次打开页面没有引导向导。

**建议**:
1. **短期（Phase 1）**: 补全 Mock API 为真实 API，让 Hermes UI 的所有面板正常工作。
2. **中期（Phase 2）**: 开发原生 XMclaw Web UI（Vite + TS，三面板布局），保留 Hermes UI 作为可选主题。
3. **长期（Phase 3）**: 开发 TUI（基于 `rich` + `textual`），支持终端内完整交互。

---

### 3.4 工具层差距

#### 3.4.1 工具数量与分类

**XMclaw 当前工具**:
- `file_read`, `file_write`, `list_dir`
- `bash`
- `web_fetch`, `web_search`
- `todo_write`, `todo_read`
- 可选: `browser_use` (Playwright), `lsp_*`

**HermesAgent 工具集**（40+）:
- **开发**: `git_*`, `github_*`, `docker_*`, `codebase_inspection`, `test_runner`
- **研究**: `exa_search`, `firecrawl_scrape`, `arxiv`, `polymarket`
- **创意**: `image_gen`, `audio_gen`, `video_gen`
- **MLOps**: `huggingface_hub`, `training`, `inference`, `evaluation`
- **生产力**: `notion`, `linear`, `google_workspace`, `email`
- **系统**: `ssh`, `docker_management`, `system_monitor`

**QwenPaw 工具**（20+）:
- `shell`, `file_read/write/edit`, `grep`, `glob`
- `browser_use`, `desktop_screenshot`, `view_media`, `send_file`
- `delegate_to_agent`, `chat_with_agent`

**差距**: XMclaw 的工具集过于基础，无法覆盖开发、研究、创意、生产力等场景。用户需要 Agent 写代码、查论文、生成图片、发邮件时，XMclaw 无能为力。

**建议**: 引入工具集系统，按领域分组：
1. **P0 核心工具集**: git 操作、GitHub API、代码搜索、图像生成（DALL-E/fal）、邮件发送
2. **P1 扩展工具集**: 数据库查询、API 测试 (HTTP client)、日历操作、笔记应用集成
3. **P2 领域工具集**: MLOps、区块链、智能家居（通过 MCP 实现，避免重复造轮子）

#### 3.4.2 浏览器自动化

**现状**: XMclaw 的浏览器工具是可选 Playwright，没有隔离的浏览器配置文件，也没有 CDP 控制。

**竞品**: OpenClaw 有隔离的 Chrome/Brave/Chromium 配置文件，通过 CDP 控制，可选 Docker 内沙箱浏览器。QwenPaw 的 `browser_use` 是核心工具。

**建议**: 将浏览器工具升级为核心能力，支持多配置文件、CDP 调试、截图/录屏、表单自动填充。

---

### 3.5 技能层差距

#### 3.5.1 技能生态系统

**现状**: XMclaw 的 `MarkdownSkill` 从本地磁盘加载 `SKILL.md`，有版本化注册表（`SkillRegistry`）和晋升/回滚机制。但：
- 无公共技能市场
- 无技能安装/卸载 CLI 命令
- 技能无自创建/自改进能力（进化是参数调优，不是代码生成）

**竞品**:
- OpenClaw: ClawHub (`clawhub.ai`) 公共注册表，`openclaw skills install <slug>` 一键安装。技能 Workshop 可自动从观察到的 Agent 过程生成技能。
- Hermes: Skills Hub (`agentskills.io`)，支持从经验自动创建技能，技能在使用中自我改进。
- QwenPaw: 技能中心多源导入（ClawHub/GitHub/LobeHub/ModelScope），ZIP 导入带路径遍历防护，技能扫描器检测风险。

**建议**:
1. **技能市场客户端**: 实现 `xmclaw skills install/search/uninstall` CLI，对接 ClawHub/SkillsHub。
2. **技能自创建**: 在 AgentLoop 中增加"技能提取"阶段——当 Agent 成功完成复杂任务后，提示 LLM 将过程总结为 `SKILL.md` 并写入注册表。
3. **技能安全扫描**: 导入前运行 `SkillScanner`，检测提示注入、命令注入、硬编码密钥。

#### 3.5.2 技能版本化

**XMclaw 优势**: `SkillRegistry` 的 append-only history + HEAD pointer + `promote(evidence=...)` + `rollback(reason=...)` 是独特优势。三款竞品均没有如此严格的证据驱动晋升机制。

**建议**: 保持并强化此优势，增加：
- 技能 A/B 测试框架
- 技能性能仪表盘（成功率、平均成本、用户评分）
- 技能依赖管理（Skill A 依赖 Skill B）

---

### 3.6 记忆层差距

#### 3.6.1 记忆系统对比

| 能力 | XMclaw | OpenClaw | Hermes | QwenPaw |
|------|--------|----------|--------|---------|
| 语义搜索 | ✅ sqlite-vec | ✅ sqlite-vec / QMD | ✅ Honcho | ✅ ReMe |
| 文件索引 | ✅ FileMemoryIndex | ✅ Markdown 文件 | ✅ Honcho | ✅ JSON 会话 |
| 跨会话召回 | ❌ | ✅ FTS5 + 摘要 | ✅ FTS5 + LLM 摘要 | ✅ 长时记忆 |
| 用户建模 | ❌ | ✅ USER.md | ✅ Honcho 方言 | ✅ 用户画像 |
| Dreaming | ❌ | ✅ 后台记忆整合 | ❌ | ❌ |
| 记忆 Wiki | ❌ | ✅ Obsidian 兼容 | ❌ | ❌ |
| 记忆压缩 | ❌ | ✅ 压缩前静默刷新 | ✅ trajectory_compressor | ✅ ContextManager |

**差距**: XMclaw 的记忆是"索引 + 向量搜索"，缺少跨会话的主动召回、用户画像建模、后台整理。

**建议**:
1. **跨会话搜索**: 为 `FileMemoryIndex` 增加 FTS5 全文搜索，支持按关键词检索历史会话。
2. **用户画像**: 在会话结束时，提示 LLM 提取用户偏好/习惯，写入 `USER.md`。
3. **记忆压缩**: 在长会话中，自动将早期消息压缩为摘要，释放上下文窗口。

---

### 3.7 安全层差距

#### 3.7.1 安全架构对比

| 安全层 | XMclaw | OpenClaw | Hermes | QwenPaw |
|--------|--------|----------|--------|---------|
| 权限模式 | ✅ auto/ask/block | ✅ 执行审批分类 | ✅ 命令审批 | ✅ deny/guard/approve |
| 设备绑定认证 | ✅ Ed25519 配对 | ✅ 共享密钥 + Tailscale | ❌ | ❌ |
| 危险命令拦截 | ❌ | ✅ 自动分类 | ✅ 审批门 | ✅ ToolGuardEngine |
| 文件访问限制 | ❌（仅 allowed_dirs） | ✅ 沙箱隔离 | ✅ 路径限制 | ✅ FilePathToolGuardian |
| Shell 逃逸检测 | ❌ | ❌ | ❌ | ✅ ShellEvasionGuardian |
| 技能安全扫描 | ❌ | ❌ | ❌ | ✅ SkillScanner |
| 密钥加密存储 | ❌（明文 JSON） | ❌（明文？） | ❌ | ✅ keyring + AES |
| Web 认证 | ❌ | ✅ 共享密钥 | ❌ | ✅ 可选 AuthMiddleware |
| 沙箱执行 | ❌ | ✅ Docker/SSH | ✅ 6 种后端 | ❌（工具守卫替代） |

**差距**: XMclaw 的安全体系是"认证 + 权限模式"，缺少运行时的主动拦截和扫描。

**建议**（优先级从高到低）：
1. **P0 - 危险命令拦截**: 在 `BuiltinTools.bash` 执行前，通过正则/AST 检测 `rm -rf /`, `mkfs`, `dd if=/dev/zero`, `curl ... | bash` 等危险模式。支持配置 denylist。
2. **P0 - 文件访问守卫**: 在 `file_read`/`file_write`/`list_dir` 中，检查路径是否在 `allowed_dirs` 内，阻止访问 `~/.ssh`, `~/.gnupg`, 系统目录。
3. **P1 - Shell 逃逸检测**: 检测混淆命令（base64 解码管道、十六进制转义、反引号嵌套）。
4. **P1 - 技能扫描器**: 导入技能前扫描 `SKILL.md` 和脚本中的风险模式。
5. **P2 - 密钥加密**: 将 `config.json` 中的 API key 迁移到 OS keyring 或加密存储。
6. **P2 - Web 认证**: 为 Web UI 增加可选的登录保护。

---

### 3.8 部署与运维差距

#### 3.8.1 部署方式对比

| 方式 | XMclaw | OpenClaw | Hermes | QwenPaw |
|------|--------|----------|--------|---------|
| pip 安装 | ✅ editable | ❌ (npm) | ✅ (uv pip) | ✅ |
| Docker | ❌ | ✅ | ✅ | ✅ |
| Nix | ❌ | ✅ | ✅ | ❌ |
| systemd | ❌ | ✅ | ❌ | ❌ |
| launchd | ❌ | ✅ | ❌ | ❌ |
| Windows Service | ❌ | ❌ | ❌ | ❌ |
| 无服务器 | ❌ | ❌ | ✅ (Modal/Daytona) | ❌ |
| 守护进程管理 | PID 文件 | launchd/systemd/Win Task | 无 | 无 |

**差距**: XMclaw 只有最基础的 `xmclaw serve`（前台）和 `xmclaw start`（PID 文件后台）。没有容器化、没有系统服务集成。

**建议**:
1. **Dockerfile**: 提供官方 Docker 镜像，支持多阶段构建（减小镜像体积）。
2. **docker-compose.yml**: 一键启动 XMclaw + 可选的浏览器服务。
3. **系统服务模板**: 提供 systemd service 文件、Windows Service 包装器（`pywin32`）、macOS launchd plist。
4. **安装脚本**: 提供 `curl | bash` 一键安装脚本（检测平台、安装依赖、创建虚拟环境）。

---

### 3.9 API 与集成差距

#### 3.9.1 API 兼容性

**现状**: XMclaw 的 API 是自有设计（`/api/v2/*` + WS `/agent/v2/{sid}`）。没有与现有生态的兼容性层。

**竞品**:
- OpenClaw: OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`, `/v1/responses`)
- Hermes: ACP (Agent Client Protocol) 客户端 + 服务器
- QwenPaw: ACP 服务器模式

**建议**:
1. **OpenAI 兼容层**: 实现 `/v1/chat/completions` 和 `/v1/models`，将 XMclaw 的 AgentLoop 包装为 OpenAI 聊天完成流。这让现有客户端（如 Continue.dev、Cursor、ChatGPT-Next-Web）可直接使用 XMclaw。
2. **ACP 适配器**: 实现 Agent Client Protocol，让 XMclaw 可被 Codex CLI、Claude Code 等工具调用。

#### 3.9.2 MCP 集成

**现状**: `MCPBridge` 存在于代码中，但功能和测试覆盖不足。

**竞品**: Hermes 和 QwenPaw 都有完整的 MCP 客户端管理器，支持 stdio 和 HTTP/SSE 传输，热重载。

**建议**: 将 MCP 提升为一等公民，实现 `MCPClientManager`，支持：
- 多服务器配置
- stdio 和 HTTP/SSE 传输
- 热重载（连接新服务器 → 交换 → 关闭旧连接）
- 工具自动注册到 AgentLoop

---

### 3.10 测试与质量差距

#### 3.10.1 测试覆盖对比

| 测试类型 | XMclaw | Hermes | QwenPaw |
|----------|--------|--------|---------|
| 单元测试 | ✅ 693 passed | ✅ pytest | ✅ pytest |
| 集成测试 | ✅ 15 files | ✅ 有 | ✅ 有 |
| E2E 测试 | ❌ | ✅ 有 | ✅ 有 |
| 基准测试 | ✅ 5 live bench | ✅ 有 | ❌ |
| 合规测试 | ✅ 4 conformance | ❌ | ❌ |
| 前端测试 | ❌ | ✅ TUI + Web | ✅ Console |
| 通道测试 | ❌ | ✅ 有 | ✅ 有 |

**差距**: XMclaw 的后端测试质量高，但前端零测试、通道零测试。

**建议**:
1. 为 `ChannelAdapter` 基类编写契约测试（模拟入站消息，验证出站格式）。
2. 为 Web UI 引入 Playwright E2E 测试（至少覆盖：发送消息、工具调用、设置面板、模型切换）。
3. 为 CLI 引入 `pytest-console-scripts` 测试。

---

## 4. 开发路线图

### Phase 1 — 安全与基础补全（1~2 个月）

**目标**: 消除安全盲区，补全用户日常使用的基础能力。

- [ ] **安全体系**
  - [ ] 实现 `ToolGuardEngine`：规则引擎 + 文件路径守卫 + Shell 逃逸检测
  - [ ] 为 `bash` 工具增加 denylist（`rm -rf /`, `mkfs`, `dd if=/dev/zero` 等）
  - [ ] 为 `file_read`/`file_write` 增加敏感路径拦截（`~/.ssh`, 系统目录）
  - [ ] 实现 `SkillScanner`：导入技能前扫描风险模式
- [ ] **通道扩展**
  - [ ] 实现 `WhatsAppAdapter`（基于 `whatsapp-web.js` 或 Baileys 的 Python 封装）
  - [ ] 实现 `EmailAdapter`（IMAP 入站 + SMTP 出站）
  - [ ] 为所有通道增加 `dmPolicy` 安全策略（open / pairing / allowlist）
- [ ] **前端补全**
  - [ ] 将 Mock API 替换为真实后端实现：file browser、workspaces、profiles、memory editor
  - [ ] 实现 onboarding 向导页面（模型配置、通道设置、初始技能选择）
- [ ] **部署**
  - [ ] 提供官方 `Dockerfile` 和 `docker-compose.yml`
  - [ ] 提供 systemd service 模板
  - [ ] 编写一键安装脚本 `install.sh` / `install.ps1`

### Phase 2 — 多智能体与工具生态（2~3 个月）

**目标**: 从单 Agent 升级为多 Workspace 平台，扩展工具覆盖。

- [ ] **多智能体架构**
  - [ ] 引入 `Workspace` 类，封装 `AgentLoop` + `ChannelManager` + `MemoryManager` + `SkillRegistry`
  - [ ] 实现 `WorkspaceManager`：多 Workspace 生命周期管理、路由、隔离
  - [ ] 实现零停机重载：新 Workspace 预热 → 原子交换 → 旧实例优雅停止
  - [ ] 实现子智能体工具：`spawn_subagent`, `delegate_to_agent`, `check_subagent_task`
- [ ] **工具集系统**
  - [ ] 设计 `Toolset` 抽象：按领域分组（dev/research/creative/productivity/system）
  - [ ] 实现 `dev` 工具集：git、GitHub API、代码搜索、测试运行器
  - [ ] 实现 `creative` 工具集：图像生成（DALL-E/fal）、音频生成
  - [ ] 实现 `productivity` 工具集：邮件、日历、笔记集成
- [ ] **MCP 一等公民**
  - [ ] 重写 `MCPClientManager`：支持 stdio + HTTP/SSE、热重载、自动工具注册
  - [ ] 为常用 MCP 服务器提供预配置模板（filesystem、fetch、github、postgres）
- [ ] **API 兼容层**
  - [ ] 实现 OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
  - [ ] 实现 ACP 服务器适配器

### Phase 3 — 产品化与生态（3~4 个月）

**目标**: 达到开箱即用的产品级体验，建立技能生态。

- [ ] **原生前端**
  - [ ] 开发 XMclaw Web UI（Vite + TypeScript SPA，三面板布局）
  - [ ] 保留 Hermes UI 作为可选主题
  - [ ] 开发 TUI（基于 `textual` 或 `rich` + `prompt_toolkit`）
  - [ ] 为前端引入 Playwright E2E 测试
- [ ] **技能生态**
  - [ ] 实现 `xmclaw skills install/search/uninstall` CLI
  - [ ] 对接 ClawHub / SkillsHub 公共市场
  - [ ] 实现技能自创建：Agent 在复杂任务后自动生成 `SKILL.md`
  - [ ] 实现技能 A/B 测试和性能仪表盘
- [ ] **记忆增强**
  - [ ] 集成 FTS5 全文搜索，支持跨会话关键词检索
  - [ ] 实现用户画像自动提取（会话结束时写入 `USER.md`）
  - [ ] 实现记忆压缩：长会话自动摘要早期内容
- [ ] **高级部署**
  - [ ] 提供 Nix flake
  - [ ] 提供 Windows Service 包装器
  - [ ] 支持云部署模板（AWS ECS、Fly.io、Railway）
- [ ] **语音（可选）**
  - [ ] 集成 Edge TTS（免费，无需 API key）
  - [ ] 集成 faster-whisper 本地 STT

### Phase 4 — 差异化强化（持续）

**目标**: 将 XMclaw 的自主进化能力打造成竞品无法复制的护城河。

- [ ] **进化系统增强**
  - [ ] 多智能体进化：不同 Workspace 的技能进化结果可共享/合并
  - [ ] 在线学习可视化：Web UI 中展示技能成功率、UCB1 置信区间、晋升历史
  - [ ] 进化策略市场：用户可分享/导入他人的进化策略配置
- [ ] **轨迹与 RL**
  - [ ] 实现 `TrajectoryCompressor`：压缩事件流为训练数据
  - [ ] 导出工具调用数据集（ShareGPT / OpenAI fine-tuning 格式）
  - [ ] 探索与 RL 框架集成（可选 Tinker-Atropos）
- [ ] **企业特性**
  - [ ] RBAC（基于角色的访问控制）：不同用户/组拥有不同 Workspace 权限
  - [ ] 审计日志：所有工具调用、配置变更、晋升操作的不可篡改日志
  - [ ] SLA 监控：Agent 响应时间、成功率、成本仪表盘

---

## 5. 文件与路径规范建议

### 5.1 项目目录结构规范

```
xmclaw/
├── xmclaw/                          # Python 包
│   ├── __init__.py
│   ├── cli/                         # CLI 命令
│   │   ├── main.py                  # Typer 根入口
│   │   ├── commands/                # 子命令模块
│   │   │   ├── serve.py
│   │   │   ├── start.py
│   │   │   ├── skills.py            # skills install/search/uninstall
│   │   │   ├── channels.py          # channels list/start/stop
│   │   │   ├── agent.py             # agent chat/reset/status
│   │   │   └── doctor.py
│   │   └── _utils.py
│   ├── daemon/                      # FastAPI 服务
│   │   ├── app.py                   # create_app()
│   │   ├── lifespan.py              # startup/shutdown 逻辑
│   │   ├── routers/                 # REST API 路由
│   │   │   ├── __init__.py
│   │   │   ├── config.py
│   │   │   ├── agent.py
│   │   │   ├── skills.py
│   │   │   ├── channels.py
│   │   │   ├── cron.py
│   │   │   ├── memory.py
│   │   │   ├── models.py
│   │   │   ├── files.py             # file browser API
│   │   │   ├── workspaces.py        # workspace CRUD
│   │   │   └── openai_compat.py     # /v1/chat/completions
│   │   ├── websockets/              # WS 网关
│   │   │   ├── gateway.py           # /agent/v2/{sid}
│   │   │   └── protocol.py          # 消息格式定义
│   │   └── static/                  # 静态文件（Web UI）
│   │       ├── index.html
│   │       ├── assets/
│   │       └── ...
│   ├── core/                        # 内部引擎
│   │   ├── bus/                     # 事件总线
│   │   ├── evolution/               # 进化系统
│   │   ├── grader/                  # 评分系统
│   │   ├── scheduler/               # 调度器
│   │   ├── memory/                  # 记忆系统
│   │   └── session/                 # 会话管理
│   ├── workspace/                   # 多智能体工作区（NEW）
│   │   ├── __init__.py
│   │   ├── workspace.py             # Workspace 类
│   │   ├── manager.py               # WorkspaceManager
│   │   └── router.py                # 请求路由
│   ├── providers/                   # 可插拔后端
│   │   ├── llm/                     # LLM 提供商
│   │   ├── tool/                    # 工具提供商
│   │   ├── memory/                  # 记忆后端
│   │   ├── runtime/                 # 执行运行时
│   │   └── channel/                 # 通道适配器
│   ├── channels/                    # 通道实现
│   │   ├── base.py                  # BaseChannelAdapter
│   │   ├── manager.py               # ChannelManager
│   │   ├── telegram.py
│   │   ├── slack.py
│   │   ├── discord.py
│   │   ├── lark.py
│   │   ├── whatsapp.py              # NEW
│   │   ├── email.py                 # NEW
│   │   └── webchat.py               # NEW
│   ├── skills/                      # 技能系统
│   │   ├── registry.py
│   │   ├── loader.py
│   │   ├── markdown_skill.py
│   │   ├── scanner.py               # SkillScanner (NEW)
│   │   └── hub.py                   # 技能市场客户端 (NEW)
│   ├── security/                    # 安全系统（NEW）
│   │   ├── __init__.py
│   │   ├── tool_guard.py            # ToolGuardEngine
│   │   ├── file_guard.py            # FilePathToolGuardian
│   │   ├── shell_guard.py           # ShellEvasionGuardian
│   │   └── scanner.py               # SkillScanner
│   ├── plugins/                     # 插件系统
│   │   └── sdk/
│   └── _version.py
├── tests/
│   ├── unit/                        # 单元测试
│   ├── integration/                 # 集成测试
│   ├── e2e/                         # E2E 测试（NEW）
│   ├── bench/                       # 基准测试
│   ├── conformance/                 # 合规测试
│   └── fixtures/
├── console/                         # 原生 Web UI（NEW，可选 Vite TS SPA）
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   └── dist/                        # 构建输出 → xmclaw/daemon/static/
├── deploy/                          # 部署配置（NEW）
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── systemd/
│   ├── launchd/
│   └── windows-service/
├── docs/                            # 文档
│   ├── architecture.md
│   ├── api.md
│   ├── channels.md
│   ├── skills.md
│   └── security.md
├── scripts/                         # 开发/构建脚本
│   ├── install.sh                   # 一键安装（NEW）
│   ├── install.ps1                  # Windows 一键安装（NEW）
│   └── build-ui.sh
├── pyproject.toml
├── README.md
└── AGENTS.md
```

### 5.2 配置文件规范

**当前问题**: `daemon/config.json` 使用扁平结构，LLM 提供商配置混杂在 anthropic 块中（minimax 实际配置挂在 anthropic 下）。

**建议**: 采用分层命名空间，与 QwenPaw/OpenClaw 对齐：

```json
{
  "gateway": {
    "host": "127.0.0.1",
    "port": 8080,
    "auth": {
      "enabled": false,
      "type": "bearer"
    }
  },
  "llm": {
    "providers": {
      "anthropic": {
        "api_key": "sk-...",
        "base_url": null,
        "default_model": "claude-sonnet-4-1"
      },
      "minimax": {
        "api_key": "sk-...",
        "base_url": "https://api.minimaxi.com/anthropic",
        "default_model": "minimax-portal/MiniMax-M2.7-highspeed"
      },
      "openai": {
        "api_key": null,
        "base_url": null,
        "default_model": "gpt-4o"
      },
      "ollama": {
        "base_url": "http://localhost:11434",
        "default_model": "llama3.1"
      }
    },
    "active_provider": "minimax",
    "active_model": "minimax-portal/MiniMax-M2.7-highspeed"
  },
  "tools": {
    "allowed_dirs": ["C:\\Users\\15978\\Desktop"],
    "enable_bash": true,
    "enable_web": true,
    "enable_browser": false,
    "enable_lsp": false,
    "deny_patterns": ["rm -rf /", "mkfs", "dd if=/dev/zero"]
  },
  "security": {
    "permission_mode": "ask",
    "file_guard_enabled": true,
    "shell_guard_enabled": true,
    "skill_scan_enabled": true,
    "blocked_paths": ["~/.ssh", "~/.gnupg", "/etc", "C:\\Windows\\System32"]
  },
  "channels": {
    "telegram": { "enabled": false, "token": null, "dm_policy": "pairing" },
    "slack": { "enabled": false, "token": null },
    "discord": { "enabled": false, "token": null },
    "lark": { "enabled": false, "app_id": null, "app_secret": null },
    "whatsapp": { "enabled": false, "session_path": null },
    "email": { "enabled": false, "imap_host": null, "smtp_host": null }
  },
  "memory": {
    "vector_db_path": "./memory.db",
    "session_retention_days": 30,
    "max_context_tokens": 128000
  },
  "evolution": {
    "enabled": true,
    "interval_minutes": 60,
    "vfm_threshold": 0.75,
    "auto_rollback": true
  },
  "workspaces": [
    {
      "id": "default",
      "name": "Default",
      "description": "Default workspace",
      "channels": ["telegram", "slack"],
      "skills": ["coding", "research"],
      "tools": ["file", "bash", "web"]
    }
  ],
  "mcp_servers": {},
  "cron_jobs": []
}
```

### 5.3 代码规范建议

1. **导入排序**: 使用 `ruff` 的 `I001` 规则统一 import 排序（标准库 → 第三方 → 本地）。
2. **类型注解**: 所有公共函数必须带类型注解，复杂数据结构使用 `TypedDict` 或 `Pydantic` 模型。
3. **异常层次**: 定义 `XMclawError` 基类，所有业务异常继承自它。禁止裸 `raise Exception`。
4. **日志规范**: 使用结构化日志（`structlog`），所有事件包含 `session_id`, `workspace_id`, `event_type` 字段。
5. **路径处理**: 所有文件路径使用 `pathlib.Path`，禁止字符串拼接路径。Windows/Linux 兼容通过 `Path` 自动处理。
6. **配置访问**: 禁止直接读取全局 `config` 变量，所有配置通过 `request.app.state.config` 或注入的 `Config` 对象访问。
7. **测试命名**: `test_v2_<module>_<scenario>.py`，测试函数名 `test_<given>_<when>_<then>`。
8. **文档字符串**: 所有公共类/方法使用 Google Style docstrings。

---

## 5.4 → 开始执行前读

本文 §4 的 Phase 1–4 给出**策略粒度**的时间框架（6–9 月）。若要落到**可 PR 的任务级拆解**（12–16 周，17 Epic），请继续读：

- [DEV_ROADMAP.md §4 · 17 Epic 执行表](DEV_ROADMAP.md#4-对标差异执行表拆到可-pr-粒度) — 每 Epic 带 checkbox + 具体文件 + 参考仓库行号
- [DEV_ROADMAP.md §7 · M1-M9 里程碑](DEV_ROADMAP.md#7-成熟度里程碑从-dev-alpha-到-ga) — 每个里程碑带退出标准
- [DEV_ROADMAP.md §8 · Week 1-2 落地清单](DEV_ROADMAP.md#8-落地优先级建议下一周动什么) — 明天开工的 7 件事

**粒度对照**：

| 本文（策略） | DEV_ROADMAP.md（执行） |
|------|-----------------------|
| Phase 1 安全与基础补全 | Epic #3 沙箱 / #10 doctor / #14 注入防御 / #15 日志 / #16 secrets → M1 + M4 + M8 |
| Phase 2 多智能体与工具生态 | Epic #2 Plugin SDK / #13 事件总线 / #17 多 agent / #1 Channel SDK → M2 + M3 |
| Phase 3 产品化与生态 | Epic #8 Skill Hub / #9 Onboarding → M6 + M7 |
| Phase 4 差异化强化 | Epic #4 进化执行层（★） → M5 |

---

## 6. 总结

XMclaw 的 **自主进化引擎** 是三款竞品均不具备的差异化优势，这是应继续深化的护城河。但在 **产品化外壳** 上，XMclaw 存在明显短板：

1. **安全** 是最高优先级——当前缺少运行时拦截，不适合公开部署。
2. **通道扩展** 是用户增长关键——WhatsApp、Email、WebChat 必须尽快补齐。
3. **多智能体架构** 是平台化必经之路——从单 AgentLoop 升级到 Workspace 模型。
4. **前端** 是用户体验门面——从适配层过渡到原生 UI，或至少补全所有 Mock API。
5. **部署** 是采纳门槛——Docker + 系统服务模板让非开发者也能使用。

按照本文档的 **Phase 1 → Phase 2 → Phase 3 → Phase 4** 路线图执行，XMclaw 有望在 6~9 个月内达到与竞品同等的功能广度，同时保持进化系统的深度优势。

---

*本文档由代码级竞品分析生成，基于 OpenClaw (main, 2026-04-22)、HermesAgent (main, 2026-04-22)、QwenPaw/CoPaw (main, 2026-04-22) 的真实仓库代码。*
