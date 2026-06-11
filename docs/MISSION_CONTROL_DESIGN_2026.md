# Mission Control — Web UI / TUI 整体重设计（2026-06）

> **地位**: Phase 10 的设计规格书（JARVIS_IMPLEMENTATION_PLAN_2026.md §Phase 10 是进度与验收的 source of truth，本文是其设计附件）。
> **决策日期**: 2026-06-11，用户拍板三项方向决策（见 §7 决策记录）。
> **历史教训**: commit ce7a172（上一轮 "Agent UI + TUI rewrite"）被 cd528c4 整体 revert，原因是**做出来效果不好**（视觉/方向问题，非技术 bug）。本轮流程修正写入 §6。

---

## 0. 问题诊断：为什么现在像"聊天网页"

1. **消息流是唯一主角**。`pages/Chat.js` 是经典聊天三件套（会话列表 + 气泡流 + 输入框）；工具调用、计划、思考全部作为"消息附属品"塞进气泡。Agent 产品的正确次序是反的：**任务与执行过程是主角，对话只是控制通道**。
2. **Agent 能力被流放到 20+ 个后台页面**。Evolution / Cognition / Memory / Trace / MultiAgent / Cron 等 XMclaw 真正的差异化能力藏在图标栏后面，像管理后台。主界面感受不到"agent 在自主做事"。
3. **没有"任务"这个概念**。左栏列的是 session（聊天记录），不是 task（带目标、状态、产物的工作单元）。看不到哪个在跑、哪个在等审批、哪个完成了。
4. **WorkspacePanel 只是被动抽屉**。文件/diff 这类工作产物应是常驻一等公民。
5. **TUI 不是 TUI**。`cli/chat.py` 是行式 REPL，靠 `QUIET_MS=3s` 静默期猜回合结束；`xmclaw/tui/`（Textual 骨架）只有 chat 屏 + 2 个 widget，未承载任务/审批/工作区。

## 1. 设计原则

- **P1 任务一等公民**：打开界面第一眼回答"agent 正在干什么"，不是"上次聊了什么"。
- **P2 执行过程即主舞台**：工具调用、计划步骤、审批请求、子 agent 是时间线的平级条目，不是气泡附件。
- **P3 对话是指挥通道**：输入框常驻底部——下指令、补充约束、随时打断；纯闲聊降级为"无任务对话"。
- **P4 产物常驻可见**：Diff / 文件 / 终端输出 / 预览在右侧常驻面板，agent 改了什么实时亮出来。
- **P5 Web 与 TUI 同构**：同一套信息架构、同一个 WS 协议、同一套事件→条目映射，TUI 是终端里的投影。
- **P6 事件流是唯一数据源**：UI 只消费 `BehavioralEvent`（`core/bus/events.py`）+ 既有 REST。不为 UI 发明私有后端通道。

## 2. 信息架构

### 2.1 布局（Web）

```
┌─ HUD：agent 身份 · 模型 · $成本 · 记忆数 · 自主目标 · 连接状态 ─────────┐
│ 任务栏        │  任务视图                          │  工作区          │
│  ▶ 运行中     │   ┌ 目标 + 计划步骤条 [✓✓▶··]      │   Diff │ 文件    │
│  ⏸ 等待审批   │   ├ 活动时间线                     │   终端 │ 预览    │
│  ✓ 已完成     │   │  ⚙ 工具调用卡（可折叠）        │                  │
│  · 无任务对话 │   │  ◆ agent 陈述                  │  (workspace      │
│  ─────────    │   │  ⚠ 审批卡 [允许/总是/拒绝]     │   tree/diff API  │
│  定时 · 后台  │   └ 指挥通道（输入框 + 打断键）    │   复用)          │
└──────────────┴────────────────────────────────────┴──────────────────┘
```

- **左栏 任务栏**：任务卡 = 标题 + 状态徽章（运行中/等审批/已完成/受阻/无任务对话）+ 当前步骤摘要。底部挂定时任务（cron）与后台任务（sleep tasks）计数入口。全局导航（记忆/能力/系统三域）收为左栏底部图标组。
- **中栏 任务视图**：顶部目标 + 计划步骤条（`plan_*` 事件驱动）；中部活动时间线；底部指挥通道。
- **右栏 工作区**：四标签 Diff / 文件 / 终端 / 预览。复用既有 `session_workspaces` 的 tree/commits/diff API 与 canvas artifact 渲染链（Phase 9 M1 桥）。

### 2.2 任务模型（后端聚合视图，不造新调度器）

"任务"不是新的存储实体，是对既有事实的**聚合投影**：

