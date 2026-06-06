# 多 Agent 群聊 / 编排 调研报告（2026-06-06）

> 目标：① 多 agent，各自**独立人格**；② **群聊聊天室**——多个 agent + 用户在
> **同一对话**里协作发言，本质是**多 agent 编排工作流**。
> 本文先盘点 XMclaw 现有底子，再对照 SOTA 范式做差距分析 + 架构建议。**先调研，不实现。**

---

## 1. XMclaw 现有底子（不用重造的部分）

代码核实（`xmclaw/core/multi_agent/`、`xmclaw/daemon/multi_agent_manager.py`、
`routers/agents.py`、`swarm_orchestrator.py`、`providers/tool/agent_inter.py`）：

| 能力 | 现状 | 文件 |
|---|---|---|
| **多 agent 注册表** | ✅ 每 agent = 一个完整 `Workspace`(独立 AgentLoop + 独立 config)，并发安全、落盘 `~/.xmclaw/v2/agents/<id>.json`、X-Agent-Id 路由 | `daemon/multi_agent_manager.py`、`core/multi_agent/manager.py` |
| **独立人格** | ✅ **已支持**：`create_agent(agent_id, config)` 接收**完整 agent config**，每 agent 可有自己的 model / persona profile dir / 工具集；persona 经 `factory.build_agent_from_config` + `_resolve_persona_profile_dir(cfg)` 解析 | `routers/agents.py`、`factory.py` |
| **agent 间通信** | ✅ `chat_with_agent`(阻塞，等对方 `run_turn` 返回 reply)、`submit_to_agent`(异步+`check_agent_task` 轮询)、`list_agents`、`fork_session`、`stop_agent_task` | `providers/tool/agent_inter.py` |
| **任务编排(swarm)** | ✅ `SwarmOrchestrator`：目标→HTN 分解成 DAG→负载均衡分派给多 agent→聚合(concat/vote/map_reduce) | `swarm_orchestrator.py` |
| **前端多 agent** | ⚠️ 仅 **agent 切换器**：聊天页一个下拉，选"跟 main 还是某 sub agent 说话"；Agents 页能建/删/列 agent。WS 按 (session, agentId) 连接 | `static/pages/Chat.js`、`app.js`、`pages/Agents.js` |

**结论**：多 agent 运行时、独立人格、点对点委派、任务分解编排 **都已具备**。

---

## 2. 真正缺的：群聊「房间」层

现有模型是 **一对话一活跃 agent**（你切换跟谁说），以及 **main 编排 sub 的点对点委派**。
**没有**的是：

- ❌ **共享房间**：N 个 agent + 用户在**同一条 transcript** 里，彼此能看到对方发言、相互回应。
- ❌ **讲者选择 / 轮次编排**：群聊核心——下一个谁说话（轮流 / 主持人决定 / @点名 / 自荐）。
- ❌ **群会话(group session)概念**：一条消息扇出给多个 agent、按序收集回复、合并流式推到一个 UI 房间。
- ❌ **群聊 UI**：多 agent transcript（每 agent 头像/配色/名牌）、@点名、参与者列表、"谁在打字"。
- ❌ **终止条件**：最大轮数 / 无人想发言 / 用户打断 / "完成"信号。

grep `group_chat|chatroom|round_robin.*agent|speaker_select` = **全空**，确认是全新一层。

---

## 3. SOTA 范式（带真实来源）

### 3.1 AutoGen GroupChat + GroupChatManager（最贴近用户需求）
一个共享消息列表 + 一个 **GroupChatManager** 当"指挥"，重复三步：**动态选讲者 → 收集该讲者回复 → 广播给全体**。讲者选择策略可配：
- `auto`（默认，Manager 用 LLM 选下一个讲者）
- `round_robin`（按给定顺序轮流）
- `manual`（人选）/ `random` / **自定义 Callable**

→ 这就是用户要的"群聊"基本骨架。XMclaw 可直接对标这套。

### 3.2 Microsoft Magentic-One（编排工作流的更强形态）
**Orchestrator** 主导规划：外层维护**任务台账(Task Ledger：事实/猜测/计划)**，内层维护**进度台账(Progress Ledger：当前进度/把子任务分给哪个 agent)**，每步自省是否完成、卡住就重规划。底下 4 个专才 agent（WebSurfer / FileSurfer / Coder / ComputerTerminal）。
→ 当群聊要做**有目标的工作流**（不是闲聊）时，这种"主持人 + 双台账"模式比纯轮流强；和 XMclaw 已有的 `SwarmOrchestrator` 思路同源，可融合。

### 3.3 CrewAI（角色化人格 + 流程）
每 agent 有 **role / goal / backstory**（= 人格三件套），Crew 以 **sequential 或 hierarchical** 流程跑；hierarchical 模式有一个 manager agent 派活。
→ 印证"独立人格"该带 **role+goal+backstory** 结构化字段，而不只一段 system prompt。

