# XMclaw 贾维斯化 — 详细实施计划

> **版本**: v1.0
> **日期**: 2026-05-09
> **状态**: 定稿，按本文执行
> **依据**: `xmclaw-architecture-redesign.md` + `jarvis-design-doc.md` + `jarvis-dev-spec.md`
> **基准代码**: XMclaw v1.0.0 (commit 范围: Phase 0-9 已交付 + 未合并的贾维斯化设计)

---

## 0. 执行摘要

将 XMclaw 从"事件驱动的 Agent Loop"进化为"自主认知系统"。核心是在**不破坏现有进化层、不改动已有接口契约**的前提下，新增 4 个 P0/P1 模块：

| 模块 | 优先级 | 状态 | 与现有代码关系 |
|------|--------|------|----------------|
| memory_graph | P0 | 新建 | 在 `sqlite_vec` 旁加 `graph.db` |
| task_scheduler | P0 | 新建 | 扩展现有 `CronTickTask` 的 Task 模型 |
| file_watcher | P1 | 新建 | 在 daemon lifespan 中加 watchdog 协程 |
| evolution_loop | P1 | 新建 | 复用 `skill_autoresearch` + `EvolutionController` 框架 |

**铁律**（文档间冲突时的裁决规则）：
1. `BehavioralEvent` 语义不动 — 已有事件类型表是产品契约
2. `HonestGrader` / `EvolutionController` / `SkillRegistry` 不动 — 核心差异化
3. 所有新增模块必须对接现有事件总线 (`InProcessEventBus`)
4. 新增数据库表必须兼容现有 `memory_items` schema（共用 connection 或显式分离）
5. Windows-first：所有文件监控、进程监控必须考虑 Windows 兼容性

---

## 1. 当前架构快照（实施基准）

### 1.1 记忆系统现状

```
xmclaw/providers/memory/
├── base.py              # MemoryProvider ABC, MemoryItem, Layer
├── manager.py           # MemoryManager (builtin + 1 external)
├── sqlite_vec.py        # SqliteVecMemory (short/working/long + vec0)
├── builtin_file.py      # MEMORY.md / USER.md 文本包装
├── embedding.py         # build_embedding_provider (Doubao/本地)
├── file_index.py        # MemoryFileIndexer (WAL 增量索引)
├── relevant_picker.py   # 语义召回前置筛选
└── ...
```

**已有能力**：
- 三层存储（short 1h / working 1d / long ∞）
- sqlite-vec KNN + LIKE fallback + hybrid RRF
- upsert_fact（evidence_count + auto promote working→long）
- MemoryManager 多 provider 编排

**缺失能力**：
- 无节点/边关系模型
- 无因果链遍历
- 无主动回忆（proactive_recall）
- 无跨会话人格一致性（依赖现有 persona 文件，但无程序化的"醒来自检"）

### 1.2 任务调度现状

```
xmclaw/core/scheduler/
├── cron.py              # CronStore + CronTickTask (60s polling)
├── online.py            # OnlineScheduler (UCB1 over skill variants)
└── policy.py            # 调度策略
```

**已有能力**：
- 时间驱动定时任务（croniter + interval）
- per-job enabled_toolsets + wake_agent gate
- JSON 持久化 + 输出到 `~/.xmclaw/cron/output/`

**缺失能力**：
- 无任务依赖 DAG
- 无状态机（PENDING→RUNNING→COMPLETED / FAILED→RETRYING→ESCALATED）
- 无优先级抢占
- 无自愈重试（指数退避）

### 1.3 进化系统现状

```
xmclaw/core/evolution/
├── controller.py        # Promotion gate (plays/mean/gap)
├── mutator.py           # DSPy/GEPA mutation wrapper
├── dataset.py           # Synthetic dataset builder
├── constraints.py       # Size/growth/structure validators
├── proposer.py          # Skill candidate proposer
└── staging.py           # Staging gate

xmclaw/skills/
├── registry.py          # Versioned skill registry
├── orchestrator.py      # EvolutionOrchestrator (auto_apply)
└── ...
```

**已有能力**：
- 完整的 mutate → grader → controller → promote 链路
- HonestGrader（0.80 硬检查 + 0.20 LLM）
- SkillRegistry 版本化 + rollback
- BehavioralEvent 驱动

**缺失能力**：
- 无自主实验循环（观察→假设→实验→验证）
- 无模式检测（detect_recurring_pattern）
- 无实验结果记录表

### 1.4 感知系统现状

**完全缺失**。Daemon 的 lifespan 中没有文件监控、进程监控、网络监控协程。

### 1.5 事件总线现状

```
xmclaw/core/bus/
├── events.py            # BehavioralEvent + EventType enum (50+ 类型)
├── sqlite.py            # SqliteEventBus (持久化)
└── memory.py            # InProcessEventBus (内存)
```

已有丰富事件流：USER_MESSAGE, LLM_RESPONSE, TOOL_INVOCATION_*, GRADER_VERDICT, SKILL_*, MEMORY_*, CRON_JOB_FIRED, CONFIG_RELOADED, SLEEP_*, CONTEXT_COMPRESSED 等。

**新增事件类型**（本计划需要）：
- `PERCEPTION_EVENT` — 感知层检测到环境变化
- `ATTENTION_SHIFT` — 注意力机制重新分配焦点
- `TASK_STATUS_CHANGED` — 任务状态机转换
- `TASK_DEPENDENCY_MET` — 依赖任务完成
- `EXPERIMENT_RESULT` — 自主实验完成

---

## 2. 模块详细设计

### 2.1 memory_graph（P0）

#### 2.1.1 架构决策

**决策 1：Schema 选择**
- 文档 A（架构重构）推荐：SQLite + Neo4j/NetworkX
- 文档 B（贾维斯设计）推荐：在现有 sqlite-vec 旁加 `memory_graph` 表
- 文档 C（开发规范）推荐：独立 `graph.db`

**裁决**：采用文档 C 的独立 `graph.db`（`~/.xmclaw/v2/graph.db`），但使用 SQLite 而非 Neo4j（降低部署复杂度）。NetworkX 作为内存缓存用于图遍历算法。

