# XMclaw → Jarvis 演进路线图

> **文档定位**：从"可进化的本地 Agent"到"个人 AI 操作系统"的架构跃迁规划。  
> **日期**：2026-05-20  
> **基线版本**：v1.0.0（1.0 GA）  
> **文档性质**：允许重构 —— 为达成目标，任何模块都可以被拆分、合并或重写，只要保留事件总线契约和 anti-req 不变量。  
> **配套阅读**：`docs/DEV_ROADMAP.md`（17 Epic 工程拆解）、`docs/ARCHITECTURE.md`（v2 架构权威设计）、`docs/V2_DEVELOPMENT.md`（接口契约与数据流）

---

## 0. TL;DR

| 问题 | 一句话回答 |
|---|---|
| **贾维斯方向是什么？** | 从"你问、我答"的聊天 Agent，进化为"我知你所需、我先行动、我协调万物"的个人 AI 操作系统 |
| **为什么现在做？** | v1.0 核心引擎（Evolution-as-Runtime + HonestGrader + EventBus）已稳定；Commodity 层（渠道/记忆/沙盒）已补齐。地基够了，该盖楼了 |
| **最大一块重构是什么？** | **AgentLoop 从单会话单线程升级为 Orchestrator + Worker Swarm**。当前 `AgentLoop.run_turn()` 是同步顺序执行；贾维斯需要并行、规划、委派 |
| **用户最先感知到什么？** | "我还没说，它就知道我要什么"——意图预测 + 主动编排 |
| **技术债会爆炸吗？** | 不会。所有重构以 **EventBus 契约** 为护城河；内部实现可以换，事件 schema 不动 |
| **多久？** | 三阶段 × 6–8 周 = 18–24 周；但每阶段都可独立交付 |

---

## 1. 愿景：贾维斯不是什么

不是聊天机器人。不是 Copilot。不是又一个 Claude Code 的克隆。

贾维斯是 **你数字生活的操作系统**——它住在你的机器上，像电影里的 Jarvis 一样：

| 电影 Jarvis | XMclaw 今天 | XMclaw 贾维斯态 |
|---|---|---|
| "Sir, your meeting is in 10 minutes and traffic is heavy" | `calendar_reminder` 触发器：提前 5 分钟发提醒 | **意图预测**："根据你过去 3 次迟到模式 + 实时路况，我提前 20 分钟叫你，并已经叫了车" |
| 同时监控反应堆、战甲状态、全球通信 | 单会话 AgentLoop 一次处理一个 turn | **全局状态层**：并行监视代码库健康、系统资源、未读消息、待办逾期——统一视图 |
| "Shall I engage the house party protocol?" | 用户发指令 → agent 执行 | **主动提议**：识别"周五晚 + 好友群聊活跃 + 你上周说想放松"→主动问要不要启动聚会模式 |
| 控制所有硬件（战甲、豪宅、卫星） | 本地文件 + bash + 浏览器 | **全域控制**：本地进程 / Docker / SSH 远程 / 智能家居 / 飞书机器人——统一工具面 |
| 理解 Stark 的一切习惯、偏好、关系 | 自传体记忆 + 向量检索 | **项目中心记忆 + 人员模型**：不仅记住"你喜欢咖啡"，还记住"这个项目用 pytest、小张负责后端、上次重构踩了 circular import 坑" |

**核心跃迁**：从 **Reactive（被动响应）** → **Predictive（预测主动）** → **Orchestrative（编排万物）**。

---

## 2. 现状盘点：已有基础 vs 缺口

### 2.1 可以直接 leverage 的硬资产（不要拆，要扩展）

| 资产 | 当前状态 | 贾维斯态怎么用 |
|---|---|---|
| **EventBus（Streaming Observer）** | `core/bus/`：in-process + SQLite WAL + FTS5 | ✅ 不变。Orchestrator、Worker Agent、State Monitor 全部作为 subscriber 接入 |
| **HonestGrader** | `core/grader/`：0.8 硬信号 + 0.2 LLM | ✅ 不变。Worker Agent 的执行结果同样走 grader；Orchestrator 的调度决策也要有 grader |
| **SkillRegistry + VariantSelector** | `skills/`：UCB1 bandit + 版本化 + 回滚 | ✅ 不变。Worker Agent 调用的 skill 仍然走 registry；Orchestrator 本身也可能被进化 |
| **ProactiveAgent** | `cognition/`：30s tick，6 种触发器 | 🔄 **扩展**。从固定触发器升级为**意图预测引擎**驱动的动态触发 |
| **Channel 系统** | `providers/channel/`：8 个可用 adapter | ✅ 不变。Orchestrator 的 outbound 仍然走 channel adapter |
| **安全体系** | `security/`：Guardian + ApprovalService + SkillScanner + i18n | ✅ 不变。Worker Agent 的 tool 调用同样走 `GuardedToolProvider` |
| **Dashboard** | Web UI `/ui/dashboard`：9 卡聚合 | 🔄 **扩展**。新增 Orchestrator 面板、Worker Swarm 面板、全局状态面板 |

