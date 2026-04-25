# XMclaw 前端重设计 — 详细开发文档

> **状态**：初版蓝图 · 2026-04-25
> **作者**：XMclaw Bot（基于 2026-04-25 同类竞品调研）
> **适用范围**：`xmclaw/daemon/static/` 下的 Web UI 首版实现；为 Epic #23「前端」做蓝图
> **约束**：CLAUDE.md — **无 Node.js 构建步骤**、FastAPI `StaticFiles` 直出、WebSocket `/agent/v2/{session_id}`（后端 agent-v2 版本，与 UI 无关）

---

## 目录

1. [调研摘要 + 横向对比矩阵](#1-调研摘要--横向对比矩阵)
2. [设计原则（对应六点要求的可度量落地）](#2-设计原则对应六点要求的可度量落地)
3. [信息架构 & 导航](#3-信息架构--导航)
4. [核心页面线框 + 交互状态机](#4-核心页面线框--交互状态机)
5. [组件库清单（token → atom → molecule → organism）](#5-组件库清单)
6. [状态树 + WebSocket 事件映射表](#6-状态树--websocket-事件映射表)
7. [交互规范（快捷键 / 右键菜单 / 拖拽 / 命令面板）](#7-交互规范)
8. [权限与审批 UI（前端高权限落地）](#8-权限与审批-ui前端高权限落地)
9. [视觉规范（CSS 变量 / 字体 / 间距 / 动画）](#9-视觉规范)
10. [无障碍 + 国际化](#10-无障碍--国际化)
11. [技术选型决策树](#11-技术选型决策树)
12. [分阶段实施路线（Epic #23 Roadmap）](#12-分阶段实施路线)

---

## 1. 调研摘要 + 横向对比矩阵

### 1.1 本轮调研覆盖面

- **AI IDE / 代码助手**：Claude Code · Cursor · Windsurf · Continue.dev · Cline · Aider · Zed（OSS ai-editor）
- **本地优先 Agent Runtime**：OpenClaw · Hermes Agent · QwenPaw（CoPaw）
- **低代码 Agent 平台**：AutoGPT Platform · LangGraph Studio · Flowise · n8n · Dify
- **本地 LLM 客户端**：Open WebUI · LM Studio · Jan.ai
- **直接读源码（5 仓库、浅克隆到 `.claude/scratch/competitor-code/`）**：Continue.dev · Cline · Open WebUI · Aider · AutoGPT Platform

### 1.2 一张表看完竞品

| 产品 | 技术栈 | 状态管理 | UI 库 | 流式 Markdown | Plan/Approve | 自进化 UI | 多 Agent | Canvas | Dashboard | XMclaw 可借鉴度 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Continue.dev | React 18 + Vite + SWC | Redux Toolkit + persist（白名单） | 自造 + Headless UI + Tippy | react-remark + memo | 单轮 diff toolbar | 无 | 无 | 无 | 无 | ★★★ |
| Cline | React 18 + Vite 7 + Biome | React Context（60 字段） | Radix + heroui + shadcn 风 + framer-motion | react-markdown + memo + rAF 不显式 | **14 种 ask + Approve/Reject，primary+secondary** | 无 | 子任务 | 无 | 无 | ★★★★★ |
| Open WebUI | SvelteKit 2 + Svelte 5 + TW4 | Svelte stores | bits-ui + tiptap + xyflow | **rAF 合帧 + token 数组 + keyed child** | 模型切换弹审批 | 点赞点踩（下游不可用） | 人格切换 | 流程图 | 无 | ★★★★ |
| LM Studio | 桌面 Tauri | — | — | — | **MCP tool confirmation dialog，可编辑参数** | 无 | — | 无 | 右侧参数面板 | ★★★★ |
| Hermes | TUI + Web（PR #5163） | — | — | — | 斜杠审批 | **自动生成 skill + DSPy/GEPA** | subagent | 无 | 聊天内命令 | ★★★★★ |
| QwenPaw | Web Console（原生） | — | — | — | — | Memory-Evolving + Proactive | ACP Server | 无 | **Agent Statistics 页** | ★★★★★ |
| OpenClaw | CLI + macOS 菜单栏 | — | — | — | 显式 pairing | 无 | 按通道隔离 | **Live Canvas（agent 主导）** | 聊天内命令 | ★★★ |
| AutoGPT Platform | Next.js 15 + React 19 + Zustand + TanStack + shadcn | Zustand + nuqs + TanStack | Radix + Phosphor + shadcn 风 | — | 节点参数表单 | 无 | 无 | **@xyflow/react 12** | Monitoring | ★★★★ |
| LangGraph Studio | React + Graph | — | — | — | Breakpoint pause | 无 | 多图 | **核心** | 时间旅行 | ★★★★ |
| Flowise | Angular-ish canvas | — | — | — | — | 无 | — | **核心** | Inline | ★★ |
| n8n | Vue + canvas | — | — | — | — | 无 | — | **核心** + 模板库 | Inline | ★★ |
| Dify | Next.js | — | — | — | — | 无 | — | Workflow | Observability | ★★★ |
| Aider | Streamlit 545 行 | `@st.cache_resource` | Streamlit 原生 | `st.write_stream`（零控制） | 无 | 无 | 无 | 无 | `st.metric` | ★（反面教材） |
| Jan.ai | Tauri | — | — | — | — | 无 | 弱 | 无 | 无 | ★★ |
| Claude Code / Cursor / Windsurf / Zed | IDE 内嵌 | — | — | — | **Plan/Act 双模式 + 审批队列** | — | — | — | — | ★★★★★ |

### 1.3 六条跨竞品的强共识

1. **Radix / Headless 无头组件 + Tailwind + Lucide/Phosphor 图标 + CVA（class-variance-authority）**已经是 2026 年业内默认组合（Cline、AutoGPT、大多数 shadcn 派生）。
2. **主题 token 锚定 CSS 变量**——Continue / Cline 把所有颜色绑到 `--vscode-*`；AutoGPT 用标准 shadcn HSL。**XMclaw 应采双层 token**：内层 `--xmc-*` 私有变量，外层在 VSCode/Cursor webview 里回退到 `--vscode-*`，独立跑时回退到预设主题。
3. **流式 Markdown 渲染必须 rAF 合帧 + token 级 keyed diff**（Open WebUI `Markdown.svelte:62-87`）。单纯 `innerHTML = parseMarkdown(buffer)` 在长回答时会 jank。
4. **工具调用默认折叠、内容 N 行以内自动展开**（Cline `CommandOutputRow.tsx:25` 的 `lineCount <= 5`；Continue 的 `max-h-40 + gradient mask`）。
5. **Approve/Reject 走多态化 ask 分派**（Cline 14 种 `clineAsk` 类型），按钮文案随语义变化，响应结构化 `responseType`。
6. **全局快捷键集中 registry**（Open WebUI `shortcuts.ts:14-168` 的 5 分类、25 个 shortcut）——散落 `onKeyDown` 是反面教材。

### 1.4 四条必须避开的坑

| 坑 | 教训源 | XMclaw 对策 |
| --- | --- | --- |
| **上帝组件**（`ChatTextArea.tsx` 1622 行、`Chat.svelte` 3101 行、`ChatRow.tsx` 1245 行） | Cline、Open WebUI | 任何单文件 ≤ 500 行；超过必须拆，参考 Continue `StepContainer/` 的子职责文件 |
| **YOLO / auto-run 的单字符串白名单**（Cursor `allowlist:["*"]`）、**反馈收上来下游不可用**（Open WebUI Issue #9589）、**版本错位**（QwenPaw Issue #3675） | Cursor、Open WebUI、QwenPaw | 见 §8 审批 UI + §6 反馈流 + §4.9 设置页版本卡 |
| **技术栈漂移**：styled-components + Tailwind 双栈并行 | Continue | XMclaw 只用 **CSS 变量 + 少量 utility**，不引新样式系统 |
| **定位漂移**（AutoGPT 从本地 agent → SaaS 平台）、**把主 UI 交给 Streamlit** | AutoGPT、Aider | 坚持 local-first + 纯 vanilla 可 `StaticFiles` 直出的叙事 |

### 1.5 XMclaw 的差异化开口（无人领地）

- **自进化可视化**：所有竞品只展示"LLM 在做什么"，没一家把"技能在被如何评分、正在被提升还是淘汰"做成一等 UI。Hermes 有叙事（"built-in learning loop"）但没有可视卡片；QwenPaw 有 Memory-Evolving 但没有技能血条。XMclaw 有 SkillForge + EvolutionEngine + HonestGrader，UI 上应该把这三者拼成**「XMclaw 今天学会了什么」Card + 血条 + 对比页**——这是产品叙事的锚点。
- **事件总线时间旅行**：LangGraph Studio 的「回到任一状态节点继续」是终极调试体验。XMclaw 有 SQLite 事件总线 + `EventBus.replay()` 端点，**把这个做成 UI 的 Timeline scrubber**，同类里没第二家。
- **Tool Guardian 审批队列 + 可编辑参数**：抄 LM Studio 的 MCP confirmation dialog，叠加 Epic #3 的 policy 分级（critical/high/medium），在所有 agent runtime 里是最完整的审批 UX。
- **Skill Preset 卡片**：抄 Open WebUI Model Builder 的 model+prompt+knowledge+tools+skills 四件套，把「自定义 agent」做成可保存、可分享、可 diff 的 preset。

---

## 2. 设计原则（对应六点要求的可度量落地）

用户明确提出六条：**交互 · 前端高权限 · 便捷交互 · 功能完善 · 直观 · 方便**。我把它们翻译成可检查的硬指标。

### 2.1 交互（响应性 + 确定性）

| 指标 | 目标值 | 测量方法 |
| --- | --- | --- |
| 首字节 → 首帧渲染 | < 200 ms（本地 daemon） | Chrome DevTools Performance |
| 流式 token 帧率 | ≥ 30 fps，无 jank（60 fps 理想） | rAF 合帧见 §6.4 |
| 按钮点击 → 视觉反馈 | ≤ 16 ms | CSS `:active` + optimistic UI |
| 长列表滚动（事件流 10k 行） | ≥ 30 fps | 虚拟滚动（Intersection Observer 手搓） |
| 输入框输入延迟 | ≤ 32 ms | `input` 事件不触发全量 re-render |
| 状态变更可回溯 | 100% | 所有状态变更经 Store.dispatch，内存环形 buffer 保 200 条 |

### 2.2 前端高权限（不是只读查看器）

**反面定义**：不是"后端推什么、前端显示什么"。**正面定义**：前端可以在用户许可的范围内**直接驱动 daemon**，具备以下 10 个高权动作（→ 对应 REST/WS endpoint）：

| 动作 | 端点 | UI 目标 |
| --- | --- | --- |
| 创建 / 恢复 / 删除会话 | `POST/DELETE /v2/sessions` | 接入 |
| 切换 / 创建 / 命名 agent | `POST /v2/agents`, `PATCH /v2/agents/{id}` | **接入** |
| **批准 / 拒绝工具调用** | `POST /v2/approvals/{id}` | **接入**（见 §8） |
| **中断流式** | WS `{type: "cancel"}` | **接入** + UI 按钮 |
| **编辑参数后重放** | `POST /v2/tool/retry` | **新增** |
| **回到事件点重分叉** | `POST /v2/sessions/{id}/rewind` | **新增**（借 SQLite bus） |
| **升 / 降 skill 版本** | `POST /v2/skills/{name}/promote` | 接入 |
| **下载 / 恢复备份** | `GET /v2/backups/{name}`, `POST /v2/backups/{name}/restore` | **接入** |
| **跑 doctor + 自修** | `GET /v2/doctor`, `POST /v2/doctor/fix` | 接入 |
| **切主题 / 密度** | 前端存 localStorage | 接入 |

### 2.3 便捷（Keyboard-first、One-handed）

- **所有高频动作都有快捷键**（见 §7.1 注册表，≥ 30 个）
- **命令面板**（`Ctrl/Cmd+K`）能找到任何页面、任何 agent、任何 skill、任何历史消息
- **`@` 上下文**：在输入框内敲 `@file`、`@skill`、`@agent`、`@memory`、`@event` 快速插入引用（抄 Continue / Cline）
- **`/` 斜杠命令**：`/plan`、`/act`、`/clear`、`/rewind`、`/approve`、`/status`、`/skill <name>`（抄 Claude Code、OpenClaw）
- **全局 Escape 语义统一**：优先关弹窗 → 次之停生成 → 最后聚焦输入框（抄 Open WebUI）

### 2.4 功能完善（覆盖率清单）

UI 首版必须一等覆盖以下 **16 个领域**：

Chat · Agents · Skills · Memory · Evolution · Tools · MCP · Security · Cost/Tokens · Files · Sessions · Timeline · Doctor · Config · Backup · About

### 2.5 直观（无需文档的心智模型）

- **三栏布局**深入骨髓（左侧导航 260 px、中主视图、右侧 workspace dock 可开合）—— 和所有 AI IDE 一致
- **颜色语义**：绿 = 成功 / 主动、红 = 错误 / 销毁、琥珀 = 审批 / 警告、蓝 = 信息 / 进行中、紫 = 进化 / 学习（XMclaw 专属）
- **永远显示状态**：顶部状态栏实时显示 daemon 连接、当前模型、累计成本（抄 LM Studio 右侧参数面板）
- **第一次使用有引导**：`onboard/` 的六步 wizard（见 Epic #9）在 Web UI 里继续发扬

### 2.6 方便（零门槛启动 + 安装即用）

- **启动即打开 UI**：`xmclaw start` 后自动 `webbrowser.open("http://127.0.0.1:8765/ui/")`（已实现，保留）
- **无 Node.js 构建**：CLAUDE.md 约束 → 技术选型只在 vanilla / Alpine / Preact+htm 三者选（见 §11）
- **单一静态入口**：`index.html` → 动态 import ES modules，不打包
- **配对 token 体验**：第一次弹一个 token 卡片（大字号、一键复制、QR 码备用），之后本地会话自动重连（抄 OpenClaw 的 pairing）

---

## 3. 信息架构 & 导航

### 3.1 三栏主布局

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Top Bar (40px):  [XMclaw logo]  [agent selector ▼]   [🔍 Cmd+K]  [🔔] [👤]  │
├────────┬───────────────────────────────────────────┬─────────────────────────┤
│ Sidebar│   Main View (Chat / Agents / Skills / …)  │  Workspace Dock (可开合) │
│ 260 px │                                           │  320 / 480 / 640 px 三档 │
│        │                                           │                         │
│ [Chat] │   (根据 Sidebar 选中切换)                  │  Tabs: Activity/Timeline/ │
│ [Agents│                                           │        Todos/Files/Tokens/│
│ [Skills│                                           │        Cost…            │
│ …      │                                           │                         │
│        │                                           │                         │
├────────┴───────────────────────────────────────────┴─────────────────────────┤
│  Status Bar (24px): [● daemon]  [⚡ model]  [$0.0031]  [127k tokens]  [v0.2.1]│
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Sidebar** 260 px（可折成 48 px icon-only），CSS `--sidebar-w` 变量控制
- **Workspace Dock** 320/480/640 三档（双击边框循环），`Alt+\` 全开/全关
- **Status Bar** 永远在底，点击 daemon 指示灯弹连接诊断弹窗（跑 `GET /v2/health`）

### 3.2 Sidebar 主导航（一级条目）

1. **💬 Chat** — 会话列表 + 当前活动
2. **🤖 Agents** — 多 agent 管理 + 各自 workspace
3. **✨ Skills** — Skill 仓库 + 版本矩阵 + 手动 promote/demote
4. **🧬 Evolution** — VFM 曲线 + 候选队列 + Grader 明细（**XMclaw 独有**）
5. **🧠 Memory** — short / working / long 三层浏览 + prune 按钮
6. **🔧 Tools** — ToolProvider 列表 + 每个工具的调用历史
7. **🔌 MCP** — MCP server 连接 + 工具 discovery
8. **🛡 Security** — Guardian 开关 + Approval 队列 + Redaction 规则
9. **💾 Backup** — 备份列表 + 手动触发 + 一键恢复
10. **🏥 Doctor** — 检查清单 + --fix 按钮
11. **📊 Insights** — 成本 / 会话 / Tool 使用统计
12. **⚙ Settings** — LLM 密钥 / 通道 / 主题 / 集成

**折叠规则**：前 6 个总展开；7-12 归入"更多"可展开（抄 Open WebUI 的 sidebar 紧凑）。

### 3.3 右侧 workspace 内容规划

Chat 主页右侧的 workspace 面板按以下分组：

- **Live**（默认 tab）— 流式事件流（events.db 实时订阅）
- **Todos** — 当前会话的 task list
- **Timeline** — 事件轴 + scrubber（点某事件可 rewind）
- **Files** — 当前 workspace 的文件树（`/v2/files`）
- **Tokens** — 会话内 token / cost 计数（链接到 Insights 大面板）

Skills / Tools / MCP / Agents / Security / Settings 等都在左侧 Sidebar 独立成页，
workspace 里只保留"当前会话涉及到的子集"mini 视图（避免一个页面塞不下）。

### 3.4 路由（URL 结构）

即便是 SPA，也维护可分享 URL：

```
/ui/                                    → redirect to /ui/chat
/ui/chat                                → chat list
/ui/chat/{session_id}                   → 指定会话
/ui/chat/{session_id}?event={evt_id}    → 跳到事件点（rewind 预览）
/ui/agents                              → agent 列表
/ui/agents/{agent_id}                   → agent 详情
/ui/skills                              → skill 矩阵
/ui/skills/{name}                       → skill 详情 + 版本
/ui/skills/{name}?v={version}           → 指定版本
/ui/evolution                           → 进化 dashboard
/ui/evolution/candidates/{cand_id}      → 候选详情
/ui/memory                              → 记忆层
/ui/memory/{layer}/{item_id}            → 单条
/ui/tools                               → 工具
/ui/mcp                                 → MCP servers
/ui/security/approvals                  → 审批队列
/ui/security/approvals/{id}             → 单条审批
/ui/backup                              → 备份列表
/ui/doctor                              → 诊断
/ui/insights                            → 统计
/ui/settings/{section}                  → llm / integrations / theme / advanced
```

实现：用 `history.pushState` + `popstate` 事件手搓 router（50 行以内），无需引入 vue-router / react-router。

---

## 4. 核心页面线框 + 交互状态机

### 4.1 Chat（主场景）

```
┌──────────────────────────────┬──────────────────────────────────────┬──────────────┐
│ [+ New Chat]          [⋯]    │  Session Title              [⭐] [⋯]  │ Activity     │
│ ─────────────────────────────│ ────────────────────────────────────│ ──────────── │
│ ▣ 2026-04-25 今天            │                                      │ 🟢 Connected │
│   ● refactor bus.py          │  [assistant]:                        │ ─────────── │
│   ○ add backup UI            │    Reading xmclaw/core/bus/...       │ TOOLS USED  │
│ ▣ 2026-04-24                 │    ┌─── tool: file_read ────┐ (▼)   │ • file_read │
│   ○ epic #16 phase 2         │    │ xmclaw/core/bus/events.py│      │ • grep      │
│ ▣ Pinned                     │    │ (expand for content)     │      │             │
│   ★ playground               │    └──────────────────────────┘      │ COST        │
│                              │                                      │ $0.0031     │
│                              │  [user]:                             │ 127k tokens │
│                              │    @file xmclaw/core/bus/events.py   │             │
│                              │    请加个 cancel 字段                 │ MODE        │
│                              │                                      │ ⚡ Act       │
│                              │  [assistant]:                        │ [Plan] [Act]│
│                              │    I'll edit events.py …             │             │
│                              │    ┌─── tool: file_edit ────┐ 🟡     │             │
│                              │    │ NEEDS_APPROVAL          │      │             │
│                              │    │ [Approve] [Reject] [⚙]   │      │             │
│                              │    └──────────────────────────┘      │             │
│                              │ ──────────────────────────────────── │             │
│                              │  ┌────────────────────────────────┐  │             │
│                              │  │ Type message… (@ / /)          │  │             │
│                              │  │                                │  │             │
│                              │  │  [📎] [🎤] [🖼] [⏹ stop] [↑]   │  │             │
│                              │  └────────────────────────────────┘  │             │
└──────────────────────────────┴──────────────────────────────────────┴──────────────┘
```

#### 4.1.1 状态机（输入框）

```
            ┌─────────── Esc ───────────┐
            │                           ▼
       [IDLE]──type──→[COMPOSING]──enter──→[SUBMITTING]──ok──→[STREAMING]
            ▲                                  │                   │
            │                                  └──err──→[ERROR]    │
            │                                           │          │
            └──────────── clear ───────────────[DONE]◀──done───────┘
                                                  │
                                                  └──cancel──→[CANCELLING]
```

#### 4.1.2 消息类型 × UI 映射（17 个 EventType 全覆盖）

| EventType | UI 组件 | 默认状态 | 交互 |
| --- | --- | --- | --- |
| `user_message` | `<UserMessage>` 气泡右对齐 | — | 右键菜单：编辑并重发 / 复制 |
| `llm_request` | 隐藏（只作 debug） | — | Timeline 可见 |
| `llm_chunk` | 流入 `<AssistantMessage>` 文本节点 | — | rAF 合帧（§6.4） |
| `llm_response` | `<AssistantMessage>` 气泡左对齐 | — | Hover 显 token/cost；点击"重试"可重放 |
| `tool_call_emitted` | `<ToolCallCard>` 骨架 "requesting..." | — | 折叠 |
| `tool_invocation_started` | 卡片状态 "running..." + spinner | 折叠 | 点击展开参数 |
| `tool_invocation_finished` | 卡片状态 "ok/err" + 结果预览 | 行数 ≤ 5 自动展开；> 5 折叠 | 点击展开；右键"以此为参数重跑" |
| `skill_exec_started` | `<SkillBadge>` 进入 Activity 右栏 | — | Hover 显 skill name + version |
| `skill_exec_finished` | 同上，加 ✔/✗ | — | 点击跳到 Skills 详情 |
| `grader_verdict` | 紫色 `<GraderChip>` 嵌入该轮消息尾 | 压缩成图标 | 点击展开评分明细（4 项） |
| `cost_tick` | 右栏 "Cost" 数字实时动 | — | 点击跳 Insights |
| `session_lifecycle` | 银色系统消息条 "会话已重连" | — | — |
| `skill_candidate_proposed` | Evolution 页新卡片；聊天中出弹出 `<Toast>` | — | 点击 toast 跳转 |
| `skill_promoted` | 紫色 `<EvolutionBadge>` "XMclaw 学会了 X" | — | 点击跳 Skill 详情 |
| `skill_rolled_back` | 灰色 `<EvolutionBadge>` + undo 按钮 | — | 点击查看原因 |
| `anti_req_violation` | 红色 `<ViolationCard>` 放消息流中 | 展开 | 右键"加白名单" |
| `todo_updated` | 右栏 Todos 面板 checkbox 更新 | — | 点击勾/取消 |
| `prompt_injection_detected` | 琥珀色 `<SecurityNote>` 嵌入消息流 | 折叠 | 展开看 pattern_id |
| `memory_evicted` | 底部 footer "2 条记忆被淘汰" 灰字 | — | 点击跳 Memory |

#### 4.1.3 Plan / Act 双模式（抄 Claude Code / Cline）

- **Plan Mode**：LLM 输出全过审批、不调任何写工具、Tool Guardian 一律 `approve`。输入框下方显示蓝色横条 "Plan mode — tools require approval"。
- **Act Mode**：正常流。Tool Guardian 按 config.policy 自动分级。
- **切换**：`Ctrl/Cmd+Shift+P` 或输入框旁按钮 `[Plan] [Act]`。切换时 toast 提醒且写入事件 `session_lifecycle.mode_changed`。
- **Plan → Act 门槛**：从 Plan 退到 Act 前弹确认，列出"当前 plan 提案的所有步骤"让用户勾选哪些执行（抄 Cursor 的 Plan 模式）。

#### 4.1.4 Rewind（回到事件点继续）— XMclaw 专属

- Timeline panel 每个事件右侧有一个 `⤺ rewind here`
- 点击 → 弹确认 → 调 `POST /v2/sessions/{id}/rewind?event_id=...`（后端把会话状态截到该事件、分叉到新 session_id）
- 新 session 继续在右侧开第二列（双栏并排抄 LM Studio split view）

### 4.2 Agents

```
┌──────────────────────────────────────────────────────────────────────┐
│  Agents                                             [+ New Agent]   │
│ ───────────────────────────────────────────────────────────────────  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                 │
│  │ 🤖 Main       │ │ 🔬 Research   │ │ 🎨 Creative   │                 │
│  │ claude-sonnet│ │ gpt-4o       │ │ claude-haiku │                 │
│  │ 3 sessions   │ │ 1 session    │ │ idle         │                 │
│  │ ✨ 12 skills │ │ ✨ 4 skills  │ │ ✨ 2 skills  │                 │
│  │ $0.12/d      │ │ $0.03/d      │ │ $0.00/d      │                 │
│  │  [Open] [⋯]  │ │  [Open] [⋯]  │ │  [Open] [⋯]  │                 │
│  └──────────────┘ └──────────────┘ └──────────────┘                 │
└──────────────────────────────────────────────────────────────────────┘
```

**新建 Agent 向导**（5 步）：1. 取名/头像 → 2. 选 LLM provider + model → 3. 选 tools（checkbox 树）→ 4. 绑 skills（可选）→ 5. system prompt 模板 → "Create"。背后调 `POST /v2/agents`（当前未实现，需要补）。

### 4.3 Skills（仓库 + 版本矩阵）

```
┌──────────────────────────────────────────────────────────────────────┐
│  Skills                [🔍 search]  [filter: all ▼]  [+ New Skill]   │
│ ───────────────────────────────────────────────────────────────────  │
│  │ refactor_python         │ v3 (HEAD)  │ VFM 7.2 │ ▲ 3  │ [⋯] │    │
│  │   auto-generated        │ v2 👻       │ VFM 6.1 │      │      │    │
│  │                         │ v1 👻       │ VFM 5.0 │      │      │    │
│  │ explain_stack_trace     │ v1 (HEAD)  │ VFM 4.3 │      │      │    │
│  │ browser_scrape          │ v2 (HEAD)  │ VFM 6.8 │ ▲ 5  │      │    │
│  │                         │ v1 👻       │ VFM 5.4 │      │      │    │
│  └─────────────────────────┴────────────┴─────────┴──────┴──────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

- **HEAD badge**：当前激活版本
- **VFM**：Honest Grader 给的 Value-for-Money 分（0-10）
- **▲ N**：本周调用次数（sparkline 在点击后展开）
- **点击某版本行**：右栏打开 diff，两两对比（v2 vs v3）+ prompt patch + grader 对比卡
- **右键菜单**：Promote to HEAD / Demote / Delete / Export as .md / Share link

### 4.4 Evolution（XMclaw 独有）

```
┌──────────────────────────────────────────────────────────────────────┐
│  Evolution                                                          │
│ ───────────────────────────────────────────────────────────────────  │
│  VFM trend (last 30d)                    Skills: 24 / Genes: 147    │
│  ┌─────────────────────────────────┐                                │
│  │      .─○                        │   CANDIDATES (3)               │
│  │    .○                           │   ┌──────────────────────────┐ │
│  │  ○                              │   │ ⚡ refactor_async_v4     │ │
│  │○                                │   │   VFM 7.8 (prev 7.2) +0.6│ │
│  └─────────────────────────────────┘   │   evidence: 4 runs       │ │
│                                         │   [Promote] [Reject]     │ │
│  TODAY                                  └──────────────────────────┘ │
│  ✨ Learned: handle_timeout → 8 evals   ┌──────────────────────────┐ │
│  ♻ Rolled back: bad_parser → 0.3 drop  │ ⚡ browser_click_v3      │ │
│                                         └──────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

**Widget 清单**：
- **VFM trend chart**（SVG 手绘，30 px 高，30 个点）
- **Candidate card**：含 grader 四项评分 mini bar（accuracy / cost / latency / safety）
- **"Learned today" feed**：紫色小卡片流，点击展开对话片段
- **"Rolled back" 区**：灰色，记录回滚原因 + 一键 `/v2/skills/{name}/forgive`

### 4.5 Memory（三层浏览）

```
┌────────┬──────────────────────────────────────────────────────────┐
│ SHORT  │  layer: working   total: 1,284    size: 32.4 MB          │
│ 312    │ ─────────────────────────────────────────────────────── │
│        │  [🔍 keyword]  [tag: all ▼]  [session: all ▼]            │
│ WORKING│ ─────────────────────────────────────────────────────── │
│ 1,284  │  🏷 [identity] 2d ago  session=abc123                   │
│        │    "user prefers concise explanations"                   │
│ LONG   │    [📌 pin] [✏ edit] [🗑 delete]                        │
│ 4,902  │                                                          │
│        │  🏷 [user-profile] 5h ago                                │
│ ────── │    "working on Epic #20 backup feature"                  │
│ Pinned │    [📌 pinned] [✏] [🗑]                                 │
│ 47     │                                                          │
│        │  … 1,282 more                                            │
│ [Prune]│                                                          │
└────────┴──────────────────────────────────────────────────────────┘
```

- 左列是分层计数，点击切换
- 右侧虚拟滚动列表（抄 VS Code Problems 面板）
- 搜索框下拉提示可选 tag 和 session（参考 QwenPaw 的 Skill Pool 搜索与过滤分离）
- Prune 按钮点击弹对话框：选层 + 按时间或按容量，跑 `POST /v2/memory/prune`

### 4.6 Tools & MCP

```
Tools 页
┌──────────────────────────────────────────────────────────────┐
│  file_read         built-in    calls 247  avg 12ms  ✅       │
│  file_write        built-in    calls 98   avg 21ms  ✅       │
│  bash              built-in    calls 45   avg 340ms ✅       │
│  browser.click     playwright  calls 12   avg 1.2s  ⚠ slow   │
│  github.search     mcp:github  calls 3    avg 480ms ✅       │
│  [+ Install MCP server]                                      │
└──────────────────────────────────────────────────────────────┘
```

点击单行 → 右栏展开最近 20 次调用的历史（timestamp / args / result / duration）。

MCP 页：每个服务器一张卡，含 "status"（绿/红点）、"discovered tools" 数、[Restart] [Remove] 按钮。

### 4.7 Security & Approvals

```
Approval Queue
┌──────────────────────────────────────────────────────────────┐
│  🟠 HIGH · file_write · session abc123                       │
│    path: /etc/hosts                                          │
│    content preview: [▼ expand 3 lines]                       │
│    guardian: FilePathToolGuardian (sensitive path)           │
│    [Approve] [Reject] [⚙ Edit params]                        │
│ ───────────────────────────────────────────────────────────  │
│  🟡 MEDIUM · bash · session abc123                           │
│    cmd: rm -rf ~/Downloads/tmp                               │
│    guardian: ShellEvasionGuardian                            │
│    [Approve] [Reject]                                        │
└──────────────────────────────────────────────────────────────┘
```

- **可编辑参数**（点 ⚙ 打开表单）后再批准 — 抄 LM Studio MCP confirmation dialog
- **批量批准**（勾 checkbox 再"Approve selected"）
- **Remember decision**：勾 "Always allow `file_write` in this session" → 写入内存 ACL，当前会话不再问

### 4.8 Backup（Epic #20 落地后）

```
┌──────────────────────────────────────────────────────────────┐
│  Backups                          [+ Create Backup]  [⚙]    │
│ ───────────────────────────────────────────────────────────  │
│  auto-20260425-060000        42.3 MB   today 06:00  verified │
│    [Info] [Download] [Restore] [Delete]                      │
│  before-refactor             38.1 MB   2d ago       verified │
│    📌 pinned (manual)                                        │
│  auto-20260424-060000        41.8 MB   1d ago                │
│ ───────────────────────────────────────────────────────────  │
│  Scheduler: ⚡ auto-daily ON · keep 7 · prefix "auto-"       │
│  [Pause] [Run now] [Settings]                                │
└──────────────────────────────────────────────────────────────┘
```

Restore 按钮点击 → 二次确认（打印 "这会关闭当前 daemon、替换 workspace、重启" 红色警告）→ 调 `POST /v2/backups/{name}/restore` → 显示进度 → 自动 WS 重连后换装。

### 4.9 Settings（深度 + 宽度）

左右两栏，左侧是 section 树（LLM Providers / Integrations / Appearance / Shortcuts / Advanced），右侧是表单。

关键 section：

- **LLM Providers**：每个 provider 一张卡（api_key 脱敏显示 `sk-***xxxx` + 👁 查看 + 🔑 rotate）+ base_url + default model 选择 + "test connection" 按钮
- **Integrations**（Slack / Discord / Telegram / Notion / GitHub）：enable 开关 + 最小必要字段 + test 按钮
- **Appearance**：主题（light / dark / system / soft-dark / high-contrast）+ 字号 scale（0.85 / 1.0 / 1.15）+ 密度（comfortable / compact）
- **Shortcuts**：完整快捷键列表，支持自定义重绑（参考 Open WebUI `shortcuts.ts` 的结构）
- **Advanced**：config.json 编辑器（monaco-lite 或 pure textarea + JSON validator）；一键"revert to example"；版本卡（当前 v0.2.1 · 有更新 v0.2.2 可升）——避免 QwenPaw 版本错位坑

### 4.10 Doctor

```
┌──────────────────────────────────────────────────────────────┐
│  System Diagnostics                       [Run] [--fix all] │
│ ───────────────────────────────────────────────────────────  │
│  ✓ Python 3.11.2                                              │
│  ✓ dependencies (43 pinned)                                  │
│  ⚠ Playwright browsers not installed                         │
│     → `playwright install chromium`   [Auto-fix]             │
│  ✓ config.json readable                                      │
│  ✓ anthropic API key configured                              │
│  ⚠ 2 backups, newest 3d old (consider `backup create`)       │
│     → [Create backup now]                                     │
│  ✓ event bus writable                                        │
│  …                                                            │
└──────────────────────────────────────────────────────────────┘
```

每条 check 独立卡片、`--fix` 按钮（能修的才显示）。背后 `GET /v2/doctor` 返回 14 条 check 结果；"Auto-fix" 调 `POST /v2/doctor/fix?check=<id>`。

### 4.11 Insights（dashboard）

三个 tab：**Usage**（消息/会话/tool 时间序列）· **Cost**（按 provider / model / session 的堆叠柱状）· **Learning**（VFM 增长 + skill 列表按提升幅度排序）。

纯 SVG 手绘，无 d3/chart.js（维持无 Node.js 约束）。

---

## 5. 组件库清单

### 5.1 层级

```
/ui/ 下的 ES module 结构
  tokens.css           — §9 的所有 CSS 变量定义
  components/
    atoms/
      button.js        — 7 变体: primary / secondary / ghost / danger / success / approval / icon
      chip.js          — 状态 chip: success / warn / error / info / evolution
      input.js         — text / textarea / number / password
      checkbox.js
      radio.js
      toggle.js
      avatar.js
      icon.js          — <Icon name="…" /> 包 inline SVG
      spinner.js
      progress.js
      divider.js
      kbd.js           — <Kbd>Ctrl+K</Kbd>
    molecules/
      tooltip.js
      popover.js
      dropdown.js
      context-menu.js  — 右键菜单
      tab-bar.js
      search-box.js
      command-palette.js
      toast.js
      dialog.js
      confirm.js       — 基于 dialog 的二次确认
      empty-state.js
      code-block.js    — 含 copy / run-in-terminal / insert
      diff-view.js     — side-by-side monospace diff
      collapsible.js   — 基于行数阈值的自动折展（抄 Cline）
      kbd-registry.js  — shortcut 展示表
    organisms/
      sidebar.js
      status-bar.js
      top-bar.js
      chat-view.js     — 整体 chat 容器
      message-list.js
      user-message.js
      assistant-message.js
      tool-call-card.js
      grader-chip.js
      evolution-badge.js
      approval-card.js
      skill-card.js
      skill-version-matrix.js
      memory-item.js
      backup-card.js
      doctor-check-card.js
      cost-sparkline.js
      vfm-chart.js
    pages/
      chat-page.js
      agents-page.js
      skills-page.js
      evolution-page.js
      memory-page.js
      tools-page.js
      mcp-page.js
      security-page.js
      backup-page.js
      doctor-page.js
      insights-page.js
      settings-page.js
```

### 5.2 约束

- 每个文件 ≤ 500 行；超了必须拆（Continue `StepContainer/` 范本）。
- 每个组件默认 export 一个 `create(…)` 工厂函数（若走 vanilla / Alpine）或 default React/Preact component（若走 Preact+htm）—— 选型见 §11。
- 所有组件必须接受 `className` 合并、`data-testid` 透传。
- **不引外部组件库**；所有 atom 自己写。`@headlessui/react` 之类若要用，必须可通过 ESM CDN 加载（maintainer 不能引新构建）。

---

## 6. 状态树 + WebSocket 事件映射表

### 6.1 Store shape（单一状态树）

```ts
type RootState = {
  connection: {
    status: "connecting" | "open" | "reconnecting" | "closed" | "error"
    pairingTokenStatus: "pending" | "ok" | "rejected"
    lastPongTs: number
  }
  agents: Record<AgentId, Agent>
  activeAgentId: AgentId | null
  sessions: Record<SessionId, SessionMeta>
  activeSessionId: SessionId | null
  messages: Record<SessionId, Message[]>          // 截断 500 条 + "load older"
  streamingBuffers: Record<MessageId, string>     // rAF 合帧缓冲
  toolCalls: Record<ToolCallId, ToolCall>
  approvals: Record<ApprovalId, PendingApproval>
  skills: Record<SkillName, Skill>                // 含 versions: SkillVersion[]
  evolution: {
    candidates: Candidate[]
    vfmSeries: Array<{ ts: number; vfm: number }>
    learnedToday: LearnedEntry[]
    rolledBack: RollbackEntry[]
  }
  memory: {
    counts: { short: number; working: number; long: number; pinned: number }
    pageCache: Record<string, MemoryItem[]>
  }
  tools: Record<ToolName, ToolStats>
  mcp: Record<ServerId, McpServer>
  backups: BackupEntry[]
  backupScheduler: { enabled: boolean; keep: number; lastRunTs: number | null }
  doctor: { lastRunTs: number | null; results: DoctorCheck[] }
  insights: { cost: CostSeries; usage: UsageSeries }
  ui: {
    theme: "light" | "dark" | "system" | "soft-dark" | "high-contrast"
    fontScale: 0.85 | 1.0 | 1.15
    density: "comfortable" | "compact"
    sidebarCollapsed: boolean
    workspaceDockWidth: 0 | 320 | 480 | 640
    activeWorkspacePanel: string
    activePage: string
    commandPaletteOpen: boolean
    planMode: boolean
    router: { path: string; params: Record<string, string> }
  }
  settings: { llm: LlmSettings; integrations: Integrations; ... }
  toasts: Toast[]
  violations: Violation[]
  cost: { totalUsd: number; totalTokens: number }
}
```

### 6.2 Store 实现策略（无 Node.js）

- **一个 `store.js` 单例**（单向数据流）：
  ```js
  const listeners = new Set()
  let state = initial
  export function getState() { return state }
  export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn) }
  export function dispatch(action) {
    const next = reducer(state, action)
    if (next !== state) { state = next; listeners.forEach(l => l(state)) }
  }
  ```
- Reducer 按 slice 分文件：`sessionSlice.js` / `skillSlice.js` …
- 持久化：**白名单** localStorage（抄 Continue）：`ui.theme, ui.fontScale, ui.density, activeAgentId, sidebarCollapsed, shortcuts.custom`
- **不持久化**：所有 streaming buffers、approvals、messages（重连走 `/v2/events/replay`）

### 6.3 WebSocket 事件 → action 映射表

| WS 消息类型 | 产生 action | Store 变更 |
| --- | --- | --- |
| `hello` | `CONN/HELLO` | connection.status = "open" |
| `session_lifecycle` | `SESSION/LIFECYCLE` | sessions[id] 更新；触发 toast |
| `user_message` | `MSG/USER_ADD` | messages[session_id].push |
| `llm_request` | `MSG/LLM_REQ` | 当前会话进入 streaming |
| `llm_chunk` | `MSG/LLM_CHUNK` | streamingBuffers[msg_id] += chunk（rAF flush） |
| `llm_response` | `MSG/LLM_DONE` | 把 buffer flush 到 messages；清 streamingBuffers[msg_id] |
| `tool_call_emitted` | `TOOL/EMIT` | toolCalls[id] 创建（pending） |
| `tool_invocation_started` | `TOOL/START` | toolCalls[id].state = "running" |
| `tool_invocation_finished` | `TOOL/FINISH` | toolCalls[id].result / state = "ok\|err" |
| `skill_exec_started` | `SKILL/EXEC_START` | skills[name].runningSessionIds.add |
| `skill_exec_finished` | `SKILL/EXEC_DONE` | skills[name].runningSessionIds.delete；recent.push |
| `grader_verdict` | `GRADER/VERDICT` | 找到本轮消息尾附加 verdict |
| `cost_tick` | `COST/TICK` | cost += chunk；触发 insights 更新 |
| `skill_candidate_proposed` | `EVOLUTION/CANDIDATE` | candidates.push；toast |
| `skill_promoted` | `EVOLUTION/PROMOTED` | skill version 升；learnedToday.push |
| `skill_rolled_back` | `EVOLUTION/ROLLED_BACK` | rolledBack.push |
| `anti_req_violation` | `SECURITY/VIOLATION` | violations.push；消息流插入 card |
| `prompt_injection_detected` | `SECURITY/INJECTION` | 消息流插入 SecurityNote |
| `memory_evicted` | `MEMORY/EVICT` | memory.counts--；footer toast |
| `todo_updated` | `TODO/UPDATE` | workspace todos panel 同步 |
| WS `close` | `CONN/CLOSE` | status = "reconnecting"；重试 backoff (1,2,4,8,15)s |

### 6.4 流式 Markdown 渲染（抄 Open WebUI 精髓）

```js
// assistant-message.js（伪代码）
let frameBufferByMsg = new Map()
let rafScheduled = false

function enqueueChunk(msgId, chunk) {
  const cur = frameBufferByMsg.get(msgId) ?? ""
  frameBufferByMsg.set(msgId, cur + chunk)
  if (!rafScheduled) {
    rafScheduled = true
    requestAnimationFrame(flush)
  }
}

function flush() {
  rafScheduled = false
  for (const [msgId, buffer] of frameBufferByMsg) {
    const tokens = parseMarkdownToTokens(buffer)   // 用 marked.lexer
    const view = document.getElementById(`msg-${msgId}`)
    reconcileTokens(view, tokens)                  // keyed 子节点 diff
  }
  frameBufferByMsg.clear()
}
```

**关键细节**（Continue `StyledMarkdownPreview/index.tsx:225,309-311` 的经验）：
- code block 用 `data-codeblock-index` 稳定 id，流式过程不销毁重建
- Token-level diff 最粗糙的实现：维护 `children.length`，新增的从末尾 append；最后一个 paragraph 用 `textContent =` 追加
- 长回答（> 2000 token）时把 fence 之前的 token 数组缓存、不重解析

### 6.5 取消 / 重放 / 分叉

- **取消**：按钮点击 → `ws.send({type:"cancel", session_id})` → 后端响应 `llm_response` with `cancelled: true` → 前端在消息末尾追加灰色 "(cancelled)"
- **重试**：消息气泡右键 "Retry" → `POST /v2/sessions/{id}/messages/{msg_id}/retry` → 后端产生新 llm_request 与原消息同 `parent_id`，UI 侧以"分支"渲染（抄 Claude Code 的 branch tree，但简化到 2 层）
- **分叉**：同上但从 user 消息开始、且改内容 → 新 session_id

---

## 7. 交互规范

### 7.1 快捷键注册表（全局 + 页面）

所有快捷键集中在 `shortcuts.js` registry，可在 Settings → Shortcuts 页重绑。

| 快捷键 | 分类 | 动作 | 出处 |
| --- | --- | --- | --- |
| `Ctrl/Cmd+K` | Global | 打开命令面板 | Open WebUI |
| `Ctrl/Cmd+Shift+O` | Chat | 新会话 | Open WebUI |
| `Ctrl/Cmd+Shift+S` | Global | 切 Sidebar | Open WebUI |
| `Alt+\` | Global | 切 Workspace dock | 自创 |
| `Ctrl/Cmd+/` | Global | 显示所有快捷键 | Open WebUI |
| `Ctrl/Cmd+,` | Global | 打开 Settings | VSCode 惯例 |
| `Ctrl/Cmd+L` | Chat | 清空当前会话（软） | Continue |
| `Ctrl/Cmd+I` | Chat | 聚焦输入框 | Continue |
| `Ctrl/Cmd+Shift+P` | Chat | 切 Plan/Act | 自创 |
| `Ctrl/Cmd+Enter` | Chat Input | 发送 | 全行业 |
| `Shift+Enter` | Chat Input | 换行 | 全行业 |
| `Ctrl/Cmd+Backspace` | Chat | 取消当前生成 | Continue |
| `@` | Chat Input | 触发上下文选择 | Continue/Cline |
| `/` | Chat Input | 触发斜杠命令 | Cline |
| `Esc` | Global | 依次：关弹窗 / 停生成 / 聚焦输入 | Open WebUI |
| `Ctrl/Cmd+R` | Chat | 重新生成最后回复 | Open WebUI |
| `Ctrl/Cmd+Shift+;` | Chat | 复制最后代码块 | Open WebUI |
| `Ctrl/Cmd+Shift+C` | Chat | 复制最后回复 | Open WebUI |
| `Ctrl/Cmd+Shift+Enter` | Approval | 批准高亮中的审批 | 自创 |
| `Ctrl/Cmd+Shift+Backspace` | Approval | 拒绝高亮中的审批 | Continue |
| `Alt+Up/Down` | Chat | 上/下翻会话 | VSCode-ish |
| `Ctrl/Cmd+1..9` | Global | 跳 Sidebar 第 N 项 | VSCode |
| `Ctrl/Cmd+Shift+D` | Global | 切主题 light/dark | 自创 |
| `F1` | Global | 同 Ctrl+K | VSCode |

### 7.2 命令面板（Ctrl+K）

- 模糊搜索三类条目：**动作**（跳页面、切主题、跑 doctor …）· **实体**（会话 / agent / skill / memory）· **最近历史**
- 实现参考 VSCode command palette：顶部输入框、下方最多 7 条结果、回车执行
- 实现约束：fzf-style 模糊匹配用手搓 Sellers 算法（< 100 行 JS）

### 7.3 右键菜单（按上下文分派）

| 对象 | 菜单项 |
| --- | --- |
| User message | 编辑并重发 / 复制 / 删除（本地） |
| Assistant message | 重试 / 复制 / 复制为 markdown / 分叉新会话 |
| Tool call card | 复制参数 / 以此参数重跑 / 跳到 Timeline |
| Code block | 复制 / 插入到文件 / 在终端运行 / 应用为 diff |
| Skill card | Promote / Demote / Export / Delete |
| Session item | 重命名 / 置顶 / 导出 JSON / 删除 |
| Memory item | Pin / Edit / Delete / Copy id |
| Approval card | Approve / Reject / Edit params / Always allow this session |

### 7.4 拖拽（DnD）

- **文件拖入输入框** → 调 `/v2/uploads` 上传，插入 `@file:<path>` 标记
- **会话拖动排序** → 调 `PATCH /v2/sessions/{id}?position=N`
- **Sidebar 一级条目重排** → 本地 localStorage
- **Skill card 拖到 Agent card 上** → 给该 agent 绑定该 skill（快捷操作）

### 7.5 @ 上下文提供者（抄 Continue/Cline）

输入框内敲 `@` 触发下拉，按类型筛：

| Provider | 触发 | 插入形式 |
| --- | --- | --- |
| `@file <path>` | `@file` | `@file:xmclaw/core/bus/events.py` |
| `@skill <name>` | `@skill` | `@skill:refactor_python@v3` |
| `@agent <name>` | `@agent` | `@agent:research` |
| `@memory <tag>` | `@memory` | `@memory:identity` |
| `@event <id>` | `@event` | `@event:evt_abc123`（插入时间戳引用） |
| `@session <id>` | `@session` | `@session:abc123`（跨会话引用） |

### 7.6 / 斜杠命令

| 命令 | 作用 | 实现 |
| --- | --- | --- |
| `/plan` | 切 Plan 模式 | 前端 dispatch ACTION/PLAN |
| `/act` | 切 Act 模式 | 同上 |
| `/clear` | 软清空当前会话 | 只清 UI，不动 daemon |
| `/rewind <N>` | 回退 N 步 | `POST /v2/sessions/{id}/rewind` |
| `/approve` | 批准最新高亮审批 | 同快捷键 |
| `/skill <name>` | 手动调一个 skill | `POST /v2/skills/{name}/invoke` |
| `/doctor` | 跑 doctor | 跳到 doctor 页并触发 run |
| `/backup` | 触发备份 | 同 Backup 页的"Run now" |
| `/status` | 打印 daemon 状态 | 聊天流里插系统消息 |
| `/export` | 导出当前会话为 markdown | 前端生成并下载 |
| `/theme <name>` | 切主题 | 同 Ctrl+Shift+D |

### 7.7 焦点与 Tab 顺序

全局 Tab 顺序：Top Bar → Sidebar → Main View → Workspace Dock → Status Bar，每个区域内再按视觉从上到下、从左到右。所有交互元素有可见 `:focus-visible` outline（§9.5）。

---

## 8. 权限与审批 UI（前端高权限落地）

### 8.1 审批流（Epic #3 Tool Guardian 对接）

```
            ┌──────── rejected → [ToolError] → 继续下条
            │
tool_call_emitted
            │
            ▼
      NEEDS_APPROVAL:{id}  ────→  前端 store.approvals[id] 新增
            │                              │
            │                              ▼
            │                       ApprovalCard 渲染 + toast
            │                              │
            │                 ┌────approve──┤
            │                 │             │
            │                 │             └────reject──┐
            │                 ▼                          ▼
            │          POST /v2/approvals/{id}/approve   POST /v2/approvals/{id}/reject
            │                 │                          │
            │                 ▼                          ▼
            └←── tool_invocation_started ←──       cancel tool; emit anti_req_violation
```

### 8.2 ApprovalCard 组件规格

```
┌─────────────────────────────────────────────────────────┐
│ 🟠 HIGH · file_write                     2s ago     [⋯] │
│  path:  /etc/hosts                                       │
│  args preview:                                           │
│    127.0.0.1 example.com                                 │
│    [▼ expand full content]                               │
│                                                           │
│  triggered by: FilePathToolGuardian                      │
│  rule: sensitive_paths contains "/etc"                   │
│  severity: high → policy: approve                        │
│                                                           │
│  [Approve]  [Reject]  [⚙ Edit]  [□ Remember this session]│
└─────────────────────────────────────────────────────────┘
```

- **严重度点**：critical=红、high=橙、medium=黄、low=蓝、info=灰
- **Edit 参数**：按 JSON Schema 渲染表单（读 Tool 的 args schema）。参数被编辑后 Approve 时 POST `{id, approved_args: {...}}` —— 后端用 approved_args 覆盖原 call（抄 LM Studio）
- **Remember this session**：前端写内存 ACL + 下一个同工具同参数范围调用时**自动** POST approve
- **批量操作**：Security 页提供 "Approve all low"、"Reject all critical" 的策略快捷按钮（但写入 audit log）

### 8.3 "前端高权限"的边界

| 能做 | 不能做 |
| --- | --- |
| 批准 / 拒绝单条审批 | 在没有 token 的情况下批准 |
| 在会话内标记"always allow" | 跨会话自动沿用（必须显式写 config） |
| 编辑 tool args 后批准 | 修改 guardian policy（必须进 Settings） |
| 一键 rewind + 分叉 | 跨 agent 搬运上下文（必须用 /v2/memory API） |
| 删除本地 UI 中的消息 | 删除 SQLite bus 里的事件（bus 是不可变日志） |

### 8.4 审计日志

所有审批 / 编辑参数 / 策略变更自动写 `anti_req_violation` 事件（带 reason="user_approved" / "user_rejected_with_override"），Security 页"Audit Log" tab 可审计过去 30 天。

---

## 9. 视觉规范

### 9.1 CSS 变量分层（双模 token）

```css
/* tokens.css */
:root {
  /* Scale anchors */
  --app-text-scale: 1;
  --radius-sm: 4px; --radius-md: 8px; --radius-lg: 12px;
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px;
  --sidebar-w: 260px;
  --topbar-h: 40px;
  --statusbar-h: 24px;

  /* Typography */
  --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont,
               "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  --fs-xs: 11px; --fs-sm: 12px; --fs-base: 13px; --fs-md: 14px;
  --fs-lg: 16px; --fs-xl: 20px; --fs-2xl: 24px;
  --fw-regular: 400; --fw-medium: 500; --fw-semibold: 600;

  /* Private XMclaw palette (light default) */
  --xmc-bg: #ffffff;
  --xmc-bg-elevated: #fafafa;
  --xmc-bg-hover: #f3f4f6;
  --xmc-fg: #111827;
  --xmc-fg-muted: #6b7280;
  --xmc-border: #e5e7eb;
  --xmc-primary: #5b21b6;          /* 进化紫 */
  --xmc-accent: #2563eb;            /* 信息蓝 */
  --xmc-success: #10b981;
  --xmc-warn: #f59e0b;
  --xmc-danger: #ef4444;
  --xmc-approval: #f97316;          /* 审批橙 */
  --xmc-evolution: #a855f7;         /* 学习紫（高饱和） */

  /* Semantic tokens: fall back to VSCode host if embedded */
  --tok-bg:       var(--vscode-sideBar-background, var(--xmc-bg));
  --tok-bg-alt:   var(--vscode-editor-background, var(--xmc-bg-elevated));
  --tok-fg:       var(--vscode-foreground, var(--xmc-fg));
  --tok-fg-muted: var(--vscode-descriptionForeground, var(--xmc-fg-muted));
  --tok-border:   var(--vscode-panel-border, var(--xmc-border));
  --tok-primary:  var(--vscode-button-background, var(--xmc-primary));
  --tok-danger:   var(--vscode-errorForeground, var(--xmc-danger));
}

:root[data-theme="dark"] {
  --xmc-bg: #0f172a;
  --xmc-bg-elevated: #1e293b;
  --xmc-bg-hover: #334155;
  --xmc-fg: #f1f5f9;
  --xmc-fg-muted: #94a3b8;
  --xmc-border: #334155;
  --xmc-primary: #a78bfa;
  --xmc-accent: #60a5fa;
  --xmc-success: #34d399;
  --xmc-warn: #fbbf24;
  --xmc-danger: #f87171;
  --xmc-approval: #fb923c;
  --xmc-evolution: #c084fc;
}

:root[data-theme="high-contrast"] {
  --xmc-bg: #000000;
  --xmc-fg: #ffffff;
  --xmc-border: #ffffff;
  /* 其余同 dark，但增加边框宽度 */
}

html { font-size: calc(16px * var(--app-text-scale)); }   /* 抄 Open WebUI */
```

### 9.2 模式切换

- Theme: `document.documentElement.dataset.theme = "dark"`（立即生效）
- Font scale: `document.documentElement.style.setProperty('--app-text-scale', 1.15)`
- 所有 token 走 `var(--tok-*)` —— 切换时不需重渲染

### 9.3 间距 & 排版规则

- 垂直节奏：相邻块 `--space-4`（16px）；分节 `--space-6`（32px）
- 行高：正文 `1.5`；代码 `1.4`
- 最大可读宽度（chat 气泡）：`72ch`（约 720 px）—— 对应长代码块 scroll-x
- 代码块内边距：`--space-3 --space-4`，圆角 `--radius-md`

### 9.4 动画

全局 easing `cubic-bezier(0.2, 0, 0.13, 1.5)`（轻微弹跳）；时长三档 `100ms / 200ms / 350ms`。

| 场景 | 时长 | 属性 |
| --- | --- | --- |
| 按钮 hover / active | 100 ms | bg-color, transform scale(0.98) |
| Tooltip 出现 | 100 ms | opacity, translateY |
| Dialog / Toast 进入 | 200 ms | opacity, translateY |
| Tab 切换 | 200 ms | opacity + translateX(8px) |
| Sidebar 折叠 | 350 ms | width, transform |
| 流式文本 | 无动画 | rAF 合帧已够 |
| Evolution badge 闪光 | 2× 500 ms pulse | box-shadow |

`prefers-reduced-motion: reduce` 下统一降到 0（§10.1）。

### 9.5 Focus ring

```css
*:focus-visible {
  outline: 2px solid var(--xmc-accent);
  outline-offset: 2px;
  border-radius: inherit;
}
```

High-contrast 下加粗到 3px。

### 9.6 Density

`comfortable` 是默认，`compact` 时：

- padding 减 25%
- 字号 -1px
- 行高 1.4

通过 `:root[data-density="compact"]` 作用域变量实现。

### 9.7 Empty states

每个页面都有专属空态：线描图标 + 一句引导 + 一个 CTA 按钮（抄 GitHub 空态）。示例："No skills yet — XMclaw will propose one after the first 5-tool task. [Run doctor to verify evolution engine]"。

---

## 10. 无障碍 + 国际化

### 10.1 A11y 硬要求

- **键盘全覆盖**：tab 顺序、Enter/Space 激活、方向键在列表中导航（抄 VSCode）
- **ARIA 角色**：sidebar=`navigation`、main=`main`、dialog=`dialog` aria-modal、toast=`status` polite、approval=`alert` assertive
- **屏幕阅读器朗读流**：流式输出时给 `<div aria-live="polite">` 外层；结束后朗读 "assistant finished"
- **色盲友好**：所有颜色语义必有"形状 / 图标 / 文字"辅助（不靠颜色单独传达）
- **最小对比度**：4.5 正文、3 大字（WCAG AA）；high-contrast 主题达 AAA
- **prefers-reduced-motion**：在 `@media (prefers-reduced-motion: reduce)` 下把所有动画压成 0

### 10.2 I18n

已有 `xmclaw/i18n/` 作 CLI 基础；前端从日常 `.json` 词典加载，默认 zh-CN + en-US（与 doctor i18n 风格一致）。

- 所有 UI 字串走 `t("key.path")`；源语言 zh-CN
- 日期、数字按浏览器 locale（`Intl.DateTimeFormat`）
- RTL 支持：`dir="auto"`；组件内避免硬编码 `margin-left`，用 `margin-inline-start`

语言列表：zh-CN（锚）· en-US · ja-JP · ko-KR · es-ES · fr-FR（第一批）。社区贡献其他语言直接加 `.json`。

---

## 11. 技术选型决策树

### 11.1 强约束回顾

1. **无 Node.js 构建**（CLAUDE.md）→ 任何构建工具（Vite / esbuild / rollup）排除
2. **FastAPI `StaticFiles` 直出** → 静态资源从 `xmclaw/daemon/static/` 发
3. **大小预算**：首屏 `≤ 300 KB` 原始、`≤ 80 KB` gzipped；JS 可以更大（ES module 按需加载）
4. **低维护**：项目人力 1-2 人，不能每月追 framework 大升级
5. **可嵌入**：未来可能跑在 VSCode webview / Cursor webview（Continue + Cline 的路径）

### 11.2 候选方案

| 方案 | 特点 | 大小 | 学习成本 | 可嵌入 | 生态 | 评分 |
| --- | --- | --- | --- | --- | --- | --- |
| **纯 vanilla**（现状） | 无依赖；手搓 all | 0 KB deps | 低 | ✔ | 0 | ★★★ |
| **Preact + htm（ESM CDN）** | JSX-like 但无构建；~10 KB | 10 KB | 中 | ✔ | 小 | ★★★★★ |
| **Alpine.js** | 类 Vue 声明式，HTML attribute 驱动 | 15 KB | 低 | ✔ | 中 | ★★★★ |
| **Lit** | Web Component 原生；tag-based | 18 KB | 中 | ✔ | 中 | ★★★★ |
| **Svelte**（需编译） | 最终产物小但必须构建 | — | 中 | ✔ | 大 | ✗（违反无 Node 构建） |
| **React/Vue + Vite** | 生态最强但构建步骤 | — | 高 | ✔ | 最大 | ✗（违反无 Node 构建） |

### 11.3 推荐

**Preact + htm + 小型手搓 store**，理由：
1. 最接近 React 写法、未来迁移 React 成本低
2. `<script type="module">` import 即用，无构建
3. htm 用模板字符串代替 JSX，无需 babel
4. 10 KB 可塞满 ESM cache
5. 组件心智模型和 Continue / Cline 完全一致 → 照搬他们源码的组件写法

示例：
```html
<script type="module">
  import { h, render } from "https://esm.sh/preact@10"
  import htm from "https://esm.sh/htm@3"
  const html = htm.bind(h)
  const App = () => html`<div class="app">Hello</div>`
  render(html`<${App} />`, document.getElementById("root"))
</script>
```

**兜底方案**：如果 preact ESM CDN 在中国访问不稳，fallback 到 **Alpine.js 自托管**（把 15 KB alpine.min.js 放进 `static/vendor/`）。

### 11.3.1 最终决策（Phase 0 敲定，2026-04-25）

**选定 Preact + htm，双轨加载策略**：

1. **主加载路径**：`https://esm.sh/preact@10` + `https://esm.sh/htm@3`（ESM module）
2. **自托管兜底**：`static/vendor/preact.min.js` + `static/vendor/htm.min.js`（`scripts/fetch_vendor.py` 落盘）
3. **运行时选择**：`bootstrap.js` 先试 CDN，5 秒超时或解析失败自动切自托管；结果写 `localStorage.xmc_bootstrap_source` 便于诊断
4. **离线/内网场景**：`config.json` 里给 `frontend.assets_mode = "local"` 强制走自托管，直接跳过 CDN 探测

理由：
- 不让"中国访问性"阻塞开发节奏（CDN 通就省 15 KB 首屏带宽，不通就退自托管）
- 用户只需一个开关就切换，不牵涉代码改动
- Phase 0 的实际任务是**把骨架落下来**，不是在这里决网络路径 — 两个路径都留口子

**未选 Alpine 的原因**：经过 §11.3 重审，Alpine 的模板指令（`x-for`/`x-if`/`x-show`）在超过 Chat 页规模后会让 HTML 模板越写越像 "PHP 回归"，与 §5 的 "组件化 atoms/molecules/organisms" 心智冲突；而 Preact + htm 的模板字符串本质就是 JSX，能严格按 §5 拆文件、和 §1.4 的"单文件 ≤ 500 行"硬约束天然契合。Alpine 作为 Plan B 保留在 §11.3 兜底段，短期不落代码。

### 11.4 放弃的能力（诚实列出）

- TS 类型检查（只能靠 JSDoc `@typedef`）
- 热重载（开发时靠浏览器硬刷）
- Tailwind（靠 tokens.css 的 utility 替代）
- React DevTools / Redux DevTools（自建"mini DevTools"悬浮面板，显示最近 50 个 action）

---

## 12. 分阶段实施路线

> 作为 **Epic #23 · 前端（Web UI 首版）** 提交到 `docs/DEV_ROADMAP.md` §4；下列阶段严格串行，每阶段一个 milestone demo。

### Phase 0 · 技术预研（1 周）

- [x] 决定最终选型（Preact+htm vs Alpine）并在 `docs/FRONTEND_DESIGN.md` §11 加"最终决策"条目
- [x] 写 `xmclaw/daemon/static/` 目录骨架 + tokens.css + router.js + store.js
- [x] 双轨加载器：ESM CDN + `scripts/fetch_vendor.py` 自托管兜底
- [x] 写 5 个 atom 组件 + pytest 门卫测试（文件存在 / HTML 连线 / 500 行预算 / `/ui/*` 可达）

### Phase 1 · Chat 骨架 + 连通性（2 周）

- [ ] 三栏布局 + TopBar + StatusBar + Sidebar 静态版
- [ ] Chat 页：UserMessage / AssistantMessage / ToolCallCard 三组件
- [ ] WS 接入 + §6.3 完整事件映射
- [ ] 流式 Markdown 渲染（§6.4）通过 token 级 keyed diff 测试：回答 5000 字内无 jank
- [ ] Plan/Act 切换 + `Ctrl+Shift+P`
- [ ] @ 上下文 + / 斜杠命令（至少 `/plan /act /clear` 三条）
- [ ] 验收：回放一份 500 事件的会话 ≤ 2s 完成加载

### Phase 2 · 高权限交互（2 周）

- [ ] ApprovalCard + 批准流
- [ ] Edit params 表单（JSON Schema → form）
- [ ] Rewind / Retry / Branch
- [ ] 命令面板（Ctrl+K）+ 至少 20 个 action
- [ ] 完整快捷键 registry + Settings→Shortcuts 页
- [ ] 验收：完成 §7.1 所有快捷键；命令面板 50ms 以内模糊搜索

### Phase 3 · 所有 Sidebar 页（3 周）

- [ ] Agents / Skills（含版本矩阵 + diff）
- [ ] Memory 三层浏览 + prune
- [ ] Tools / MCP
- [ ] Security 队列 + audit log
- [ ] Backup 列表 + restore
- [ ] Doctor + auto-fix
- [ ] Settings 全量表单
- [ ] 验收：§2.4 的 16 个领域在 UI 上都有入口（Live / Todos / Timeline / Files / Sessions / Agents / Skills / Memory / Tools / MCP / Security / Backup / Doctor / Insights / Settings / Evolution）

### Phase 4 · 进化 & Insights（2 周）

- [ ] Evolution 页：VFM chart + candidate cards + learned-today feed
- [ ] "XMclaw 今天学会了什么"首页小卡片流
- [ ] Insights dashboard（usage / cost / learning 三 tab）
- [ ] Status bar 实时 cost 显示
- [ ] 验收：在跑一个跑过 10 个 5+ tool 任务的 workspace 时，Evolution 页上的所有数字都能对上 SQLite bus 的真实事件

### Phase 5 · 打磨 + a11y + i18n（2 周）

- [ ] 所有页面的空态
- [ ] 所有 ARIA 角色 + 键盘全覆盖
- [ ] 英文全文翻译
- [ ] prefers-reduced-motion + high-contrast 主题
- [ ] 性能回归：Chrome Performance 录一份回放 ≥ 55fps
- [ ] 验收：盲人 / 弱视 / 色盲用户能完整走完 "新会话 → 批准工具 → 看结果 → 导出" 五步

### Phase 6 · 发布打磨（1 周）

- [ ] 端到端 smoke：走 "新会话 → 流式回答 → 批准工具 → rewind → 查 Evolution" 全路径
- [ ] 更新 README + 截图
- [ ] PR 合并后发 v0.3.0

---

## 附录 A · 首版覆盖范围对照（§2.4 16 个领域 → UI 实现点）

| 维度 | UI 目标 |
| --- | --- |
| 行数 | ≤ 4000 行（含 12 页） |
| 架构 | 页 + 组件分文件，每文件 ≤ 500 |
| 路由 | history.pushState |
| 快捷键 | §7.1 完整 registry |
| 审批 UI | §8 完整 |
| 进化可视化 | §4.4 |
| Rewind | §6.5 |
| 主题 | 5 档（light/dark/HC-AAA/VSCode host/density-compact） |
| I18n | zh/en 首批 |
| a11y | WCAG AA + HC 主题 AAA |
| 测试 | 单元（pytest）+ e2e（Playwright） |

## 附录 B · 关键 REST / WS 端点清单（需后端补齐的用 ★ 标）

```
# Sessions
GET    /v2/sessions
POST   /v2/sessions
DELETE /v2/sessions/{id}
POST   /v2/sessions/{id}/rewind                     ★
POST   /v2/sessions/{id}/messages/{msg_id}/retry    ★

# Agents
GET    /v2/agents
POST   /v2/agents                                   ★
PATCH  /v2/agents/{id}                              ★
DELETE /v2/agents/{id}                              ★

# Skills
GET    /v2/skills
GET    /v2/skills/{name}
POST   /v2/skills/{name}/promote
POST   /v2/skills/{name}/demote
POST   /v2/skills/{name}/invoke                     ★
GET    /v2/skills/{name}/diff?from=vN&to=vM         ★

# Evolution
GET    /v2/evolution/candidates
POST   /v2/evolution/candidates/{id}/approve
POST   /v2/evolution/candidates/{id}/reject

# Memory
GET    /v2/memory/{layer}
POST   /v2/memory/prune
PATCH  /v2/memory/{id}                              ★
DELETE /v2/memory/{id}                              ★

# Tools & MCP
GET    /v2/tools
GET    /v2/mcp/servers
POST   /v2/mcp/servers                              ★
DELETE /v2/mcp/servers/{id}                         ★

# Security / Approvals
GET    /v2/approvals
POST   /v2/approvals/{id}/approve
POST   /v2/approvals/{id}/reject
POST   /v2/approvals/{id}/approve-with-args         ★

# Backup
GET    /v2/backups
POST   /v2/backups
GET    /v2/backups/{name}
POST   /v2/backups/{name}/restore
DELETE /v2/backups/{name}

# Doctor
GET    /v2/doctor
POST   /v2/doctor/fix?check={id}

# Insights
GET    /v2/insights/cost?days=30                    ★
GET    /v2/insights/usage?days=30                   ★
GET    /v2/insights/learning?days=30                ★

# WebSocket
WS     /agent/v2/{session_id}?token={pairing}
  ← hello / session_lifecycle / user_message / llm_* / tool_* / skill_* / cost_tick /
    grader_verdict / skill_candidate_* / skill_promoted / skill_rolled_back /
    anti_req_violation / prompt_injection_detected / memory_evicted / todo_updated
  → user_message / cancel / retry / approve / reject
```

## 附录 C · 决策记录（Architecture Decision Records）

| ADR | 决策 | 时间 | 理由 |
| --- | --- | --- | --- |
| ADR-001 | 保留"无 Node.js 构建"约束 | 2026-04-25 | CLAUDE.md 明示；降低维护负担 |
| ADR-002 | 推荐 Preact + htm 作为框架 | 2026-04-25 | §11.3；最接近 React 心智、10 KB、无构建 |
| ADR-003 | 双模 CSS 变量（XMclaw + VSCode host） | 2026-04-25 | 抄 Continue / Cline；为未来嵌入铺路 |
| ADR-004 | 自进化可视化作为差异化叙事锚点 | 2026-04-25 | §1.5；所有竞品空白地带 |
| ADR-005 | Plan/Act 双模式 + approval 队列 | 2026-04-25 | 抄 Claude Code / Cline + LM Studio |
| ADR-006 | 流式 Markdown：rAF + token keyed diff | 2026-04-25 | 抄 Open WebUI `Markdown.svelte:62-87` |
| ADR-007 | gRPC 风格类型化 IPC 暂不引入 | 2026-04-25 | §1.3 有记录；待 WS 字段 > 30 时再考虑走 proto |
| ADR-008 | 组件单文件 ≤ 500 行硬约束 | 2026-04-25 | §1.4 反教材 |
| ADR-009 | **Phase 0 敲定 Preact + htm 为最终框架**（升级 ADR-002 的"推荐"为"决策"）；双轨加载：esm.sh CDN 为主 + `static/vendor/` 自托管兜底 + `config.json frontend.assets_mode` 可强制本地 | 2026-04-25 | §11.3.1；不让网络访问性阻塞节奏；Alpine 作 Plan B 留底不落代码 |
| ADR-010 | 前端产物直接在 `xmclaw/daemon/static/`，挂 `/ui/`；不做 "旧 UI 并存" 的二级目录（XMclaw 从未发布过旧 Web UI，没有历史负担） | 2026-04-25 | 一次做对；避免任何 "v1 vs v2" 心智税 |

## 附录 D · 调研工件位置

```
.claude/scratch/competitor-code/     # 5 个浅克隆仓库（Continue / Cline / OpenWebUI / Aider / AutoGPT）
docs/FRONTEND_DESIGN.md              # 本文档
# 原调研报告（4 个 agent 的输出），从本 PR 的对话历史可追溯
```

---

**本文档目标**：在不引入 Node.js 构建、不打破 FastAPI StaticFiles 模型的前提下，把 XMclaw 前端一次做到"local-first agent runtime 里 UI 最完整 + 唯一有自进化可视化"的定位。

**下一步**：把本文档作为 Epic #23 的开工依据；先跑 Phase 0 预研，两周内产出最终技术选型 + 骨架目录。