| 来源 | 映射 |
|------|------|
| session + `plan_started/…/plan_completed` | 有计划的 session = 任务，步骤条来自 plan 事件 |
| `todo_updated` | 无显式 plan 时的轻量步骤条 |
| `task_state_changed` / TaskScheduler | 状态徽章 |
| `agent_asked_question` / NEEDS_APPROVAL | "等待审批"态 |
| cron / `sleep_task_started/finished` | 左栏"定时·后台"区 |
| 无以上信号的 session | "无任务对话" |

新增一个只读 router `GET /api/v2/tasks`（聚合快照，启动水化用）；增量更新全部走既有 WS 事件。**后端唯一新代码是这个聚合 router + 必要的事件补字段，不动 AgentLoop。**

### 2.3 事件 → 时间线条目映射

| BehavioralEvent | UI 条目 |
|---|---|
| `llm_chunk` / `llm_response` | agent 陈述（流式 markdown） |
| `llm_thinking_chunk` | 折叠的思考块 |
| `tool_call_emitted` → `tool_invocation_started/finished` | 工具卡：名称+参数摘要 → 状态翻转 ✓/✗ + 耗时；**按工具类型特化渲染**（见 §2.3.1），输出大体折叠 |
| `agent_asked_question` | 内联审批/提问卡（按钮直接回 `answer_question` 帧） |
| `workspace_file_changed` | 时间线小条目 + 右栏 Diff 标签亮点 |
| `plan_*` | 顶部步骤条状态翻转（不进时间线刷屏） |
| `cost_tick` / `context_compressed` | HUD 数字更新（不进时间线） |
| `prompt_injection_detected` / `anti_req_violation` | 红色安全条目 |
| `skill_invoked` / `proactive_proposal` / `inner_monologue` | 默认折叠的"agent 内部活动"组 |

#### 2.3.1 工具卡按类型特化渲染（Claude Code 级细节，M2 核心验收项）

通用"名称 + JSON 参数"卡只是兜底。常用工具必须有专属渲染器：

| 工具类 | 时间线内联渲染 |
|---|---|
| `file_edit` / `file_write` | **内联语法高亮 diff 卡**：头部 `✎ 编辑 service.py +30 −3` + 折叠箭头 + 「预览」切换；体部带行号 gutter、删除行红底/新增行绿底、上下文行原色、语法着色叠加（按扩展名选 lexer）；超过 ~40 行默认折叠中段；「预览」切到编辑后全文视图；点击文件名 → 右栏工作区定位该文件 |
| `bash` / `code_python` | 终端式卡：`$ 命令` 头 + 等宽输出流（流式 append），退出码徽章，长输出折叠尾部展开 |
| `file_read` | 单行摘要卡：`📄 读取 auth.py (1-120 行)`，点击 → 右栏文件标签打开 |
| `browser_*` / computer-use | 截图缩略卡（点击 lightbox），动作描述为标题 |
| `canvas_*` | 直接内联渲染 artifact（复用 Phase 9 渲染链） |
| 其他 | 兜底卡：名称 + 参数摘要 + 折叠 JSON |

实现要点：diff 数据优先取自工具结果里的结构化 diff/patch 字段；没有则前端用 before/after 文本跑 diff 算法（diff-match-patch 或 jsdiff，vendor 进 dist）。语法高亮用与右栏工作区同一套 highlighter（Shiki 或 highlight.js，统一主题 token）。TUI 侧同构：Textual 里 diff 卡用 `rich.syntax` + 红绿行底色降级呈现。

### 2.4 20+ 页面收编（M3）

四个域，旧页不 1:1 搬家，按"驾驶舱仪表"重组：

| 新域 | 收编旧页 |
|------|----------|
| **任务**（主界面） | Chat, Sessions, Cron, Trace, Workspace, Files |
| **记忆** | Memory（含 V2 facts/graph/journal 面板） |
| **能力** | Skills, Marketplace, Evolution, Cognition, Tools, Agents/Rooms, Channels |
| **系统** | Settings, Security, Logs, Analytics, ConfigViewer, Dashboard, Docs |

## 3. 技术栈与工程结构（Web）

| 项 | 决策 | 理由 |
|---|---|---|
| 构建 | **Vite 6 + TypeScript** | 用户 2026-06-11 拍板引入现代构建链（推翻 ADR-001，见 §7） |
| 框架 | **React 19** | 生态（shadcn/ui、xterm.js、虚拟列表）最厚；上轮 Preact 无构建的痛点正是组件生态贫瘠 |
| 样式 | Tailwind CSS 4 + shadcn/ui | 设计 token 统一，暗色模式原生 |
| 状态 | zustand + 移植版 chat reducer | **数据层移植不重写**：`lib/ws.js` 重连补发、历史水化、pending question 恢复等踩坑逻辑按语义移植为 TS |
| 终端 | xterm.js（vendor 进 dist） | 右栏"终端"标签渲染 bash 工具输出流 |