### 2.2 必须新建或重构的模块（缺口 → 蓝图）

| 缺口（来自竞品调研） | 当前代码证据 | 贾维斯解决方案 |
|---|---|---|
| **无多 Agent 编排** | `AgentLoop.run_turn()` 单会话单线程；`builtin_subagent.py` 只有 ephemeral fanout（2–8 个一次性子任务） | **新建 `xmclaw/orchestrator/`**：长期 Worker Agent + Plan-Execute-Verify 流水线 |
| **无代码库语义索引** | 记忆是"对话中心"的，没有 tree-sitter / 代码图 | **新建 `xmclaw/cognition/codebase_index/`**：项目地图 + 符号索引 + 变更感知 |
| **无持久项目记忆** | 跨会话不记得"这个项目用 pytest" | **扩展 `memory/`**：Project Profile 层（代码库约定 + 架构决策 + 踩坑记录） |
| **无隔离执行后端** | 只有 `LocalSkillRuntime` + `ProcessSkillRuntime`；无 Docker/SSH/Modal | **扩展 `providers/runtime/`**：DockerBackend + SSHBackend（已有 ABC） |
| **无 TUI / IDE 集成** | CLI 是 typer 基础命令；无 ACP adapter | **新建 `xmclaw/tui/`（textual）+ `xmclaw/acp/`**：IDE 原生体验 |
| **意图预测缺位** | Proactive trigger 是人工配置的规则（cron / idle timeout） | **新建 `xmclaw/cognition/intent_engine/`**：从事件流学习用户模式，生成主动提议 |

---

## 3. 架构蓝图：允许重构的范围与红线

### 3.1 总体拓扑（目标态）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           JARVIS ORCHESTRATOR                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  IntentEngine │  │  PlanEngine  │  │ WorkerSwarm  │  │  GlobalState    │  │
│  │  (意图预测)   │  │  (任务规划)   │  │ (并行执行)   │  │  (全局状态层)   │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └────────┬────────┘  │
│         │                 │                  │                    │         │
│         └─────────────────┴──────────────────┴────────────────────┘         │
│                                    │                                        │
│                                    ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        EventBus (保留不变)                          │   │
│  │     所有组件只通过 bus 耦合；不允许直接互调                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
  ┌──────────────┐            ┌──────────────┐            ┌──────────────┐
  │  Worker #1   │            │  Worker #2   │            │  Worker #N   │
  │  (代码编辑)   │            │  (信息检索)   │            │  (系统运维)   │
  │  AgentLoop   │            │  AgentLoop   │            │  AgentLoop   │
  │  + SkillReg  │            │  + SkillReg  │            │  + SkillReg  │
  └──────────────┘            └──────────────┘            └──────────────┘