**理由**：
- Neo4j 需要额外服务，与"local-first"定位冲突
- NetworkX 纯 Python，适合本地图算法（最短路径、社区检测）
- SQLite 作持久化，NetworkX 在 `MemoryGraph.__init__` 时加载热点子图到内存

**决策 2：与现有记忆系统的关系**
- `MemoryGraph` 不是 `MemoryProvider` 的子类（关系查询和向量查询是不同范式）
- `MemoryManager` 新增 `graph` 可选属性，在 `query()` 后自动扩展图谱邻居（文档 B Phase 2）
- `SqliteVecMemory` 的 `memory_items` 表保持不动，`graph.db` 独立

#### 2.1.2 数据模型

```python
# xmclaw/cognition/memory_graph/models.py

from dataclasses import dataclass
from typing import Literal

NodeType = Literal["event", "entity", "state", "intent"]
EdgeType = Literal["CAUSED_BY", "RELATED_TO", "LEADS_TO", "CONTRADICTS", "PART_OF"]

@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    type: NodeType
    content: str
    embedding: tuple[float, ...] | None
    created_at: float
    # 与现有 memory_items 的关联
    memory_item_id: str | None = None

@dataclass(frozen=True, slots=True)
class GraphEdge:
    id: str
    source_id: str
    target_id: str
    relation: EdgeType
    strength: float  # 0-1 置信度
    created_at: float
```

#### 2.1.3 数据库 Schema

```sql
-- graph.db (独立 SQLite 文件)

CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('event', 'entity', 'state', 'intent')),
    content TEXT NOT NULL,
    embedding BLOB,              -- 可选，用于语义相似节点聚类
    created_at REAL NOT NULL,
    memory_item_id TEXT          -- 关联到 memory.db 的 memory_items.id
);

CREATE INDEX IF NOT EXISTS graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS graph_nodes_created ON graph_nodes(created_at);

CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN ('CAUSED_BY', 'RELATED_TO', 'LEADS_TO', 'CONTRADICTS', 'PART_OF')),
    strength REAL NOT NULL DEFAULT 1.0 CHECK(strength >= 0 AND strength <= 1),
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS graph_edges_source ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS graph_edges_target ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS graph_edges_relation ON graph_edges(relation);
-- 防止重复边（同 source+target+relation）
CREATE UNIQUE INDEX IF NOT EXISTS graph_edges_unique 
    ON graph_edges(source_id, target_id, relation);
```

#### 2.1.4 接口定义

```python
# xmclaw/cognition/memory_graph/graph.py

class MemoryGraph:
    """记忆图谱 — 关系型记忆的存储与查询。"""

    def __init__(
        self,
        db_path: Path | str = "~/.xmclaw/v2/graph.db",
        *,
        embedding_dim: int | None = None,
        bus: Any | None = None,
    ) -> None: ...

    # ── 写操作 ──
    async def add_node(self, node: GraphNode) -> str: ...
    async def add_edge(self, edge: GraphEdge) -> str: ...
    async def merge_node(self, content: str, type: NodeType, *, embedding: list[float] | None = None) -> tuple[str, bool]:
        """Upsert：如果内容语义相似则合并，否则新建。返回 (id, was_merged)。"""
        ...
    async def remove_node(self, node_id: str) -> None: ...
    async def remove_edge(self, edge_id: str) -> None: ...

    # ── 读操作 ──
    async def get_node(self, node_id: str) -> GraphNode | None: ...
    async def get_neighbors(
        self,
        node_id: str,
        *,
        relation: EdgeType | None = None,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[tuple[GraphEdge, GraphNode]]: ...
    
    async def find_path(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 5,
    ) -> list[GraphEdge] | None: ...

    # ── 语义查询 ──
    async def find_similar_nodes(
        self,
        query_embedding: list[float],
        *,
        k: int = 5,
        type_filter: NodeType | None = None,
    ) -> list[tuple[GraphNode, float]]: ...  # (node, distance)

    # ── 主动回忆 ──
    async def proactive_recall(
        self,
        context: str,
        *,
        intent_embedding: list[float] | None = None,
        limit: int = 5,
    ) -> str:
        """基于当前上下文，主动推送相关历史记忆。"""
        ...

    # ── 维护 ──
    async def prune_orphaned(self) -> int: ...
    async def stats(self) -> dict[str, Any]: ...
    def close(self) -> None: ...
```

#### 2.1.5 与现有代码对接

| 对接点 | 动作 | 文件 |
|--------|------|------|
| `MemoryManager.__init__` | 新增可选 `graph: MemoryGraph \| None = None` 参数 | `providers/memory/manager.py` |
| `MemoryManager.query()` | 返回后，如 graph 存在，自动扩展查询邻居 | `providers/memory/manager.py` |
| `AgentLoop.run_turn()` | turn 开始前调用 `graph.proactive_recall()`，结果注入 `<memory-context>` | `daemon/agent_loop.py` |
| `AgentLoop.run_turn()` | turn 结束后，提取因果链写入图谱（Phase 2） | `daemon/agent_loop.py` |
| `SessionStore` | session 结束时，将对话摘要转为 event 节点写入图谱 | `daemon/session_store.py` |

#### 2.1.6 实施步骤

**Step 1.1**: 新建模块结构
```
xmclaw/cognition/
├── __init__.py
├── memory_graph/
│   ├── __init__.py
│   ├── models.py      # GraphNode, GraphEdge dataclasses
│   ├── graph.py       # MemoryGraph 主类
│   └── _queries.py    # SQL 查询模板（内部）
```

**Step 1.2**: 实现 `models.py` + `graph.py` 核心 CRUD
- `add_node`, `add_edge`, `get_neighbors`
- WAL mode + busy_timeout（复用 `sqlite_vec.py` 的 _open_conn 模式）
- asyncio.Lock 写保护

**Step 1.3**: 实现 `merge_node`（语义相似合并）
- 如果 embedding 提供，用 L2 distance 找最近邻
- distance < 0.4 → 合并（更新 content，strength 取 max）
- 否则 → 新建节点

**Step 1.4**: 实现 `find_path`（NetworkX 图遍历）
- `networkx` 为可选依赖：`pip install 'xmclaw[cognition]'`
- 无 networkx 时回退到 SQLite recursive CTE