### 3.4 LangGraph supervisor / handoff
把 agent 建成图节点，**supervisor 节点**根据上下文把控制权"交接(handoff)"给某个 worker agent；显式边定义谁能交给谁。
→ 适合做**确定性工作流**（固定流转），是 `auto` 选讲者之外的另一极。

**三种编排光谱**：自由轮流(round-robin) ←→ 主持人动态选(auto/supervisor) ←→ 固定流程(sequential/graph)。群聊房间最好**三种都支持、可切**。

---

## 4. 给 XMclaw 的架构建议（复用 + 新建）

### 4.1 数据/运行时
- **GroupRoom**：新会话类型 `group:<id>`，持有 **participants**（agent_id 列表 + 用户）+ 共享 transcript + 编排策略。
- **GroupOrchestrator**（新，~AutoGen GroupChatManager）：核心循环
  1. **选讲者**（策略：`round_robin` / `mention`(@点名) / `auto`(主持 LLM 选) / `supervisor`(固定流转)）
  2. **跑该 agent 的 `run_turn`**，上下文 = 共享房间 transcript（他人发言带讲者名牌）
  3. **把回复打上 agent_id 流式推到房间**
  4. 回到 1，直到：到用户回合 / 无人自荐 / 达最大轮数 / 用户打断。
- **复用**：`MultiAgentManager`(取参与者 Workspace)、各 agent 的 `AgentLoop.run_turn`、`agent_inter` 的"等对方 run_turn 返回"原语、事件总线(事件已能带 agent_id)。

### 4.2 人格（结构化，CrewAI 式）
给 agent config 增 **role / goal / backstory / 发言风格** 字段，注入各自 system prompt；每 agent 仍有独立 memory（已支持，按 agent 隔离）。提供几个**预设人格模板**（如：产品/架构/测试/批评者）一键拉进房间。

### 4.3 上下文与记忆（关键，避免之前的污染坑）
- 每 agent 看**房间公共历史**（带讲者标签），但**只写自己的记忆**。
- 公共历史要做**长度/相关性裁剪**（沿用刚修的召回阈值思路，别把噪音灌进每个 agent）。

### 4.4 前端：群聊房间 UI
- 新"房间"页：多 agent transcript，每发言带 **agent 头像/配色/名牌**；
- **@点名** composer（指定下一个谁说）、**参与者侧栏**（在线/思考中）、**"X 正在输入"**；
- 复用已做的消息流渲染 + 微交互（头像呼吸/折叠工具卡等）。

### 4.5 终止与安全
最大轮数 + 预算（token/时间）+ 死循环检测（两个 agent 互相空转）+ 用户随时插话/暂停。

---

## 5. 建议的分期（待你拍板后细化）
- **G1 运行时**：GroupRoom + GroupOrchestrator（先 round_robin + @mention 两策略）+ 群会话事件流（多 agent 打 agent_id 推一个房间）。
- **G2 人格**：role/goal/backstory 结构化 + 预设模板 + Agents 页编辑人格。
- **G3 UI**：群聊房间页（名牌/配色/@点名/参与者栏/谁在说）。
- **G4 编排升级**：`auto`(主持 LLM 选讲者) + 与 `SwarmOrchestrator` 融合做"有目标的工作流房间"(Magentic-One 式双台账)。
- **G5 终止/安全/预算** + 测试（跨前后端真实 HTTP/WS 路径）。

---

## 6. 关键决策点（需要你定，再进 plan）
1. **编排策略默认哪种**：自由轮流 / @点名手动 / 主持人 LLM 自动？（建议先 @点名 + 轮流，auto 放后期）
2. **群聊定位**：偏**闲聊/头脑风暴**（松）还是**有目标的工作流**（紧，要主持人+台账）？或两种房间模板都给。
3. **人格粒度**：结构化 role/goal/backstory，还是先一段自定义 system prompt 起步？
4. **记忆**：群聊里各 agent 的记忆是否互通？（建议默认隔离，可选共享一个"房间记忆"）

## Sources
- AutoGen GroupChat / 选讲者：<https://microsoft.github.io/autogen/0.2/docs/reference/agentchat/groupchat/> ·
  <https://microsoft.github.io/autogen/0.2/docs/topics/groupchat/customized_speaker_selection/> ·
  <https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/group-chat.html>
- AutoGen 论文：<https://arxiv.org/pdf/2308.08155>
- Magentic-One（Orchestrator + 双台账）：<https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/> ·
  论文 <https://arxiv.org/html/2411.04468v1>
- CrewAI GroupChat / 角色：<https://docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/>