```

### 3.2 重构红线（绝对不能碰的契约）

以下契约如果破坏，等于推倒 v2 地基重建。任何重构必须保留：

1. **EventBus 单向依赖**：`core/` 不 import `providers/` / `daemon/` / `orchestrator/` / `skills/`（`check_import_direction.py` 继续守门）
2. **BehavioralEvent schema 兼容性**：现有事件类型（`USER_MESSAGE`, `TOOL_INVOCATION_FINISHED`, `GRADER_VERDICT`, `SKILL_PROMOTED` 等）的 payload 字段只增不减
3. **HonestGrader 权重不变量**：硬信号 ≥ 0.8，LLM 自评 ≤ 0.2
4. **SkillRegistry 版本化**：append-only history + evidence-gated promotion
5. **Guardian 安全模型**：4-path 决策流（auto_denied / preapproved / needs_approval / fall_through）

### 3.3 允许重构的范围（内部实现可换）

| 模块 | 允许的重构 | 保留的契约 |
|---|---|---|
| `xmclaw/daemon/agent_loop.py` | 可以拆成 `BaseAgentLoop`（纯 turn）+ `OrchestratedAgentLoop`（接受 Plan 输入） | `run_turn()` 签名和事件发射不变 |
| `xmclaw/daemon/app.py` | 可以新增 `/api/v2/orchestrator/*` 路由、WebSocket 子协议 | 现有 `/agent/v2/{sid}` WS 契约不变 |
| `xmclaw/cognition/` | 可以新增 `intent_engine/`, `codebase_index/`, `state_monitor/` 子包 | 现有 `ProactiveAgent` tick 接口不变（被新引擎 wrap） |
| `xmclaw/memory/` | 可以新增 Project Profile 存储层 | `MemoryProvider` ABC 不变；sqlite-vec 默认实现不变 |
| `xmclaw/providers/runtime/` | 可以新增 Docker / SSH / Modal backend | `SkillRuntime` ABC 不变 |
| `xmclaw/cli/` | 可以新增 `xmclaw swarm`、`xmclaw plan`、`xmclaw codebase-index` 子命令 | 现有命令不变 |
| `xmclaw/daemon/static/` | 可以新增 Dashboard 面板、Orchestrator UI | 现有页面路由不变 |

---

## 4. Phase J1：全局感知 + 项目中心记忆（6–8 周）

> **目标**：让 XMclaw"看得见"你的整个数字生活——代码库、系统状态、跨渠道通信——而不只是当前会话的聊天记录。

### 4.1 模块：Global State Monitor（`xmclaw/cognition/state_monitor/`）

**做什么**：一个常驻的观察者进程（作为 ProactiveAgent 的扩展），周期性采集并发布系统状态事件。

**采集维度**：

| 维度 | 来源 | 事件类型 | 频率 |
|---|---|---|---|
| 代码库健康 | `git status` / `git diff --stat` / 测试失败率 | `CODEBASE_HEALTH_TICK` | 5 min |
| 系统资源 | `psutil` CPU/内存/磁盘 | `SYSTEM_HEALTH_TICK` | 30 s（复用现有） |
| 通信聚合 | 各 ChannelAdapter 未读计数 | `INBOX_DIGEST_TICK` | 5 min |
| 待办状态 | 日历 ICS + 任务队列 | `TASK_QUEUE_DIGEST` | 5 min |
| 依赖风险 | `pip list --outdated` / `npm outdated` | `DEPENDENCY_RISK_TICK` | 1 h |

**架构**：
```python
# xmclaw/cognition/state_monitor/engine.py
@dataclass
class StateMonitorEngine:
    bus: EventBus
    collectors: list[StateCollector]
    
    async def tick(self) -> None:
        for collector in self.collectors:
            snapshot = await collector.collect()
            await self.bus.publish(make_event(
                type=EventType.STATE_SNAPSHOT,
                payload={"domain": collector.domain, "snapshot": snapshot}
            ))
```

**与现有系统的关系**：复用 `ProactiveAgent` 的 tick 基础设施，但把"固定触发器"升级为"动态状态采集"。现有 trigger（idle / calendar / stale_project / cron）继续工作，只是多了一个更丰富的输入源。

### 4.2 模块：Codebase Index（`xmclaw/cognition/codebase_index/`）

**做什么**：为每个项目构建持久化的语义索引，让 Agent 跨会话记得"这个项目长什么样"。

**核心能力**：

1. **符号索引**：tree-sitter 解析 → 函数/类/接口定义 → 存入 sqlite-vec
2. **依赖图**：谁 import 谁、谁被谁 test、哪个文件修改最频繁
3. **变更感知**：`git diff` / `git log` 自动增量更新索引
4. **项目约定提取**：从已有代码推断测试框架、lint 规则、命名风格、架构分层

**文件布局**：
```
~/.xmclaw/v2/codebase/
├── <repo-hash>/
│   ├── index.db              # sqlite-vec：符号 + 文档 chunk
│   ├── graph.json            # 模块依赖图（简化版）
│   ├── conventions.md        # 自动提取的项目约定（作为 prompt 注入）
│   └── manifest.json         # repo 路径、最后索引时间、git HEAD
```

**与现有系统的关系**：
- 新增 `ToolProvider`：`codebase_search`（语义搜索）、`codebase_ask`（问答）、`codebase_conventions`（读取 conventions.md）
- 在 `AgentLoop` 的 `prompt_builder` 中，如果检测到当前 working dir 是已索引项目，自动把 `conventions.md` 注入 system prompt（走 `agent_profile` source tag，已被 `security/policy.py` 支持）

**为什么不是用外部工具（如 codebase-indexer）**：
- 外部工具是 MCP server，每次调用走 HTTP；索引是本地的、持久化的、与 XMclaw 事件流联动的（代码变更 → 自动重索引 → 发布 `CODEBASE_INDEX_UPDATED` 事件）

### 4.3 模块：Project-Centric Memory（`xmclaw/memory/project_profile.py`）

**做什么**：在现有三层记忆（short / working / long）之上新增 **Project Layer**。

**存储内容**：

| 类型 | 例子 | 来源 |
|---|---|---|
| 架构决策 | "使用 Repository 模式 + SQLAlchemy 2.0" | AGENTS.md / 代码结构 / LLM 提取 |
| 踩坑记录 | "不要在 async 里用 `sqlite3` 默认连接" | 失败事件 + grader 低分 |
| 人员角色 | "小张负责 auth 模块" | 自传体记忆 + git blame |
| 项目目标 | "Epic #14 要在 6 月底前完成" | 用户陈述 + 日历事件 |
| 代码风格 | "行宽 100，type hints 严格" | ruff/mypy 配置 + 代码统计 |

**接口**：
```python
# xmclaw/memory/project_profile.py
class ProjectProfileStore:
    async def get_profile(self, repo_path: str) -> ProjectProfile | None: ...
    async def upsert_fact(self, repo_path: str, fact: Fact) -> None: ...
    async def query(self, repo_path: str, q: str, k: int = 5) -> list[Fact]: ...
```

**与现有系统的关系**：
- 复用 `SqliteVecMemory` 的存储层（新增 `project` layer）
- `ExtractFactsHook`（已有）在 turn-end 时，除了写 MEMORY.md / USER.md，还检测 working dir 是否属于已索引项目，如果是则额外提取 project fact

### 4.4 Phase J1 重构点

| 重构项 | 范围 | 说明 |
|---|---|---|
| `ProactiveAgent` → 插件化 collector | 中等 | 把现有的 6 个 trigger 拆成 `StateCollector` 插件，新增 collector 无需改 ProactiveAgent 主循环 |
| `prompt_builder.py` → 项目感知 | 小 | 检测 `cwd` → 读 `conventions.md` → 注入 system prompt |
| 新增 `xmclaw/cognition/codebase_index/` | 大 | 新目录，不触碰现有文件 |
| 新增 `xmclaw/memory/project_profile.py` | 中等 | 扩展 `MemoryProvider` 接口，新增 `layer="project"` 支持 |

### 4.5 Phase J1 退出标准

- [ ] `xmclaw codebase index <path>` 能在 30 秒内索引一个 1000 文件的项目
- [ ] 跨会话重启后，`codebase_search` 能回答"这个项目的 auth 模块在哪"
- [ ] `conventions.md` 自动提取准确率 ≥ 80%（人工抽查 20 个项目）
- [ ] State Monitor 发布的 `STATE_SNAPSHOT` 事件能被 Dashboard 消费并显示
- [ ] 全量测试通过（含新增 conformance test）

---

## 5. Phase J2：意图预测 + 多 Agent 编排（8–10 周）

> **目标**：从"你问我答"进化为"我预判你、我规划任务、我委派 Worker 并行执行"。

### 5.1 模块：Intent Engine（`xmclaw/cognition/intent_engine/`）

**做什么**：从用户的历史行为流中学习模式，生成**主动提议（Proactive Proposal）**，而不是等固定触发器。

**输入**：
- 事件流：用户在什么时间、什么项目、什么渠道、说了什么、调了什么 tool
- 状态快照：系统当前状态（未读消息、待办、测试失败、依赖风险）
- 外部信号：日历、邮件主题、天气、股价（通过 Composio / MCP 接入）

**输出**：`PROACTIVE_INTENT_DETECTED` 事件，payload 包含：
```json
{
  "intent_type": "preemptive_reminder",
  "confidence": 0.87,
  "trigger_context": {
    "calendar_event_in": 20,
    "traffic_delay_minutes": 15,
    "user_location": "home",
    "historical_pattern": "user_left_home_25_min_before_meeting_3_of_last_5"
  },
  "proposed_action": {
    "type": "notify",
    "message": "你的会议 20 分钟后开始，当前路况需要 35 分钟，建议现在出发。",
    "channel": "feishu"
  },
  "user_override_history": ["snoozed_5_min", "accepted"]
}
```

**与现有触发器的区别**：

| | 现有 ProactiveAgent | IntentEngine |
|---|---|---|
| 触发逻辑 | 人工规则（cron / idle timeout / 文件mtime） | 从事件流学习的统计模式 + LLM 推理 |
| 上下文范围 | 单一维度（只看日历、或只看 idle） | 多维度融合（日历 + 路况 + 历史行为 + 当前专注状态） |
| 用户反馈 | 无 | 每次主动提议记录用户反应（接受/忽略/反感），闭环学习 |
| 时机 | 固定间隔 | 事件驱动 + 预测模型 |

**实现策略**：
- 第一层：规则启发（快速、可解释）—— 如"会议前 30 分钟 + 历史迟到"→提醒
- 第二层：统计模式（轻量 ML）—— EWMA 检测用户行为漂移；关联规则挖掘（"每次用户说'部署'后 5 分钟内会说'看看日志'"）
- 第三层：LLM 推理（重、慢、精准）—— 把过去 24h 的事件摘要喂给 LLM，问"现在应该主动做什么"

**架构**：
```python
# xmclaw/cognition/intent_engine/engine.py
class IntentEngine:
    async def on_event(self, event: BehavioralEvent) -> None:
        self.context_window.append(event)
        
        # Layer 1: 规则启发（每事件，O(1)）
        if proposal := self.rule_layer.evaluate(event):
            await self._emit_proposal(proposal)
            return
        
        # Layer 2: 统计模式（每 N 事件或定时）
        if self._should_run_statistical():
            if proposal := await self.statistical_layer.evaluate(self.context_window):
                await self._emit_proposal(proposal)
        
        # Layer 3: LLM 推理（低频率，如每 5 分钟最多 1 次）
        if self._should_run_llm():
            if proposal := await self.llm_layer.evaluate(self.context_window):
                await self._emit_proposal(proposal)
```

### 5.2 模块：Plan Engine（`xmclaw/orchestrator/plan_engine/`）

**做什么**：当用户给出一个复杂目标（"把 auth 模块重构到 SQLAlchemy 2.0"），Plan Engine 把它拆解为可并行的子任务，生成 **Execution Plan**。

**Plan 结构**：
```python
@dataclass
class ExecutionPlan:
    plan_id: str
    goal: str
    tasks: list[Task]
    dependencies: dict[str, list[str]]  # task_id -> [prerequisite_task_ids]
    
@dataclass
class Task:
    task_id: str
    description: str
    estimated_effort: Literal["trivial", "small", "medium", "large"]
    required_capabilities: list[str]  # e.g. ["code_edit", "test_run", "git_commit"]
    context_files: list[str]          # 从 codebase index 预检索的相关文件
```

**与 Codex PLANS.md 的区别**：
- Codex 的 plan 是文本文档，供人类和 agent 阅读；XMclaw 的 plan 是**结构化数据**，直接驱动 Worker Swarm 执行
- Plan Engine 生成的 plan 会被 HonestGrader 验证（plan 是否合理、task 是否可执行）

**Plan 验证流程**：
1. LLM 生成 draft plan
2. 用 codebase index 验证 `context_files` 是否存在
3. 用 HonestGrader 验证每个 task 的 `required_capabilities` 是否在可用 tool 集合内
4. 验证通过 → 发布 `PLAN_APPROVED` 事件；不通过 → 发 `PLAN_REJECTED` + reason，LLM 重写

### 5.3 模块：Worker Swarm（`xmclaw/orchestrator/worker_swarm/`）

**做什么**：管理一组长期 Worker Agent，每个有特定专长，能并行执行任务。

**Worker 类型（预设）**：

| Worker | 专长 | 工具集 | 生命周期 |
|---|---|---|---|
| `code_worker` | 代码编辑、重构、测试 | file_read/write, bash, lsp, test_run | 长期 |
| `research_worker` | 信息检索、文档阅读、网络搜索 | web_search, browser, file_read | 长期 |
| `ops_worker` | 系统运维、部署、监控 | bash, docker, ssh, process_watch | 长期 |
| `comm_worker` | 通信摘要、回复草稿、日程协调 | 各 channel adapter | 长期 |

**Worker 架构**：
```python
# xmclaw/orchestrator/worker_swarm/worker.py
@dataclass
class WorkerAgent:
    worker_id: str
    specialty: str
    loop: BaseAgentLoop          # 复用现有 AgentLoop，但独立 session
    tools: ToolProvider          # 子集工具（不是全部）
    memory: MemoryProvider       # 共享项目记忆，但独立会话历史
    
    async def execute(self, task: Task) -> TaskResult:
        # 独立执行，不污染 Orchestrator 的上下文
        result = await self.loop.run_turn(
            session_id=f"worker:{self.worker_id}:{task.task_id}",
            user_message=task.description,
            context_files=task.context_files,
        )
        return TaskResult(task_id=task.task_id, result=result, grader_score=...)
```

**关键设计决策**：

1. **Worker 不是 Thread / Process**：Worker 是**逻辑实体**，多个 Worker 可以共享同一个 Python asyncio event loop。它们通过独立的 `session_id` 隔离上下文。
2. **工具子集**：`code_worker` 不应该能发飞书消息；`comm_worker` 不应该能 `rm -rf`。每个 Worker 的 tool set 在启动时由 Plan Engine 根据 task 的 `required_capabilities` 裁剪。
3. **结果合成**：Worker Swarm 的 `execute_plan()` 收集所有 `TaskResult`，用 LLM（或规则）合成最终答案，发布 `PLAN_EXECUTED` 事件。
4. **与现有 `builtin_subagent.py` 的关系**：`builtin_subagent.py` 的 `parallel_subagents` 是**ephemeral fanout**（2–8 个一次性子任务，无历史）。Worker Swarm 是**长期驻留**（有状态、可召回、可进化）。两者共存：简单并行用 fanout，复杂项目用 Worker Swarm。

### 5.4 模块：Orchestrator（`xmclaw/orchestrator/`）

**做什么**：整个系统的"大脑"——接收用户消息或 IntentEngine 的主动提议，决定是简单回复、单 turn 处理、还是启动 Plan-Worker 流水线。

**状态机**：
```
Inbound Message / Intent
    │
    ▼
┌─────────────┐
│  Classifier │ ── trivial? ──► Direct Reply (现有 AgentLoop)
└──────┬──────┘
       │ complex?
       ▼
┌─────────────┐
│  PlanEngine │ ──► generate plan ──► validate ──► PLAN_APPROVED
└──────┬──────┘                            │
       │ rejected                          │
       ▼                                   ▼
   Retry (≤3)                      ┌─────────────┐
                                   │ WorkerSwarm │ ──► parallel execute
                                   └──────┬──────┘
                                          │
                                          ▼
                                   ┌─────────────┐
                                   │  Synthesis  │ ──► publish result to bus
                                   └─────────────┘
```

**与现有 AgentLoop 的关系**：
- **不删除** `AgentLoop`。
- `Orchestrator` 在 trivial 路径上**代理**给 `AgentLoop.run_turn()`——行为与今天完全一致。
- 在 complex 路径上，`Orchestrator` 创建 Plan + 调度 Worker Swarm；Worker 内部**仍然调用** `AgentLoop.run_turn()`。
- 因此：`AgentLoop` 从"顶层入口"降级为"执行引擎"，但实现不变。

### 5.5 Phase J2 重构点

| 重构项 | 范围 | 说明 |
|---|---|---|
| 提取 `BaseAgentLoop` | 中等 | 把 `AgentLoop` 中与 orchestration 无关的核心逻辑（event emit + hop loop）提取为 `BaseAgentLoop`；原 `AgentLoop` 变薄，作为 trivial 路径的包装 |
| 新增 `xmclaw/orchestrator/` | 大 | 新目录：orchestrator.py / plan_engine/ / worker_swarm/ |
| 新增 `xmclaw/cognition/intent_engine/` | 大 | 新目录 |
| `ProactiveAgent` 被 `IntentEngine` wrap | 小 | `ProactiveAgent.tick()` 改发事件给 `IntentEngine`，或直接被替换 |
| `prompt_builder.py` → Plan-aware | 小 | 如果当前 session 是 Worker session，system prompt 注入 task 描述 + context files |

### 5.6 Phase J2 退出标准

- [ ] 用户输入"重构 auth 模块到 SQLAlchemy 2.0"，Plan Engine 生成 ≥3 个 task 的 plan，Worker Swarm 并行执行，总耗时 < 单线程顺序执行的 60%
- [ ] IntentEngine 在连续使用 1 周后，主动提议的接受率 ≥ 50%
- [ ] Worker Agent 的执行结果同样走 HonestGrader，grader 分数与主 AgentLoop 可比
- [ ] Orchestrator 的 trivial/complex 分类准确率 ≥ 90%（人工标注 100 条）
- [ ] 全量测试通过；新增 plan + swarm conformance test

---

## 6. Phase J3：执行环境扩展 + 人机界面升级（6–8 周）

> **目标**：让 Jarvis 无处不在——IDE 里、终端里、远程服务器上、你的手机里。

### 6.1 执行环境：Docker / SSH / Modal Backend（`providers/runtime/`）

**做什么**：补齐竞品 Hermes/OpenClaw 已有的多后端隔离能力。

| 后端 | 场景 | 状态 |
|---|---|---|
| `LocalSkillRuntime` | 开发/测试，本机执行 | 已有 ✅ |
| `ProcessSkillRuntime` | 进程级隔离 + CPU/时间限制 | 已有 ✅ |
| `DockerSkillRuntime` | 容器隔离，生产默认 | 待实现 🔄 |
| `SSHSkillRuntime` | 远程服务器执行 | 待实现 🔄 |
| `ModalSkillRuntime` | serverless 云执行，按需唤醒 | 待实现 🔄（optional） |

**实现**：继承现有 `SkillRuntime` ABC，每个 backend 实现 `fork()` / `kill()` / `status()`。

**与 Worker Swarm 的关系**：`ops_worker` 可以配置为在 Docker/SSH 后端执行，实现"本地编排、远程执行"。

### 6.2 TUI（`xmclaw/tui/`）

**做什么**：Hermes 级别的终端体验——多行编辑、斜杠命令自动补全、流式工具输出、主题皮肤、会话树。

**技术选型**：`textual`（Python TUI 框架），原因：
- Python-native，与 XMclaw 代码库同语言
- React-like 组件模型，与现有 Web UI 的组件思维一致
- 比 `rich` 更完整的交互控件（DataTable、Tree、Input、TextArea）

**核心界面**：
- 左侧：会话树（主会话 + Worker 子会话）
- 中间：消息流（与 Web UI 一致的事件渲染）
- 右侧：工具活动面板（实时显示 Worker 执行状态）
- 底部：斜杠命令补全 + 快捷操作

**与 CLI 的关系**：`xmclaw chat` 默认启动 TUI；`xmclaw chat --plain` 回退到今天的基础 CLI。

### 6.3 IDE 集成：ACP Adapter（`xmclaw/acp/`）

**做什么**：让 VS Code / Zed / JetBrains 能把 XMclaw 当作后端，实现"编辑器里直接跟 Jarvis 对话"。

**技术路线**：Agent Client Protocol（ACP，QwenPaw / Hermes 都在用）。ACP 是 JSON-RPC over stdio，定义了：
- `initialize`：交换能力
- `tools/list`：暴露 XMclaw 的工具集
- `tools/call`：调用工具
- `prompts/list`：暴露 prompts（如 `/refactor`, `/test`）
- `resources/list`：暴露资源（如当前项目索引）

**实现**：`xmclaw/acp/server.py` 实现 ACP JSON-RPC handler，把 ACP 调用翻译成 XMclaw 内部事件。

### 6.4 语音唤醒（`xmclaw/cognition/voice_wake/`）

**做什么**：OpenClaw 级别的 Voice Wake Words + 连续对话。

**技术路线**：
- 唤醒词：本地轻量模型（如 `openwakeword` 或 `porcupine`），不依赖云
- 语音识别：复用现有 faster-whisper / edge-tts  pipeline
- 连续对话：Web UI 已有；TUI 和桌面端（未来）需要补充

### 6.5 Phase J3 退出标准

- [ ] `DockerSkillRuntime` 成为生产默认；恶意 skill 在容器内 `rm -rf /` 不影响 host
- [ ] `xmclaw chat` 启动 TUI，体验与 Web UI 等价（事件渲染、工具活动、Dashboard 切换）
- [ ] VS Code 安装 ACP 插件后，侧边栏能与 XMclaw daemon 对话，并调用 tool
- [ ] 语音唤醒词"Hey XMclaw"在本地触发响应延迟 < 500ms
- [ ] 全量测试通过

---

## 7. 数据流演进（三阶段对比）

### 今天（v1.0）
```
User ──► ChannelAdapter ──► AgentLoop.run_turn() ──► LLM ↔ Tools ──► Reply
                              │
                              ▼
                           EventBus ──► Grader / Memory / Cost / UI
```

### Phase J1 后
```
StateMonitor ──► EventBus ◄──► GlobalStateView
                                    │
User ──► ChannelAdapter ──► AgentLoop.run_turn() ──► Reply
                              │
                              ├──── CodebaseIndex（项目感知 prompt 注入）
                              └──── ProjectProfile（跨会话项目记忆）
```

### Phase J2 后（目标态）
```
StateMonitor ──► EventBus ◄──► IntentEngine（主动提议）
                                    │
User ──► ChannelAdapter ──► Orchestrator ── trivial? ──► BaseAgentLoop ──► Reply
                              │
                              └─ complex? ──► PlanEngine ──► WorkerSwarm
                                                       │          │
                                                       │          ├─ Worker #1 (code)
                                                       │          ├─ Worker #2 (research)
                                                       │          └─ Worker #N (ops)
                                                       │
                                                       └─ Synthesis ──► Reply
```

---

## 8. 重构纪律与兼容性

### 8.1 向后兼容承诺

| 层面 | 承诺 |
|---|---|
| **HTTP API** | `/api/v2/*` 所有路由保留；新增 `/api/v2/orchestrator/*`、`/api/v2/plan/*` |
| **WebSocket** | `/agent/v2/{sid}` 协议不变；新增 Orchestrator 控制帧（可选协商） |
| **CLI** | 所有现有命令保留；新增 `xmclaw swarm`、`xmclaw plan`、`xmclaw codebase-index` |
| **Config** | `daemon/config.json` 结构向后兼容；新增 `orchestrator.*`、`intent_engine.*`、`codebase_index.*` 段 |
| **事件 Schema** | 现有 `EventType` 不变；新增 `STATE_SNAPSHOT`、`PROACTIVE_INTENT_DETECTED`、`PLAN_APPROVED`、`TASK_EXECUTED` 等 |
| **数据文件** | `~/.xmclaw/v2/events.db`、`memory.db` 格式不变；新增 `codebase/`、`orchestrator/` 子目录 |

### 8.2 重构优先级（先做哪个）

**绝对不能先动的**：
- ❌ 事件总线 schema 破坏性变更
- ❌ HonestGrader 评分公式
- ❌ SkillRegistry 版本化机制
- ❌ Guardian 4-path 安全模型
- ❌ import-direction DAG 约束

**建议先动的**（低风险、高杠杆）：
1. ✅ 新增 `xmclaw/cognition/codebase_index/`（纯新增，不碰现有代码）
2. ✅ 新增 `xmclaw/memory/project_profile.py`（扩展 MemoryProvider，不删改现有 layer）
3. ✅ 提取 `BaseAgentLoop`（代码移动，行为不变）
4. ✅ 新增 `xmclaw/orchestrator/`（新目录，原 AgentLoop 作为 trivial 路径继续工作）
5. ✅ `ProactiveAgent` 插件化 collector（重构，但接口不变）

### 8.3 测试策略

每个重构必须伴随：

1. **Conformance test**：新增模块必须通过 conformance suite
2. **回退测试**：trivial 路径（不走 Orchestrator）的端到端测试必须继续通过
3. **Benchmark**：复杂任务（如"重构一个模块"）在单线程 vs Worker Swarm 下的耗时对比
4. **安全测试**：Worker Agent 的 tool 调用仍然走 `GuardedToolProvider`

---

## 9. 里程碑总表

| 里程碑 | 内容 | 预计周数 | 退出标准 | 关联 Epic |
|---|---|---|---|---|
| **J1-M1** | Codebase Index 可用 | 3 周 | 1000 文件项目 30s 内索引完毕 | 新增 |
| **J1-M2** | Project Profile 集成 | 2 周 | 跨会话记住"这个项目用 pytest" | 新增 |
| **J1-M3** | State Monitor 上线 | 2 周 | Dashboard 显示 5 维状态快照 | 扩展 Epic #5 |
| **J2-M1** | Intent Engine 主动提议 | 3 周 | 1 周后接受率 ≥ 50% | 新增 |
| **J2-M2** | Plan Engine 任务规划 | 3 周 | 复杂目标拆解准确率 ≥ 90% | 新增 |
| **J2-M3** | Worker Swarm 并行执行 | 3 周 | 并行耗时 < 单线程 60% | 新增 |
| **J3-M1** | Docker Runtime 生产默认 | 2 周 | 恶意 skill 容器内 rm -rf 不影响 host | Epic #3 扩展 |
| **J3-M2** | TUI 发布 | 3 周 | `xmclaw chat` 默认启动 TUI | 新增 |
| **J3-M3** | ACP IDE 插件 | 2 周 | VS Code 侧边栏可对话 | 新增 |
| **J3-M4** | 语音唤醒 | 2 周 | "Hey XMclaw" 延迟 < 500ms | 新增 |

---

## 10. 与现有 DEV_ROADMAP 17 Epic 的关系

本文不替代 `DEV_ROADMAP.md`。关系如下：

| DEV_ROADMAP Epic | 本文影响 | 说明 |
|---|---|---|
| Epic #1 Channel SDK | 无影响 | 继续使用，Worker Swarm 的 `comm_worker` 复用 |
| Epic #2 Plugin SDK | 无影响 | Worker 工具子集裁剪走 Plugin SDK 的 capability 声明 |
| Epic #3 Sandbox | 扩展 | Docker/SSH/Modal backend 作为 J3-M1 |
| Epic #4 Evolution | 扩展 | Worker Agent 的执行结果同样触发 grader + evolution |
| Epic #5 Memory | 扩展 | Project Profile 作为新增 layer |
| Epic #6–#17 | 无影响 | 按原 roadmap 继续 |

**新增 Epic 编号建议**（在 DEV_ROADMAP 基础上追加）：

- **Epic #18** · Codebase Index & Project Profile（J1）
- **Epic #19** · Intent Engine（J2）
- **Epic #20** · Plan Engine & Worker Swarm（J2）
- **Epic #21** · TUI & ACP Adapter（J3）
- **Epic #22** · Voice Wake & Companion Surface（J3）

---

## 11. 结语

XMclaw v1.0 的引擎（Evolution-as-Runtime + HonestGrader）是**学院派正确**的——它解决了"Agent 如何持续自我改进"的问题。

但要成为贾维斯，还需要**工程派正确**——让 Agent 看得见全局、想得了复杂、做得了并行、触得到万物。

这份路线图的核心主张是：**不要为重构而重构**。每一个新增模块都必须：
1. 接入 EventBus（保持进化架构）
2. 接受 HonestGrader（保持诚实评分）
3. 通过 Guardian（保持安全）
4. 产生用户可感知的体验升级（保持产品意义）

**底线**：如果某个重构不能让用户说出"哇，它真的在帮我干活"，那就不要做。