**Step 1.5**: 实现 `proactive_recall`
- 1. 提取当前 intent（复用现有 `llm_extractors.py` 的意图提取）
- 2. 在图谱中找相似 intent 节点
- 3. 遍历 `LEADS_TO` 边找历史 event
- 4. 格式化为提示文本

**Step 1.6**: 对接 `MemoryManager`
- 在 `query()` 返回后，如 graph 存在，对 top-3 结果调用 `get_neighbors(depth=1)`
- 将邻居信息附加到 `MemoryItem.metadata["graph_neighbors"]`

**Step 1.7**: 对接 `AgentLoop`
- turn 开始时：如 graph 存在，`await graph.proactive_recall(context)`
- 结果注入到 user_message 的 `<memory-context>` 块中

**Step 1.8**: 测试
- `tests/unit/test_memory_graph_crud.py`
- `tests/unit/test_memory_graph_proactive_recall.py`
- `tests/integration/test_memory_graph_with_manager.py`

---

### 2.2 task_scheduler（P0）

#### 2.2.1 架构决策

**决策 1：与现有 cron 的关系**
- 保留 `CronStore` + `CronTickTask`（时间触发语义清晰）
- 新建 `TaskScheduler` 接管所有**非定时**后台任务（用户提交、agent 提交、依赖驱动）
- `CronTickTask` 的 `runner` 改为向 `TaskScheduler.submit()` 提交任务，而非直接调用 `AgentLoop`

**决策 2：持久化选择**
- 文档 C 推荐独立 `tasks.db`
- **裁决**：复用现有 `events.db`（`SqliteEventBus` 的 DB），新增 `tasks` 表
- 理由：任务状态变化本身就应该产生事件，共用 DB 保证事务一致性

**决策 3：抢占实现**
- Phase 1 不实现真正的抢占（协程无法强制中断）
- Phase 1 用"软抢占"：高优先级任务到达时，低优先级任务的后续 hop 不继续执行，等当前 hop 完成后挂起
- Phase 3 再考虑真正的多 worker 抢占

#### 2.2.2 数据模型

```python
# xmclaw/cognition/task_scheduler/models.py

from dataclasses import dataclass, field
from typing import Literal

TaskStatus = Literal["pending", "blocked", "running", "completed", "failed", "retrying", "escalated"]

@dataclass(frozen=True, slots=True)
class Task:
    id: str
    prompt: str
    priority: int = 5  # 1-10, 10 最高
    dependencies: list[str] = field(default_factory=list)
    status: TaskStatus = "pending"
    retries: int = 0
    max_retries: int = 3
    timeout_seconds: int = 300
    agent_id: str = "main"
    # 执行记录
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    result: str | None = None
    # 运行时
    _task: Any | None = None  # asyncio.Task 引用（非持久化）
```

#### 2.2.3 数据库 Schema

```sql
-- 复用 events.db，新增 tasks 表

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5 CHECK(priority >= 1 AND priority <= 10),
    dependencies TEXT NOT NULL DEFAULT '[]',  -- JSON list of task IDs
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'blocked', 'running', 'completed', 'failed', 'retrying', 'escalated')),
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    agent_id TEXT NOT NULL DEFAULT 'main',
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    error TEXT,
    result TEXT
);

CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_priority ON tasks(priority DESC);
CREATE INDEX IF NOT EXISTS tasks_agent ON tasks(agent_id);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
```

#### 2.2.4 接口定义

```python
# xmclaw/cognition/task_scheduler/scheduler.py

class TaskScheduler:
    """任务 DAG 调度器 — 依赖拓扑排序、自愈重试、优先级抢占。"""

    def __init__(
        self,
        *,
        bus: Any | None = None,
        db_path: Path | str | None = None,  # None = 复用 events.db
        max_concurrent: int = 3,
        agent_resolver: Callable[[str], Awaitable[Any]] | None = None,
            # ^ 返回 AgentLoop 实例，用于执行任务
    ) -> None: ...

    # ── 提交 ──
    async def submit(self, task: Task) -> str:
        """提交任务。检查依赖，如未满足则置为 BLOCKED。"""
        ...

    async def submit_cron_job(self, job: CronJob) -> str:
        """CronTickTask 调用：将 cron job 包装为 Task 提交。"""
        ...

    # ── 控制 ──
    async def cancel(self, task_id: str) -> bool: ...
    async def pause(self, task_id: str) -> bool: ...
    async def resume(self, task_id: str) -> bool: ...

    # ── 查询 ──
    async def get_status(self, task_id: str) -> TaskStatus | None: ...
    async def get_task(self, task_id: str) -> Task | None: ...
    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[Task]: ...
    
    # 进度可视化
    async def get_progress(self, task_id: str) -> dict[str, Any]:
        """返回 {status, elapsed_seconds, retries, dependency_status}。"""
        ...

    # ── 生命周期 ──
    async def start(self) -> None:
        """启动调度循环（在 daemon lifespan 中调用）。"""
        ...
    async def stop(self) -> None:
        """优雅停止：等 running 任务完成，cancel pending。"""
        ...

    # ── 内部 ──
    async def _schedule_loop(self) -> None:
        """主循环：从 pending 队列按优先级取任务，检查依赖，执行。"""
        ...
    async def _execute(self, task: Task) -> None:
        """实际执行：调用 agent_resolver 获取 AgentLoop，运行 turn。"""
        ...
    async def _on_dependency_met(self, task_id: str) -> None:
        """依赖任务完成时，将 BLOCKED 任务改为 PENDING。"""
        ...
    async def _on_execution_failed(self, task: Task, error: str) -> None:
        """执行失败：retries < max → RETRYING；否则 → ESCALATED。"""
        ...
```

#### 2.2.5 状态机

```
                    submit()
                       │
                       ▼
    ┌─────────────────────────────────┐
    │ 检查依赖                         │
    │ 全部 COMPLETED? ──Yes──► PENDING │
    │ No ────────────────────► BLOCKED │
    └─────────────────────────────────┘
         │                       │
         ▼                       │
    ┌─────────┐            ┌────┴─────┐
    │ PENDING │◄───────────│ BLOCKED  │
    └────┬────┘ 依赖满足    └──────────┘
         │ _schedule_loop()
         ▼
    ┌─────────┐
    │ RUNNING │
    └────┬────┘
         │
    ┌────┴────┐
    │完成?    │
    │ Yes ────┼──► COMPLETED
    │ No      │
    │ retries │
    │ < max ──┼──► RETRYING ──► [指数退避] ──► PENDING
    │ >= max  │
    └─────────┴──► ESCALATED
         ▲
         │ cancel()
    ┌────┴────┐
    │ CANCEL  │ (概念状态，实际从 DB 删除)
    └─────────┘
```