**目录与产物**：

```
webui/                      # 源码（Vite 工程，repo 根）
xmclaw/daemon/webui_dist/   # 构建产物，提交进 git（豁免 gitignore）
```

- daemon 用 `StaticFiles` 挂载 `webui_dist/`：开发期挂 `/ui-next/` 与旧 `/ui/` 并存；M3 验收通过后 `/ui/` 切到新产物，旧 `static/` 移入 `docs/archive/` 留一个 tag 周期后删除。
- **最终用户零 Node**：pip 安装拿到的是已构建产物。Node 20+ 仅前端开发需要。
- CI 新增 `webui-build` job：`npm ci && npm run build`，diff 校验 `webui_dist/` 与源码一致（防"改了源码忘 build"）。
- 开发期 `vite dev` 反代 daemon 的 `/api` 与 `/agent`（WS），保住"改完即见"的开发体验。

## 4. TUI 设计（M4）

基础：**重建 `xmclaw/tui/`（Textual）**——骨架（JarvisTUI + screens/chat + status_bar/tool_log）已在，`xmclaw chat` 默认已走它。textual 收进 `pip install 'xmclaw[tui]'` extra，缺依赖时回落 `--plain`。

```
┌ HUD：模型 · $成本 · 记忆 · ● 在线 ──────────────────────────┐
│ 任务列表（状态徽章）   │ 计划步骤条 [✓✓▶··]                  │
│ 定时 · 后台计数        │ 活动时间线（工具卡/陈述/审批）       │
├───────────────────────┴─────────────────────────────────────┤
│ > 指挥通道                                                   │
└ [Tab]切面板 [d]iff [p]lan [y/a/n]审批 [Esc]打断 [?]帮助 ─────┘
```

- 审批快捷键 y(允许)/a(总是)/n(拒绝) 直接回 `answer_question` 帧。
- Diff/文件以 modal screen 呈现（终端宽度不足以常驻三栏）。
- **废除 `QUIET_MS` 静默猜回合**：回合终止判定复用 web chat reducer 的事件语义（`llm_response` 终帧 / `plan_completed` / 错误帧），移植为共享判定函数。`cli/chat.py` 行式 REPL 保留为 `--plain` 兜底，同样换用事件判定。

## 5. 里程碑（详见 JARVIS plan §Phase 10）

- **M0 设计冻结**：本文 + Phase 10 立项 + CLAUDE.md 约束更新（本 commit）。
- **M1 骨架**：`webui/` 脚手架、三栏布局、WS 接通 + reducer 移植、`/api/v2/tasks` 聚合 router、`/ui-next/` 挂载。
- **M2 执行视图**：时间线全事件映射、计划步骤条、内联审批、右栏四标签工作区。
- **M3 收编与切换**：四域收编 20+ 旧页、`/ui/` 切换、旧 static/ 退役。
- **M4 TUI**：Textual 重建 + 共享回合判定 + 审批快捷键。

每个里程碑结束**先给用户看运行效果、拿到认可再进下一步**（对 ce7a172 revert 的流程级修正）。

## 6. 风险与对策

| 风险 | 对策 |
|------|------|
| 再次"做出来效果不好" | M1 起每里程碑真实运行效果过目；设计稿（本文 §2.1 + docs/ui_mockups/）先行 |
| 双 UI 并存期分裂 | 并存仅限 M1-M3，旧 UI 冻结只修崩溃级 bug |
| dist 与源码漂移 | CI build-diff 校验强制一致 |
| reducer 移植回归 | 按既有 `tests/unit/test_v2_*` 行为对照移植；WS 协议零改动 |
| Node 依赖泄漏给用户 | 产物提交进 git；release 流程不变 |

## 7. 决策记录（ADR-010：取代 ADR-001/002 的"无构建"约束）

- **背景**：ADR-001（无 Node 构建步骤）/ ADR-002（CDN 锁定 esm.sh Preact+htm）服务于"edit+refresh"与离线优先。实践三个月的代价：无 TS、无组件生态、无 tree-shaking，500 行/文件预算靠人肉拆分维持，UI 复杂度已越过无构建模式的舒适区。
- **决策**（2026-06-11，用户拍板）：新 Web UI 采用 Vite + React + TS 构建链。
- **离线优先不弃守**：构建产物自包含进 git/PyPI 包，运行时零 CDN（比旧方案的 esm.sh 兜底更彻底）；"edit+refresh"由 `vite dev` HMR 替代且更强。
- **适用范围**：仅 `webui/` 新工程。旧 `static/` 在退役前仍受原约束（其 AGENTS.md 维持原文，加注 deprecation 指针）。