#### 2.2.6 与现有代码对接

| 对接点 | 动作 | 文件 |
|--------|------|------|
| `CronTickTask._loop()` | runner 改为 `task_scheduler.submit_cron_job(job)` | `core/scheduler/cron.py` |
| `daemon/app.py lifespan` | 构造 `TaskScheduler`，传入 `agent_resolver` | `daemon/app.py` |
| `AgentLoop.run_turn()` | 新增 `submit_to_agent()` 工具内部改为 `task_scheduler.submit()` | `daemon/agent_loop.py` |
| `MultiAgentManager` | 为 `agent_resolver` 提供 agent_id → AgentLoop 映射 | `core/multi_agent/manager.py` |
| 事件总线 | 发布 TASK_STATUS_CHANGED, TASK_DEPENDENCY_MET | `core/bus/events.py` |

#### 2.2.7 实施步骤

**Step 2.1**: 新建模块结构
```
xmclaw/cognition/
└── task_scheduler/
    ├── __init__.py
    ├── models.py
    ├── scheduler.py
    └── _store.py      # SQLite CRUD 封装（内部）
```

**Step 2.2**: 实现 `_store.py`
- 复用 `SqliteEventBus` 的 connection 或独立连接
- 实现 Task 的 CRUD + 按状态/优先级查询

**Step 2.3**: 实现 `scheduler.py` 核心逻辑
- `submit()`：检查依赖，决定 PENDING / BLOCKED
- `_schedule_loop()`：优先级队列（heapq），每 1s 扫描
- `_execute()`：调用 agent_resolver，超时控制
- `_on_execution_failed()`：指数退避重试

**Step 2.4**: 对接 `CronTickTask`
- 修改 `CronTickTask` 的 runner 签名，支持向 scheduler 提交
- Cron job 输出仍写入 `~/.xmclaw/cron/output/`

**Step 2.5**: 对接 `daemon/app.py`
- lifespan 中构造 `TaskScheduler`
- `agent_resolver` 从 `app.state.agents` 解析

**Step 2.6**: 测试
- `tests/unit/test_task_scheduler_states.py`
- `tests/unit/test_task_scheduler_dependencies.py`
- `tests/integration/test_task_scheduler_with_cron.py`

---

### 2.3 file_watcher（P1）

#### 2.3.1 架构决策

**决策 1：库选择**
- 文档 B 推荐 `watchdog`
- **裁决**：使用 `watchdog`，但包装为可选依赖（`pip install 'xmclaw[perception]'`）
- Windows 上 `watchdog` 使用 `ReadDirectoryChangesW`，稳定可靠
- 备选方案：轮询（`os.stat` 比较 mtime），在 watchdog 不可用时降级

**决策 2：不打扰原则**
- 只记录，不主动说（文档 B Phase 1）
- 上下文相关性判断（Phase 2）：如果用户最近在处理相关文件，才主动提示
- 相关性算法：向量相似度（文件路径 embedding vs 最近 memory search 的 query）

#### 2.3.2 数据模型

```python
# xmclaw/cognition/perception/models.py

from dataclasses import dataclass
from typing import Literal

FileEventType = Literal["created", "modified", "deleted", "moved"]

@dataclass(frozen=True, slots=True)
class FileEvent:
    path: str
    event_type: FileEventType
    timestamp: float
    # 可选
    is_directory: bool = False
    src_path: str | None = None  # for "moved"
```
```

#### 2.3.3 接口定义

```python
# xmclaw/cognition/perception/file_watcher.py

class FileWatcher:
    """文件系统感知 — 监控工作目录变化。"""

    def __init__(
        self,
        *,
        watch_paths: list[str],
        ignore_patterns: list[str] | None = None,
        bus: Any | None = None,
        memory_graph: Any | None = None,
    ) -> None: ...

    async def start(self) -> None:
        """启动 watchdog observer（在 daemon lifespan 中调用）。"""
        ...
    async def stop(self) -> None:
        """停止 observer。"""
        ...

    # ── 事件处理 ──
    async def on_change(self, event: FileEvent) -> None:
        """1. 记录到感知缓冲区
        2. 如 memory_graph 存在，创建/更新文件对应的 entity 节点
        3. 如 contextually_relevant，发布 PERCEPTION_EVENT"""
        ...

    async def is_contextually_relevant(self, path: str) -> bool:
        """判断文件变化是否与用户当前上下文相关。
        默认实现：检查最近 5 分钟 memory_search 的 query 与路径的 token 重叠。
        Phase 2：用 embedding 相似度。"""
        ...

    # ── 配置 ──
    def add_watch(self, path: str) -> None: ...
    def remove_watch(self, path: str) -> None: ...
```

#### 2.3.4 与现有代码对接

| 对接点 | 动作 | 文件 |
|--------|------|------|
| `daemon/app.py lifespan` | 构造 `FileWatcher`，watch_paths 从 `config.perception.watch_paths` 读取 | `daemon/app.py` |
| `events.py` | 新增 `PERCEPTION_EVENT` 事件类型 | `core/bus/events.py` |
| `memory_graph` | 文件变化自动创建/更新 entity 节点 | `cognition/memory_graph/graph.py` |

#### 2.3.5 实施步骤

**Step 3.1**: 新建模块
```
xmclaw/cognition/
└── perception/
    ├── __init__.py
    ├── models.py
    └── file_watcher.py
```

**Step 3.2**: 实现 `file_watcher.py`
- watchdog observer + event handler
- asyncio 桥接（watchdog 是线程回调，用 `asyncio.run_coroutine_threadsafe`）
- 忽略模式匹配（`.git`, `__pycache__`, `.xmclaw`, `node_modules`）

**Step 3.3**: 对接 `daemon/app.py`
- lifespan 中 `await file_watcher.start()`
- shutdown 时 `await file_watcher.stop()`

**Step 3.4**: 测试
- `tests/unit/test_file_watcher.py`（用临时目录 + 手动 touch）

---

### 2.4 evolution_loop（P1）

#### 2.4.1 架构决策

**决策 1：与现有进化系统的关系**
- 现有进化系统是"技能级"（mutate skill prompt → grader → promote）
- 新增 evolution_loop 是"系统级"（观察整体任务模式 → 假设改进方向 → 在小范围实验 → 验证是否推广）
- 两者共存：skill-level evolution 继续走现有链路，system-level evolution 走新链路

**决策 2：实验隔离**
- 复用 `enter_worktree()` / `exit_worktree()` 工具进行隔离
- 实验在独立 git worktree 中运行，不影响主工作区
- 实验结果记录到 `experiment_results` 表

#### 2.4.2 数据模型

```python
# xmclaw/cognition/evolution_loop/models.py

from dataclasses import dataclass
from typing import Literal

ExperimentStatus = Literal["pending", "running", "completed", "failed"]

@dataclass(frozen=True, slots=True)
class Experiment:
    id: str
    hypothesis: str          # "如果做 X 改变，可能提升 Y 指标"
    metric_name: str         # "task_completion_time" / "error_rate" / "token_cost"
    baseline_value: float
    experimental_value: float | None = None
    status: ExperimentStatus = "pending"
    worktree_path: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    result_summary: str | None = None
```

#### 2.4.3 数据库 Schema

```sql
-- 复用 events.db，新增 experiment_results 表

CREATE TABLE IF NOT EXISTS experiment_results (
    id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    experimental_value REAL,
    improvement_ratio REAL,  -- experimental / baseline
    status TEXT NOT NULL DEFAULT 'pending',
    worktree_path TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    result_summary TEXT
);

CREATE INDEX IF NOT EXISTS experiment_status ON experiment_results(status);
CREATE INDEX IF NOT EXISTS experiment_metric ON experiment_results(metric_name);
```

#### 2.4.4 接口定义

```python
# xmclaw/cognition/evolution_loop/loop.py

class EvolutionLoop:
    """自主实验循环 — 观察→假设→实验→验证。"""

    def __init__(
        self,
        *,
        bus: Any | None = None,
        task_scheduler: Any | None = None,
        memory_graph: Any | None = None,
        skill_registry: Any | None = None,
    ) -> None: ...

    async def start(self) -> None:
        """启动观察循环（在 daemon lifespan 中调用）。"""
        ...
    async def stop(self) -> None:
        ...

    # ── 核心循环 ──
    async def observe_recent_tasks(self, *, window_hours: int = 24) -> TaskPattern:
        """分析最近任务的平均耗时、错误率、工具使用模式。"""
        ...

    async def generate_hypothesis(self, pattern: TaskPattern) -> Hypothesis | None:
        """基于模式生成改进假设。如置信度 < 0.5 返回 None。"""
        ...

    async def run_experiment(self, hypothesis: Hypothesis) -> ExperimentResult:
        """在隔离 worktree 中运行实验。"""
        ...

    async def evaluate_experiment(self, experiment_id: str) -> bool:
        """对比实验组 vs 对照组，如提升 > 10% 返回 True。"""
        ...

    async def propose_adoption(self, experiment_id: str) -> None:
        """将实验改进提议为 skill candidate（复用现有 ProposalMaterializer）。"""
        ...

    # ── 技能自动生成 ──
    async def detect_recurring_pattern(self, *, min_occurrences: int = 3) -> Pattern | None:
        """检测重复操作模式。"""
        ...

    async def propose_skill_from_pattern(self, pattern: Pattern) -> None:
        """将模式转化为 skill 提案（进入待审核队列）。"""
        ...
```

#### 2.4.5 与现有代码对接

| 对接点 | 动作 | 文件 |
|--------|------|------|
| `EvolutionController` | 新增 `propose_system_improvement()` 入口 | `core/evolution/controller.py` |
| `ProposalMaterializer` | 接收 `EXPERIMENT_RESULT` 事件，生成 skill candidate | `daemon/proposal_materializer.py` |
| `TaskScheduler` | 提供 `observe_recent_tasks()` 所需数据 | `cognition/task_scheduler/scheduler.py` |
| `SkillRegistry` | 实验通过后调用 `registry.promote()` | `skills/registry.py` |

#### 2.4.6 实施步骤

**Step 4.1**: 新建模块
```
xmclaw/cognition/
└── evolution_loop/
    ├── __init__.py
    ├── models.py
    ├── loop.py
    └── _pattern_detector.py
```

**Step 4.2**: 实现 `observe_recent_tasks()`
- 从 `TaskScheduler` 读取最近 24h 的任务统计
- 计算：平均耗时、错误率、高频工具调用、高频 skill 调用

**Step 4.3**: 实现 `generate_hypothesis()`
- 用 LLM 分析模式，生成自然语言假设
- 示例："最近 10 次代码重构任务平均耗时 45s，其中 60% 时间花在 list_dir。如果预加载 workspace 文件树到工作记忆，可能减少 30% 时间。"

**Step 4.4**: 实现 `run_experiment()`
- 复用 `enter_worktree()` 创建隔离环境
- 在 worktree 中修改代码（如预加载文件树逻辑）
- 运行对照组任务，记录指标

**Step 4.5**: 实现 `detect_recurring_pattern()`
- 基于任务历史，用简单启发式检测重复模式
- 例如：同一 agent_id 连续 3 次调用相同工具序列

**Step 4.6**: 对接 `ProposalMaterializer`
- 实验成功后，emit `EXPERIMENT_RESULT` 事件
- `ProposalMaterializer` 订阅该事件，生成 skill candidate

**Step 4.7**: 测试
- `tests/unit/test_evolution_loop_observe.py`
- `tests/integration/test_evolution_loop_experiment.py`

---

### 2.5 认知状态管理（P0 基础设施）

#### 2.5.1 架构决策

文档 A 要求维护一个持续的认知状态（CognitiveState），包含当前目标、注意力焦点、工作记忆、情感状态、活跃计划等。

**裁决**：Phase 1 实现简化版 `CognitiveState`，不包含情感状态（可选），其他全部包含。存储在内存中，定期快照到 `~/.xmclaw/v2/cognitive_state.json`。

#### 2.5.2 数据模型

```python
# xmclaw/cognition/state.py

from dataclasses import dataclass, field
from typing import Any

@dataclass
class Goal:
    id: str
    description: str
    priority: int
    source: str  # "user" | "system" | "inferred"
    created_at: float = 0.0

@dataclass
class AttentionFocus:
    percept_id: str
    salience_score: float
    timestamp: float

@dataclass
class CognitiveState:
    current_goals: list[Goal] = field(default_factory=list)
    attention_focus: list[AttentionFocus] = field(default_factory=list)
    working_memory: list[Any] = field(default_factory=list)  # 7±2 chunks
    active_plans: list[Any] = field(default_factory=list)
    pending_actions: list[Any] = field(default_factory=list)
    last_saved: float = 0.0
```

#### 2.5.3 实施步骤

**Step 5.1**: 新建 `xmclaw/cognition/state.py`
- `CognitiveState` dataclass
- `CognitiveStateManager`：加载/保存/更新状态
- 注意力计算：`salience = w1 * urgency + w2 * relevance + w3 * novelty - w4 * fatigue`

**Step 5.2**: 对接 `AgentLoop`
- `run_turn()` 开始时更新 `attention_focus`
- `run_turn()` 结束时将提取的目标加入 `current_goals`

**Step 5.3**: 对接 `FileWatcher`
- 文件变化事件计算 salience，更新 `attention_focus`

---

## 3. 事件类型扩展

在 `xmclaw/core/bus/events.py` 的 `EventType` enum 中新增：

```python
# P0
COGNITIVE_STATE_CHANGED = "cognitive_state_changed"
TASK_STATUS_CHANGED = "task_status_changed"
TASK_DEPENDENCY_MET = "task_dependency_met"

# P1
PERCEPTION_EVENT = "perception_event"
ATTENTION_SHIFT = "attention_shift"
EXPERIMENT_RESULT = "experiment_result"
EXPERIMENT_PROPOSED = "experiment_proposed"
```

**规则**：新增事件类型必须是字符串 enum 值，保持向后兼容。已有事件类型不动。

---

## 4. 依赖关系图

```
cognition/
├── state.py              # 无外部依赖（底层）
├── memory_graph/
│   ├── models.py
│   └── graph.py          # 依赖: state.py (CognitiveState 引用)
├── task_scheduler/
│   ├── models.py
│   ├── _store.py
│   └── scheduler.py      # 依赖: state.py, bus/events.py
├── perception/
│   └── file_watcher.py   # 依赖: bus/events.py, memory_graph/
└── evolution_loop/
    ├── models.py
    └── loop.py           # 依赖: task_scheduler/, memory_graph/, skills/registry.py

daemon/app.py             # 依赖: cognition/* (lifespan 中 start/stop)
providers/memory/manager.py # 依赖: cognition/memory_graph/ (可选扩展)
core/bus/events.py        # 新增事件类型
```

---

## 5. Phase-by-Phase 实施

### Phase 0: 基础设施 + 认知状态（1 周）

**目标**：建立 `xmclaw/cognition/` 包结构，实现 `CognitiveState`，所有后续模块有地方可挂。

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 0.1 | 新建 `xmclaw/cognition/` 包 | `__init__.py`, `state.py` | 1d | `import xmclaw.cognition` 不报错 |
| 0.2 | 实现 `CognitiveState` + `CognitiveStateManager` | `cognition/state.py` | 2d | 状态可保存/加载/更新，注意力计算正确 |
| 0.3 | 新增事件类型 | `core/bus/events.py` | 0.5d | 所有新增类型在 enum 中定义 |
| 0.4 | 事件总线订阅框架 | `cognition/_bus_mixin.py` | 1d | 统一的事件发布/订阅 mixin |
| 0.5 | 单元测试 | `tests/unit/test_cognitive_state.py` | 1.5d | 全绿 |
| 0.6 | 代码审查 + lint | 全部 | 1d | ruff + mypy 全绿 |

**交付物**：
- `xmclaw/cognition/` 目录存在
- `CognitiveState` 可在 `AgentLoop` 中引用
- 新增 7 个事件类型

---

### Phase 1: memory_graph（2 周）

**目标**：实现记忆图谱核心 + 主动回忆 + 与 MemoryManager 对接。

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 1.1 | 实现 `GraphNode` + `GraphEdge` models | `cognition/memory_graph/models.py` | 0.5d | dataclass 冻结正确 |
| 1.2 | 实现 graph.db schema + 连接管理 | `cognition/memory_graph/graph.py` | 1d | WAL + busy_timeout + write_lock |
| 1.3 | 实现 CRUD（add/get/remove node/edge） | `cognition/memory_graph/graph.py` | 1d | 单元测试全过 |
| 1.4 | 实现 `get_neighbors()` | `cognition/memory_graph/graph.py` | 1d | depth=1/2/3 正确，min_strength 过滤正确 |
| 1.5 | 实现 `find_path()` | `cognition/memory_graph/graph.py` | 1d | NetworkX + SQLite CTE 双路径 |
| 1.6 | 实现 `merge_node()`（语义合并） | `cognition/memory_graph/graph.py` | 1d | 相似节点合并，不相似新建 |
| 1.7 | 实现 `proactive_recall()` | `cognition/memory_graph/graph.py` | 2d | 给定上下文返回相关历史提示 |
| 1.8 | 对接 `MemoryManager` | `providers/memory/manager.py` | 1d | query 后自动扩展邻居 |
| 1.9 | 对接 `AgentLoop`（主动回忆注入） | `daemon/agent_loop.py` | 1d | turn 开始时 proactive_recall 结果注入 user_message |
| 1.10 | 单元测试 | `tests/unit/test_memory_graph*.py` | 2d | 覆盖率 > 80% |
| 1.11 | 集成测试 | `tests/integration/test_memory_graph*.py` | 2d | 与 Manager + AgentLoop 联调通过 |
| 1.12 | 代码审查 + lint | 全部 | 1d | ruff + mypy 全绿 |

**交付物**：
- `memory_graph/graph.db` 可独立运行
- `AgentLoop` 自动进行主动回忆
- 与现有 `memory_search` 互补

---

### Phase 2: task_scheduler（2 周）

**目标**：实现任务 DAG + 状态机 + 自愈重试 + 与 Cron 对接。

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 2.1 | 实现 `Task` model + `TaskStatus` | `cognition/task_scheduler/models.py` | 0.5d | frozen dataclass |
| 2.2 | 实现 `_store.py`（SQLite CRUD） | `cognition/task_scheduler/_store.py` | 1d | 增删改查 + 按状态/优先级查询 |
| 2.3 | 实现 `submit()` + 依赖检查 | `cognition/task_scheduler/scheduler.py` | 1d | 依赖满足→PENDING，不满足→BLOCKED |
| 2.4 | 实现 `_schedule_loop()` + 优先级队列 | `cognition/task_scheduler/scheduler.py` | 1d | heapq，高优先级先执行 |
| 2.5 | 实现 `_execute()` + 超时控制 | `cognition/task_scheduler/scheduler.py` | 1d | asyncio.wait_for(timeout) |
| 2.6 | 实现失败处理（RETRYING / ESCALATED） | `cognition/task_scheduler/scheduler.py` | 1d | 指数退避，max_retries 后 ESCALATED |
| 2.7 | 实现 `_on_dependency_met()` | `cognition/task_scheduler/scheduler.py` | 0.5d | BLOCKED → PENDING 自动转换 |
| 2.8 | 实现 `get_progress()` | `cognition/task_scheduler/scheduler.py` | 0.5d | 返回 elapsed/retries/dependency_status |
| 2.9 | 对接 `CronTickTask` | `core/scheduler/cron.py` | 1d | runner 改为向 scheduler 提交 |
| 2.10 | 对接 `daemon/app.py` | `daemon/app.py` | 1d | lifespan 中构造 + start/stop |
| 2.11 | 实现 REST API router | `daemon/routers/tasks.py` | 1d | POST /tasks, GET /tasks/{id}, DELETE /tasks/{id} |
| 2.12 | 单元测试 | `tests/unit/test_task_scheduler*.py` | 2d | 状态机全覆盖 |
| 2.13 | 集成测试 | `tests/integration/test_task_scheduler*.py` | 2d | 与 Cron + AgentLoop 联调 |
| 2.14 | 代码审查 + lint | 全部 | 1d | ruff + mypy 全绿 |

**交付物**：
- 任务可带依赖提交
- Cron job 走 TaskScheduler 执行
- REST API 可查询任务进度

---

### Phase 3: file_watcher + perception（1 周）

**目标**：文件系统感知，只记录不打扰。

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 3.1 | 实现 `FileEvent` model | `cognition/perception/models.py` | 0.5d | dataclass 正确 |
| 3.2 | 实现 `FileWatcher`（watchdog 版） | `cognition/perception/file_watcher.py` | 1.5d | 可监控、可忽略、可发布事件 |
| 3.3 | 实现轮询 fallback | `cognition/perception/file_watcher.py` | 1d | watchdog 不可用时降级 |
| 3.4 | 对接 `daemon/app.py` | `daemon/app.py` | 0.5d | lifespan start/stop |
| 3.5 | 对接 `memory_graph`（entity 节点） | `cognition/perception/file_watcher.py` | 1d | 文件变化自动更新图谱 |
| 3.6 | 单元测试 | `tests/unit/test_file_watcher.py` | 1d | 临时目录测试通过 |
| 3.7 | 代码审查 + lint | 全部 | 0.5d | ruff + mypy 全绿 |

**交付物**：
- 文件变化被记录到感知缓冲区
- 变化自动创建/更新 memory_graph 的 entity 节点

---

### Phase 4: evolution_loop（2 周）

**目标**：自主实验循环框架。

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 4.1 | 实现 `Experiment` model | `cognition/evolution_loop/models.py` | 0.5d | dataclass 正确 |
| 4.2 | 实现 `observe_recent_tasks()` | `cognition/evolution_loop/loop.py` | 1d | 从 TaskScheduler 读取统计 |
| 4.3 | 实现 `generate_hypothesis()` | `cognition/evolution_loop/loop.py` | 1.5d | LLM 驱动，置信度过滤 |
| 4.4 | 实现 `run_experiment()`（worktree 隔离） | `cognition/evolution_loop/loop.py` | 2d | 隔离运行，指标收集 |
| 4.5 | 实现 `evaluate_experiment()` | `cognition/evolution_loop/loop.py` | 1d | 10% 提升阈值 |
| 4.6 | 实现 `detect_recurring_pattern()` | `cognition/evolution_loop/_pattern_detector.py` | 1d | 启发式检测重复模式 |
| 4.7 | 对接 `ProposalMaterializer` | `daemon/proposal_materializer.py` | 1d | EXPERIMENT_RESULT → skill candidate |
| 4.8 | 对接 `daemon/app.py` | `daemon/app.py` | 0.5d | lifespan start/stop |
| 4.9 | 单元测试 | `tests/unit/test_evolution_loop*.py` | 2d | 观察+假设+实验 全链路 |
| 4.10 | 代码审查 + lint | 全部 | 0.5d | ruff + mypy 全绿 |

**交付物**：
- 实验可自动设计、执行、评估
- 成功实验自动提议为 skill candidate

---

### Phase 5: 整合与优化（1 周）

| # | 任务 | 文件 | 工作量 | 验收标准 |
|---|------|------|--------|----------|
| 5.1 | 认知循环闭环 | `cognition/` 全局 | 2d | 感知→注意→记忆→推理→规划→执行→学习 数据流完整 |
| 5.2 | 状态持久化 | `cognition/state.py` | 1d | CognitiveState 定期快照到磁盘 |
| 5.3 | 性能优化 | `cognition/memory_graph/graph.py` | 1d | NetworkX 热点缓存 |
| 5.4 | 文档更新 | `docs/JARVIS_IMPLEMENTATION_PLAN.md` | 0.5d | 与实际代码一致 |
| 5.5 | 端到端测试 | `tests/e2e/test_jarvis_cognitive_loop.py` | 1.5d | 完整认知循环一次 |
| 5.6 | 代码审查 + lint | 全部 | 1d | ruff + mypy 全绿 |

**交付物**：
- 系统可自主运行超过 1 小时无需用户输入
- 所有新增模块通过集成测试

---

## 6. 配置文件扩展

在 `daemon/config.json` 中新增 `cognition` 段：

```json
{
  "cognition": {
    "enabled": true,
    "memory_graph": {
      "enabled": true,
      "db_path": "~/.xmclaw/v2/graph.db",
      "embedding_dim": 1024,
      "proactive_recall": true
    },
    "task_scheduler": {
      "enabled": true,
      "max_concurrent": 3,
      "default_timeout_seconds": 300,
      "default_max_retries": 3
    },
    "perception": {
      "enabled": true,
      "watch_paths": ["~/Desktop", "~/Documents"],
      "ignore_patterns": [".git", "__pycache__", ".xmclaw", "node_modules", ".venv"],
      "contextually_relevant_only": false
    },
    "evolution_loop": {
      "enabled": true,
      "observe_window_hours": 24,
      "min_confidence": 0.5,
      "improvement_threshold": 1.1,
      "auto_skill_creation": true,
      "min_pattern_occurrences": 3
    }
  }
}
```

**规则**：所有新增配置默认 `enabled: false`，用户显式开启后才激活。避免破坏现有安装。

---

## 7. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `XMCLAW_COGNITION_ENABLED` | `false` | 总开关 |
| `XMCLAW_GRAPH_DB` | `~/.xmclaw/v2/graph.db` | 图谱数据库路径 |
| `XMCLAW_TASK_DB` | `~/.xmclaw/v2/events.db` | 任务数据库路径（复用 events.db） |
| `XMCLAW_MAX_CONCURRENT_TASKS` | `3` | 最大并发任务数 |
| `XMCLAW_TASK_TIMEOUT` | `300` | 任务超时秒数 |
| `XMCLAW_MAX_RETRIES` | `3` | 任务最大重试次数 |
| `XMCLAW_WATCH_PATHS` | `''` | 监控路径（逗号分隔） |
| `XMCLAW_EVOLUTION_LOOP_ENABLED` | `false` | 自主实验循环开关 |

---

## 8. 测试策略

### 8.1 新增测试文件

```
tests/
├── unit/
│   ├── test_cognitive_state.py
│   ├── test_memory_graph_crud.py
│   ├── test_memory_graph_neighbors.py
│   ├── test_memory_graph_path.py
│   ├── test_memory_graph_proactive_recall.py
│   ├── test_task_scheduler_states.py
│   ├── test_task_scheduler_dependencies.py
│   ├── test_task_scheduler_retry.py
│   ├── test_file_watcher.py
│   └── test_evolution_loop_observe.py
├── integration/
│   ├── test_memory_graph_with_manager.py
│   ├── test_task_scheduler_with_cron.py
│   ├── test_task_scheduler_with_agent.py
│   └── test_evolution_loop_experiment.py
└── e2e/
    └── test_jarvis_cognitive_loop.py
```

### 8.2 测试纪律

每 Phase 完成后：
1. `python scripts/test_changed.py` 全绿
2. `pytest tests/unit/test_<phase>*.py -v` 全绿
3. `ruff check xmclaw/` 全绿
4. `mypy xmclaw/cognition/` 全绿（新增代码）

---

## 9. 风险评估与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 架构过于复杂 | 中 | 高 | 分 Phase 实施，每 Phase 可独立回滚；默认配置全部 disabled |
| watchdog 在 Windows 上不稳定 | 中 | 中 | 轮询 fallback；监控路径默认只包用户目录 |
| 图谱查询性能差 | 中 | 高 | NetworkX 内存缓存 + SQLite 索引 + 限制遍历深度 |
| 任务调度死锁 | 低 | 高 | 超时机制 + 依赖环检测（submit 时拓扑排序检查） |
| 认知循环无限自举 | 低 | 高 | 每小时最大实验次数限制 + 人工审批 gate |
| 数据不一致（memory.db vs graph.db） | 中 | 中 | 统一 ID 系统；memory_item_id 外键；定期一致性检查 |
| LLM 成本爆炸 | 中 | 中 | 假设生成用轻量模型；实验频率限制；本地模型 fallback |
| 用户不信任自主行为 | 高 | 高 | 透明日志（所有自主行动记录到事件总线）；用户可随时查看状态；危险操作仍需审批 |

---

## 10. 成功标准

| 标准 | Phase | 验收方式 |
|------|-------|----------|
| 系统可自主运行超过 24 小时无需用户输入 | 5 | 长时间运行测试 |
| 用户感觉"系统懂我" | 1, 5 | 用户反馈调查 |
| 任务完成效率提升 50%+ | 2, 5 | 对比实验（有/无任务 DAG） |
| 新技能学习周期缩短 70%+ | 4, 5 | 对比实验（有/无自主实验） |
| 用户满意度 > 4.5/5 | 5 | 调查问卷 |
| 测试覆盖率 > 80% | 全部 | `pytest --cov` |

---

## 11. 实施纪律

1. **每 Phase 完成后**：
   - `git commit -m "Phase N: <动作> (依据 JARVIS_IMPLEMENTATION_PLAN §X.Y)"`
   - `docs/DEV_ROADMAP.md` 加一行进度日志

2. **代码规范**：
   - 遵循现有 `xmclaw/` 代码风格（ruff line-length=100, target=py310）
   - 所有公共类/方法必须有 docstring
   - 所有 DB 操作必须有 WAL + busy_timeout
   - 所有 async 操作必须考虑 cancellation

3. **回滚策略**：
   - 每 Phase 独立分支：`feature/jarvis-phase-N`
   - Phase N 完成后合并到 `feature/jarvis-main`
   - 如某 Phase 失败，回滚该分支，不影响已完成 Phase

4. **与现有代码的边界**：
   - 不删除任何现有文件（Phase 8 大扫除单独进行）
   - 不修改 `core/grader/`, `core/evolution/controller.py`, `skills/registry.py` 的核心逻辑
   - 新增代码放在 `xmclaw/cognition/` 包下

---

**End of doc.** 按 Phase 0 → 1 → 2 → 3 → 4 → 5 顺序执行。
