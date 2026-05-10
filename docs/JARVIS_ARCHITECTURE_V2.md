# XMclaw 贾维斯化 V2 — 大胆框架性重构设计

> **版本**: v2.0-alpha
> **日期**: 2026-05-09
> **定位**: 取代 `JARVIS_IMPLEMENTATION_PLAN.md` 的保守方案
> **核心决策**: **AgentLoop 已死。CognitiveEngine 当立。**

---

## 0. 核心诊断：现有架构的死结

### 0.1 AgentLoop：3129 行的认知癌症

`xmclaw/daemon/agent_loop.py` 不是一个类，而是一整个应用的尸体缝合：

| 职责 | 行数范围 | 问题 |
|------|----------|------|
| 系统提示构建 | 82-537 | 硬编码 450 行字符串拼接，含中英文混合、4 层嵌套规则 |
| 记忆注入/召回 | 540-580 | `prefetch()` + `query()` 两套路径，同一会话 60s 去重逻辑埋在方法里 |
| 历史压缩 | 824-849, 2820-2980 | `_maybe_compress_messages()` 分散 3 处，LLM 压缩和规则压缩竞争 |
| ReAct 循环 | 1710-1900 | `for hop in range(max_hops)` 硬编码，无中断点、无子任务委托 |
| 工具执行 | 1900-2100 | 每个工具调用内联 grader + anti-loop + policy-scan + retry，无法复用 |
| 持久化 | 2820-2980 | `_persist_history()` 在 turn 结束后一次性写盘，无中间 checkpoint |
| 后采样钩子 | 3000-3050 | fire-and-forget `asyncio.create_task`，无 backpressure |

**15 个外部组件直接读取 `agent._*` 私有属性**：
- `app.py` lifespan 读 `agent._llm`（Dream cron / ProfileExtractor / SkillDreamCycle）
- `app.py` lifespan 读 `agent._tools`（MCP hub 变异注入、_find_skill_provider 树遍历）
- `app.py` lifespan 读 `agent._histories`（session reflection 直接访问）
- `app.py` lifespan 读 `agent._memory_manager`（on_session_end hook）
- `routers/*.py` 读 `agent.run_turn()`（18 个路由入口）

AgentLoop 是 **god object + implicit interface** 的终极形态。任何改动都会引发 15+ 处的连锁断裂。

### 0.2 daemon/app.py lifespan：22 个手动拼接的定时炸弹

```python
async def _lifespan(_app: FastAPI):
    await sweep_task.start()           # 1
    await backup_scheduler.start()     # 2
    await events_retention_task.start() # 3
    await cron_tick.start()            # 4
    # ... 记忆索引器块（150 行）
    await config_watcher.start()       # 6
    # ... 配置重载订阅（80 行）
    await dream_cron.start()           # 7
    await agents_manager.load_from_disk() # 8
    await orchestrator.start()         # 9
    await evolution_agent.start()      # 10
    await evolution_evaluation_trigger.start() # 11
    await variant_selector.start()     # 12
    await journal_writer.start()       # 13
    await profile_extractor.start()    # 14
    await skill_dream.start()          # 15
    await realtime_evolution.start()   # 16
    await sleep_worker.start()         # 17
    await skills_watcher.start()       # 18
    await mutation_orchestrator.start() # 19
    await proposal_materializer.start() # 20
    await channel_dispatcher.start_all() # 21
    await mcp_hub.reload_from_config() # 22
```

**问题**：
- 无统一生命周期管理：每个 `.start()` 独立 try/except，失败静默吞掉
- 无依赖声明：sleep_worker 必须在 skill_dream 之前停止（否则 idle-fired 任务调用已停止 downstream），这个顺序是**人肉维护**的
- 无健康检查：22 个任务中任一个挂了，系统继续跑，用户无从知晓
- 启动顺序与组件耦合：ChannelDispatcher 需要 `agent`，MCPHub 需要 `agent._tools`，所有进化组件需要 `orchestrator`

### 0.3 事件总线：买了不用

`InProcessEventBus` 有 50+ 事件类型，但模块之间**仍然大量直接调用**：

```python
# 反例 1：ProfileExtractor 直接读 agent._llm
embedder = getattr(agent, "_llm", None)

# 反例 2：MCPHub 直接变异 agent._tools
agent._tools = CompositeToolProvider(agent._tools, MCPHub(...))

# 反例 3：Session reflection 直接读 agent._histories
prior = list(agent._histories.get(session_id, []))

# 反例 4：Cron runner 直接调用 agent.run_turn()
res = await target_agent.run_turn(sid, job.prompt)
```

事件总线成了**纯 observability 层**（给 UI 看的），不是**通信层**。模块之间没有通过事件解耦。

### 0.4 记忆系统：抽象了，但没真抽象

`MemoryProvider` ABC 定义了 6 个方法，但 `MemoryManager` 只支持 **1 builtin + 1 external**：

```python
def add_provider(self, provider: MemoryProvider) -> bool:
    if not is_builtin and self._has_external:
        return False  # 第二个外部 provider 直接拒绝
```

**实际能力**:
- `BuiltinFileMemoryProvider` — 读取文本文件
- `SqliteVecMemory` — 向量搜索

**缺失能力**:
- 无关系查询（"这件事导致那件事"）
- 无时序查询（"上周三下午做了什么"）
- 无工作记忆容量管理（7±2 chunks）
- 写入时无统一 ID，三种索引各自为政

---

## 1. 新架构：CognitiveEngine

### 1.1 核心范式转变

| 维度 | 旧架构（AgentLoop） | 新架构（CognitiveEngine） |
|------|---------------------|---------------------------|
| 运行模式 | 事件-响应（用户输入 → 处理 → 等待） | 持续认知循环（不依赖用户输入） |
| 架构模式 | God Object + 直接调用 | 管道（Pipeline）+ 事件驱动 |
| 记忆模型 | 扁平 provider 列表 | 功能分层 × 维度索引 |
| 进化模式 | 外挂（EvolutionController 旁观 AgentLoop） | 内置（LearningSystem 订阅所有认知事件） |
| 任务调度 | Cron 定时 + submit_to_agent 后台 | 统一感知事件流 → 注意力筛选 → 任务 DAG |
| 配置管理 | 分散（config.json + cron/jobs.json + state.json + persona files） | 统一（config.json 为根，其他为引用或缓存） |

### 1.2 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CognitiveEngine                              │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                      Cognitive Pipeline                        │  │
│  │                                                                │  │
│  │   ┌──────────┐    ┌──────────┐    ┌──────────────────────┐   │  │
│  │   │Perception│───►│ Attention│───►│   Unified Memory     │   │  │
│  │   │  Layer   │    │  Module  │    │      System          │   │  │
│  │   └──────────┘    └──────────┘    └──────────────────────┘   │  │
│  │         │                │                   │                │  │
│  │         │                │                   ▼                │  │
│  │         │                │           ┌──────────────────┐    │  │
│  │         │                │           │ Reasoning Engine │    │  │
│  │         │                │           └──────────────────┘    │  │
│  │         │                │                   │                │  │
│  │         │                │                   ▼                │  │
│  │         │                │           ┌──────────────────┐    │  │
│  │         │                │           │ Planning System  │    │  │
│  │         │                │           └──────────────────┘    │  │
│  │         │                │                   │                │  │
│  │         └────────────────┴───────────────────┘                │  │
│  │                              │                                │  │
│  │                              ▼                                │  │
│  │                    ┌──────────────────┐                       │  │
│  │                    │ Execution Layer  │                       │  │
│  │                    │ (Tools / Code /  │                       │  │
│  │                    │  User / API)     │                       │  │
│  │                    └──────────────────┘                       │  │
│  │                              │                                │  │
│  │                              ▼                                │  │
│  │                    ┌──────────────────┐                       │  │
│  │                    │ Learning System  │                       │  │
│  │                    │ (Evolution Loop) │                       │  │
│  │                    └──────────────────┘                       │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│  ┌───────────────────────┴───────────────────────────────────────┐  │
│  │                      Unified Memory System                     │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────────┐  │  │
│  │  │ Working │  │ Short-  │  │  Long-  │  │  Procedural     │  │  │
│  │  │ Memory  │  │  Term   │  │  Term   │  │  (Skills)       │  │  │
│  │  │(in-mem) │  │(SQLite) │  │(SQLite) │  │  (skill files)  │  │  │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────────────┘  │  │
│  │       │              │              │              │          │  │
│  │  ┌────┴──────────────┴──────────────┴──────────────┴─────┐   │  │
│  │  │                   Index Layer                          │   │  │
│  │  │  Vector (sqlite-vec) │ Graph (NetworkX) │ Temporal    │   │  │
│  │  └────────────────────────────────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│  ┌───────────────────────┴───────────────────────────────────────┐  │
│  │                        Event Bus                               │  │
│  │   (所有跨边界通信通过事件，模块之间不直接调用)                  │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 关键设计原则

**原则 1：管道优于对象**
- 认知循环的每个阶段是一个独立的、可替换的组件
- 组件之间通过**显式接口**通信，不直接访问私有属性
- 数据流是单向的（感知 → 注意 → 记忆 → 推理 → 规划 → 执行 → 学习）

**原则 2：事件是唯一的跨边界通信方式**
- CognitiveEngine 内部模块通过**函数调用**通信（性能）
- CognitiveEngine ↔ daemon ↔ 外部通过**EventBus**通信（解耦）
- 所有自主行为产生事件，形成完整的审计链

**原则 3：记忆是核心，不是附属**
- UnifiedMemorySystem 是 CognitiveEngine 的核心依赖
- 所有认知阶段都读写记忆
- 记忆写入时自动更新所有索引，保证一致性

**原则 4：进化是内置能力，不是外挂**
- LearningSystem 订阅所有认知事件
- 从经验中自动提取模式、生成假设、运行实验
- 实验成功直接修改系统自身（skill 生成、参数调优）

---

## 2. 模块详细设计

### 2.1 CognitiveEngine（取代 AgentLoop）

```python
# xmclaw/cognition/engine.py

class CognitiveEngine:
    """自主认知引擎 — 持续运行的认知循环 orchestrator。
    
    取代 AgentLoop 的所有协调逻辑。
    用户输入只是 PerceptionLayer 的一种输入源。
    """

    def __init__(self, config: EngineConfig) -> None:
        self.state = CognitiveState()
        self.perception = PerceptionLayer(config.perception)
        self.attention = AttentionModule(config.attention)
        self.memory = UnifiedMemorySystem(config.memory)
        self.reasoning = ReasoningEngine(config.reasoning, memory=self.memory)
        self.planning = PlanningSystem(config.planning, memory=self.memory)
        self.execution = ExecutionLayer(config.execution)
        self.learning = LearningSystem(config.learning, memory=self.memory)
        self.bus = InProcessEventBus()
        
        # 注册内部事件流
        self._wire_internal_events()

    def _wire_internal_events(self) -> None:
        """连接认知循环各阶段的事件流。"""
        # Perception → Attention
        self.perception.on_percept = self.attention.submit
        # Attention → Memory (查询)
        self.attention.on_focus = self._on_attention_focus
        # Memory + Attention → Reasoning
        # Reasoning → Planning
        # Planning → Execution
        self.planning.on_plan_ready = self.execution.execute
        # Execution → Learning
        self.execution.on_result = self.learning.learn
        # Execution → Memory (写入)
        self.execution.on_result = self.memory.store_event

    async def start(self) -> None:
        """启动认知循环。在 daemon lifespan 中调用。"""
        await self.perception.start()
        await self.memory.start()
        await self.learning.start()
        self._cognitive_task = asyncio.create_task(
            self._cognitive_loop(), name="cognitive-loop"
        )
        await self.bus.publish(make_event(
            session_id="_system", agent_id="engine",
            type=EventType.COGNITIVE_ENGINE_STARTED,
            payload={},
        ))

    async def stop(self) -> None:
        """优雅停止认知循环。"""
        self._stop.set()
        if self._cognitive_task:
            try:
                await asyncio.wait_for(self._cognitive_task, timeout=30.0)
            except asyncio.TimeoutError:
                self._cognitive_task.cancel()
        await self.perception.stop()
        await self.memory.stop()
        await self.learning.stop()

    async def _cognitive_loop(self) -> None:
        """核心认知循环。
        
        不是 `while True` 的忙等，而是基于事件的驱动：
        - 有感知事件时：处理事件流
        - 无感知事件时：执行维护任务（记忆压缩、学习回放、自主目标生成）
        """
        while not self._stop.is_set():
            # Phase 1: 收集感知
            percepts = await self.perception.collect(timeout=1.0)
            if percepts:
                await self._process_percepts(percepts)
            else:
                await self._idle_maintenance()

    async def _process_percepts(self, percepts: list[Percept]) -> None:
        """处理一批感知事件。"""
        for percept in percepts:
            # 显著性计算
            salience = self.attention.compute_salience(percept, self.state)
            if salience < self.attention.threshold:
                continue  # 忽略低显著性事件
            
            # 更新注意力焦点
            self.state.attention_focus.add(percept, salience)
            
            # 记忆检索
            memories = await self.memory.query(
                MemoryQuery(
                    semantic=percept.content,
                    temporal=TimeRange.recent(minutes=30),
                    limit=10,
                )
            )
            
            # 推理
            reasoning = await self.reasoning.reason(
                percept=percept,
                memories=memories,
                state=self.state,
            )
            
            # 规划
            plan = await self.planning.plan(
                reasoning=reasoning,
                state=self.state,
            )
            
            # 执行
            if plan.actions:
                result = await self.execution.execute(plan, state=self.state)
                await self.learning.learn(result)

    async def _idle_maintenance(self) -> None:
        """空闲时执行维护任务。"""
        # 记忆压缩
        if self.memory.needs_compression():
            await self.memory.compress()
        # 学习回放
        if self.learning.needs_replay():
            await self.learning.replay()
        # 自主目标生成
        if self.state.current_goals.is_empty():
            goal = await self.learning.generate_maintenance_goal()
            if goal:
                self.state.current_goals.add(goal)

    # ── 外部接口 ──
    
    async def submit_user_turn(
        self, session_id: str, message: str, **kwargs
    ) -> TurnResult:
        """用户提交一轮对话。
        
        不是直接处理，而是包装为感知事件进入认知循环。
        等待该 session 的 turn 完成，返回结果。
        """
        percept = UserMessagePercept(
            session_id=session_id,
            content=message,
            timestamp=time.time(),
            **kwargs,
        )
        future = self._register_turn_future(session_id)
        await self.perception.inject(percept)
        return await future

    async def cancel_session(self, session_id: str) -> None:
        """取消 session 中正在进行的 turn。"""
        self.state.cancel_events[session_id].set()

    async def pop_last_turn(self, session_id: str) -> None:
        """撤销 session 的最后一轮对话。"""
        await self.memory.pop_session_turn(session_id)
```

### 2.2 UnifiedMemorySystem（取代 MemoryManager）

```python
# xmclaw/cognition/memory/system.py

class UnifiedMemorySystem:
    """统一记忆系统 — 功能分层 × 维度索引。
    
    取代 MemoryManager + 多个独立 provider 的模式。
    所有存储层共享统一 ID 系统。
    写入时自动更新所有索引。
    """

    def __init__(self, config: MemoryConfig) -> None:
        # 存储层（按功能分层）
        self.working = WorkingMemoryStore(
            capacity=config.working_capacity,  # 7±2 chunks
        )
        self.short_term = ShortTermStore(
            db_path=config.short_term_db,
            ttl=config.short_term_ttl,  # 1 day
        )
        self.long_term = LongTermStore(
            db_path=config.long_term_db,
        )
        self.procedural = ProceduralStore(
            skill_roots=config.skill_roots,
        )
        
        # 索引层（按查询维度）
        self.vector_index = VectorIndex(
            db_path=config.vector_db,
            embedding_dim=config.embedding_dim,
        )
        self.graph_index = GraphIndex(
            db_path=config.graph_db,
        )
        self.temporal_index = TemporalIndex(
            db_path=config.temporal_db,
        )
        
        # 一致性管理
        self._write_lock = asyncio.Lock()

    async def store(self, item: MemoryItem) -> str:
        """统一写入。
        
        1. 根据功能决定存储层
        2. 同时更新三种索引
        3. 失败时回滚
        """
        async with self._write_lock:
            tx_id = uuid.uuid4().hex
            try:
                # 写入存储层
                layer = self._resolve_layer(item)
                await self._store_in_layer(layer, item)
                
                # 更新索引
                await self.vector_index.index(item)
                await self.graph_index.index(item)
                await self.temporal_index.index(item)
                
                return item.id
            except Exception:
                await self._rollback(tx_id)
                raise

    async def query(self, query: MemoryQuery) -> list[MemoryItem]:
        """组合查询。
        
        支持语义 + 关系 + 时序的组合过滤。
        示例：
            query = MemoryQuery(
                semantic="数据库优化",
                relation=RelationQuery("项目X", depth=2),
                temporal=TimeRange.last_week(),
                layer=Layer.LONG_TERM,
                limit=10,
            )
        """
        # 1. 确定候选集（从指定存储层）
        candidates = await self._get_layer_candidates(query.layer)
        
        # 2. 向量过滤（语义维度）
        if query.semantic:
            semantic_hits = await self.vector_index.search(
                query.semantic, k=query.limit * 3
            )
            candidates = self._intersect(candidates, semantic_hits)
        
        # 3. 图谱过滤（关系维度）
        if query.relation:
            related_ids = await self.graph_index.traverse(
                query.relation.node_id,
                depth=query.relation.depth,
            )
            candidates = self._intersect(candidates, related_ids)
        
        # 4. 时序过滤（时间维度）
        if query.temporal:
            candidates = [
                c for c in candidates
                if query.temporal.contains(c.timestamp)
            ]
        
        # 5. 排序和截断
        return candidates[:query.limit]

    async def proactive_recall(self, context: str) -> str:
        """主动回忆 — 基于当前上下文推送相关历史。"""
        # 1. 提取当前意图
        intent = await self._extract_intent(context)
        
        # 2. 在记忆图谱中找相似意图
        similar_intents = await self.graph_index.find_similar(
            intent, type="intent", k=3
        )
        
        # 3. 遍历 LEADS_TO 边找历史事件
        memories = []
        for intent_node in similar_intents:
            events = await self.graph_index.get_neighbors(
                intent_node.id, relation="LEADS_TO", depth=1
            )
            memories.extend(events)
        
        # 4. 格式化为提示文本
        if memories:
            return self._format_proactive_recall(memories)
        return ""
```

### 2.3 PerceptionLayer（统一感知）

```python
# xmclaw/cognition/perception/layer.py

class PerceptionLayer:
    """感知层 — 持续监控环境，检测变化和事件。
    
    取代：CronTickTask + ChannelDispatcher 的部分功能 + 新增 file/process 监控
    """

    def __init__(self, config: PerceptionConfig) -> None:
        self.sources: list[PerceptionSource] = []
        self._percept_queue: asyncio.Queue[Percept] = asyncio.Queue()
        self._callback: Callable[[Percept], Awaitable[None]] | None = None

    def add_source(self, source: PerceptionSource) -> None:
        self.sources.append(source)

    async def start(self) -> None:
        for source in self.sources:
            await source.start(self._on_source_percept)

    async def stop(self) -> None:
        for source in self.sources:
            await source.stop()

    async def inject(self, percept: Percept) -> None:
        """外部注入感知事件（如用户消息）。"""
        await self._percept_queue.put(percept)

    async def collect(self, timeout: float = 1.0) -> list[Percept]:
        """收集一批感知事件。"""
        percepts: list[Percept] = []
        try:
            while len(percepts) < 10:
                percept = await asyncio.wait_for(
                    self._percept_queue.get(), timeout=0.1
                )
                percepts.append(percept)
        except asyncio.TimeoutError:
            pass
        return percepts

    async def _on_source_percept(self, percept: Percept) -> None:
        await self._percept_queue.put(percept)


class PerceptionSource(ABC):
    """感知源抽象基类。"""
    
    @abstractmethod
    async def start(self, callback: Callable[[Percept], Awaitable[None]]) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...


class UserInputSource(PerceptionSource):
    """用户输入感知源（WS / CLI / Channel）。"""
    
class FileWatcherSource(PerceptionSource):
    """文件系统监控（watchdog）。"""
    
class CronSource(PerceptionSource):
    """定时触发（取代 CronTickTask）。"""
    
class ProcessWatcherSource(PerceptionSource):
    """进程监控。"""
    
class TimeTriggerSource(PerceptionSource):
    """基于上下文的适时提醒。"""
```

### 2.4 AttentionModule（注意力机制）

```python
# xmclaw/cognition/attention/module.py

class AttentionModule:
    """注意力模块 — 从海量感知信息中筛选值得关注的部分。"""

    def __init__(self, config: AttentionConfig) -> None:
        self.window = AttentionWindow(capacity=config.window_size)
        self.threshold = config.salience_threshold
        self.fatigue_decay = config.fatigue_decay

    def compute_salience(self, percept: Percept, state: CognitiveState) -> float:
        """计算感知事件的显著性分数。
        
        salience = w1 * urgency + w2 * relevance + w3 * novelty - w4 * fatigue
        """
        urgency = self._compute_urgency(percept)
        relevance = self._compute_relevance(percept, state)
        novelty = self._compute_novelty(percept, state)
        fatigue = self._compute_fatigue(percept, state)
        
        return (
            config.weights.urgency * urgency +
            config.weights.relevance * relevance +
            config.weights.novelty * novelty -
            config.weights.fatigue * fatigue
        )

    def submit(self, percept: Percept) -> bool:
        """提交感知事件到注意力窗口。"""
        if len(self.window) >= self.window.capacity:
            # 淘汰最低显著性的事件
            self.window.evict_lowest()
        self.window.add(percept)
        return True
```

### 2.5 TaskScheduler（任务 DAG）

```python
# xmclaw/cognition/planning/scheduler.py

class TaskScheduler:
    """任务 DAG 调度器 — 依赖拓扑排序、自愈重试、优先级抢占。
    
    集成在 PlanningSystem 中，不是独立模块。
    """

    async def submit(self, task: Task) -> str:
        """提交任务。"""
        # 检查依赖环
        if self._has_cycle(task):
            raise ValueError("Task dependency cycle detected")
        
        # 检查依赖是否满足
        if await self._dependencies_met(task):
            task.status = "pending"
            self._pending_queue.push(task)
        else:
            task.status = "blocked"
            self._blocked_tasks[task.id] = task
        
        await self._store.save(task)
        return task.id

    async def _schedule_loop(self) -> None:
        """主调度循环。"""
        while self._running:
            # 按优先级取任务
            task = self._pending_queue.pop_highest_priority()
            if task is None:
                await asyncio.sleep(1.0)
                continue
            
            # 检查抢占
            if await self._should_preempt(task):
                await self._preempt_lower_priority(task)
            
            # 执行任务
            await self._execute(task)
```

### 2.6 LearningSystem（内置进化）

```python
# xmclaw/cognition/learning/system.py

class LearningSystem:
    """学习系统 — 从经验中学习，改进未来表现。
    
    取代外挂了 EvolutionController 的进化模式。
    订阅所有认知事件，持续学习。
    """

    def __init__(self, config: LearningConfig, memory: UnifiedMemorySystem) -> None:
        self.memory = memory
        self.experiment_loop = ExperimentLoop(config.experiment)
        self.skill_distiller = SkillDistiller(config.skill)
        self.pattern_detector = PatternDetector(config.pattern)

    async def learn(self, result: ExecutionResult) -> None:
        """从执行结果中学习。"""
        # 1. 记录经验
        await self.memory.store(
            MemoryItem(
                layer="long",
                content=result.summary,
                metadata={"type": "experience", "plan_id": result.plan_id},
            )
        )
        
        # 2. 检测模式
        pattern = await self.pattern_detector.detect()
        if pattern and pattern.confidence > 0.8:
            await self._propose_skill(pattern)
        
        # 3. 评估是否需要实验
        if await self._should_experiment(result):
            await self.experiment_loop.run_experiment(result)

    async def replay(self) -> None:
        """经验回放 — 定期回顾历史经验。"""
        experiences = await self.memory.query(
            MemoryQuery(
                temporal=TimeRange.last(days=7),
                metadata_filter={"type": "experience"},
                limit=50,
            )
        )
        # 用 LLM 分析经验，提取通用模式
        patterns = await self._extract_patterns_from_experiences(experiences)
        for pattern in patterns:
            await self._propose_skill(pattern)
```

---

## 3. 与现有代码的关系

### 3.1 迁移策略：并行建设 + 配置切换

```
Phase 1: 新架构建设（cognition/ 包）
    │
    ├── 现有代码不动，标记为 xmclaw/legacy/
    │
    ├── 新架构在 xmclaw/cognition/ 中独立开发
    │
    ├── 定义 TurnOrchestrator Protocol
    │   （让现有 router / lifespan 可以同时兼容新旧架构）
    │
    └── 新增测试覆盖新架构

Phase 2: 配置切换
    │
    ├── config.json 加 "cognitive_engine.enabled": false（默认）
    │
    ├── 用户可手动开启："cognitive_engine.enabled": true
    │
    └── 新旧架构并行运行，对比验证

Phase 3: 全面迁移
    │
    ├── 所有 router 从 agent.run_turn 切换到 engine.submit_user_turn
    │
    ├── lifespan 从 22 个手动任务切换到 engine.start()
    │
    ├── 迁移所有测试
    │
    └── 删除 legacy/ 代码
```

### 3.2 向后兼容协议

```python
# xmclaw/cognition/interfaces.py

class TurnOrchestrator(Protocol):
    """取代 AgentLoop 的接口协议。
    
    让现有 router / lifespan / channel dispatcher 可以同时兼容新旧架构。
    """
    
    async def run_turn(
        self, session_id: str, message: str,
        *, tools_allowlist: set[str] | None = None,
        llm_profile_id: str | None = None,
    ) -> TurnResult: ...
    
    async def cancel_session(self, session_id: str) -> None: ...
    async def pop_last_turn(self, session_id: str) -> None: ...
    
    @property
    def llm(self) -> LLMProvider: ...
    @property
    def tools(self) -> ToolProvider: ...
    @property
    def memory(self) -> Any: ...
    @property
    def histories(self) -> dict[str, list[Any]]: ...


class CognitiveEngine(TurnOrchestrator):
    """新架构实现 TurnOrchestrator 协议。
    
    这样现有代码不需要改接口调用，只需要换实现。
    """
    
    async def run_turn(self, session_id: str, message: str, **kwargs) -> TurnResult:
        """兼容旧接口，内部转发到 submit_user_turn。"""
        return await self.submit_user_turn(session_id, message, **kwargs)
```

### 3.3 daemon lifespan 重构

```python
# xmclaw/daemon/lifespan.py（新建）

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    config = _app.state.config
    
    if config.get("cognitive_engine", {}).get("enabled", False):
        # 新架构：一键启动
        from xmclaw.cognition.engine_factory import build_engine_from_config
        engine = build_engine_from_config(config)
        _app.state.engine = engine
        await engine.start()
        
        # 外部服务（channel adapters 等）
        await channel_dispatcher.start_all(engine)
        
        yield
        
        await channel_dispatcher.stop_all()
        await engine.stop()
    else:
        # 旧架构：保持现有逻辑
        await _legacy_lifespan(_app)
        yield
        await _legacy_shutdown(_app)
```

### 3.4 现有模块的归属

| 现有模块 | 新归属 | 处理方式 |
|----------|--------|----------|
| `daemon/agent_loop.py` | 废弃 | 功能拆分到 cognition/engine.py + 各子模块 |
| `providers/memory/manager.py` | 废弃 | 功能合并到 cognition/memory/system.py |
| `providers/memory/sqlite_vec.py` | 迁移 | 成为 VectorIndex + ShortTermStore 的后端 |
| `core/scheduler/cron.py` | 迁移 | CronStore 保留，CronTickTask 被 PerceptionLayer.CronSource 取代 |
| `core/evolution/controller.py` | 保留 | LearningSystem 调用现有 controller 做 promotion gate |
| `core/evolution/mutator.py` | 保留 | LearningSystem 调用现有 mutator 做 skill mutation |
| `skills/registry.py` | 保留 | ProceduralStore 调用现有 registry |
| `core/grader/verdict.py` | 保留 | ExecutionLayer 调用现有 grader |
| `daemon/app.py` lifespan | 重构 | 22 个手动任务 → engine.start() |
| `security/` | 保留 | ExecutionLayer 调用现有 security 层 |
| `providers/channel/` | 保留 | 成为 UserInputSource 的一种 |
| `providers/tool/` | 保留 | 成为 ExecutionLayer 的工具后端 |
| `providers/llm/` | 保留 | 成为 ReasoningEngine 的 LLM 后端 |

---

## 4. 文件系统重构

### 4.1 新目录结构

```
xmclaw/
├── __init__.py
├── __main__.py
│
├── cognition/                      ← 新建：新架构核心
│   ├── __init__.py
│   ├── engine.py                   # CognitiveEngine（取代 AgentLoop）
│   ├── state.py                    # CognitiveState + AttentionFocus + Goal
│   ├── interfaces.py               # TurnOrchestrator Protocol
│   ├── engine_factory.py           # build_engine_from_config()
│   │
│   ├── perception/                 # 感知层
│   │   ├── __init__.py
│   │   ├── layer.py                # PerceptionLayer
│   │   ├── sources.py              # PerceptionSource ABC + 实现
│   │   └── file_watcher.py         # FileWatcherSource
│   │
│   ├── attention/                  # 注意力机制
│   │   ├── __init__.py
│   │   ├── module.py               # AttentionModule
│   │   └── window.py               # AttentionWindow
│   │
│   ├── memory/                     # 统一记忆系统
│   │   ├── __init__.py
│   │   ├── system.py               # UnifiedMemorySystem（取代 MemoryManager）
│   │   ├── models.py               # MemoryItem, MemoryQuery, Layer
│   │   ├── working.py              # WorkingMemoryStore
│   │   ├── short_term.py           # ShortTermStore
│   │   ├── long_term.py            # LongTermStore
│   │   ├── procedural.py           # ProceduralStore (skills)
│   │   ├── vec_index.py            # VectorIndex (sqlite-vec)
│   │   ├── graph_index.py          # GraphIndex (SQLite + NetworkX)
│   │   └── temporal_index.py       # TemporalIndex
│   │
│   ├── reasoning/                  # 推理引擎
│   │   ├── __init__.py
│   │   ├── engine.py               # ReasoningEngine
│   │   ├── chain_of_thought.py     # CoT
│   │   └── causal.py               # 因果推断
│   │
│   ├── planning/                   # 规划系统
│   │   ├── __init__.py
│   │   ├── system.py               # PlanningSystem
│   │   ├── goal_manager.py         # GoalManager
│   │   ├── scheduler.py            # TaskScheduler (DAG)
│   │   └── htn.py                  # HTN 规划器
│   │
│   ├── execution/                  # 执行层
│   │   ├── __init__.py
│   │   ├── layer.py                # ExecutionLayer
│   │   └── sandbox.py              # 安全沙箱
│   │
│   └── learning/                   # 学习系统（内置进化）
│       ├── __init__.py
│       ├── system.py               # LearningSystem
│       ├── experiment_loop.py      # 自主实验循环
│       ├── pattern_detector.py     # 模式检测
│       └── skill_distiller.py      # 技能提炼
│
├── legacy/                         ← 新建：旧代码归档
│   └── daemon/
│       └── agent_loop.py           # 原文件移动至此
│
├── core/                           # 保持现有（不动核心差异化模块）
│   ├── bus/
│   ├── evolution/                  # 保留：controller, mutator, constraints...
│   ├── grader/                     # 保留：HonestGrader
│   ├── scheduler/
│   │   └── cron.py                 # 保留：CronStore，CronTickTask 标记 deprecated
│   └── ...
│
├── providers/                      # 保持现有（成为 cognition 的后端）
│   ├── llm/
│   ├── tool/
│   ├── memory/                     # sqlite_vec.py 保留，manager.py 标记 deprecated
│   ├── channel/
│   └── runtime/
│
├── skills/                         # 保持现有
├── security/                       # 保持现有
├── daemon/                         # 精简
│   ├── app.py                      # 大幅简化：只留 FastAPI + router + lifespan 路由
│   ├── lifespan.py                 # 新建：统一 lifespan（新旧切换）
│   ├── engine_factory.py           # 新建：构建 CognitiveEngine
│   ├── routers/                    # 保持现有，但改为调 engine 而非 agent
│   └── ...
│
└── cli/                            # 保持现有
```

---

## 5. Phase-by-Phase 实施路线

### Phase 0: 基础设施（1 周）

**目标**：建立 `cognition/` 包，定义接口协议，新旧架构可并行。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 0.1 | 新建 `xmclaw/cognition/` 包结构 | 1d | 目录存在，import 不报错 |
| 0.2 | 定义 `TurnOrchestrator` Protocol | 0.5d | 现有代码可 `isinstance(engine, TurnOrchestrator)` |
| 0.3 | 将 `agent_loop.py` 移入 `legacy/` | 0.5d | 现有 import 路径不变（通过 `__init__.py` 转发） |
| 0.4 | 新建 `daemon/lifespan.py`，实现新旧切换 | 1d | config 开关可切换 lifespan |
| 0.5 | 单元测试框架 | 1d | `pytest tests/cognition/` 可运行 |
| 0.6 | CI 配置 | 1d | ruff + mypy 覆盖 cognition/ |

**交付物**：新旧架构可并行运行，配置切换生效。

---

### Phase 1: UnifiedMemorySystem（2 周）

**目标**：建立统一记忆系统，取代 MemoryManager。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 1.1 | 实现 `MemoryItem`, `MemoryQuery` models | 0.5d | dataclass 冻结正确 |
| 1.2 | 实现 `WorkingMemoryStore`（内存，7±2） | 1d | LRU + 容量限制 |
| 1.3 | 实现 `ShortTermStore`（SQLite） | 1d | TTL 自动过期 |
| 1.4 | 实现 `LongTermStore`（SQLite） | 1d | 持久化 |
| 1.5 | 实现 `VectorIndex`（sqlite-vec 封装） | 1.5d | KNN + hybrid RRF |
| 1.6 | 实现 `GraphIndex`（SQLite + NetworkX） | 2d | 遍历 + 路径查找 |
| 1.7 | 实现 `TemporalIndex`（SQLite） | 1d | 时间范围查询 |
| 1.8 | 实现 `UnifiedMemorySystem`（统一接口） | 2d | store/query/proactive_recall 全链路 |
| 1.9 | 对接现有 `sqlite_vec.py`（数据迁移） | 1d | 现有数据可读 |
| 1.10 | 测试 | 2d | 覆盖率 > 80% |

**交付物**：`UnifiedMemorySystem` 可独立运行，支持组合查询。

---

### Phase 2: PerceptionLayer + AttentionModule（1.5 周）

**目标**：建立感知层和注意力机制。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 2.1 | 实现 `PerceptionSource` ABC | 0.5d | 接口定义清晰 |
| 2.2 | 实现 `UserInputSource` | 1d | 可接收用户消息 |
| 2.3 | 实现 `FileWatcherSource`（watchdog） | 1.5d | 可监控文件变化 |
| 2.4 | 实现 `CronSource`（取代 CronTickTask） | 1d | 定时触发正确 |
| 2.5 | 实现 `AttentionModule` + `AttentionWindow` | 1.5d | 显著性计算正确 |
| 2.6 | 对接 `CognitiveEngine` | 1d | 感知事件流入引擎 |
| 2.7 | 测试 | 1d | 覆盖率 > 80% |

**交付物**：系统可感知文件变化，注意力筛选正确。

---

### Phase 3: CognitiveEngine 核心（2 周）

**目标**：实现认知循环 orchestrator，取代 AgentLoop。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 3.1 | 实现 `CognitiveState` | 1d | 状态可保存/加载 |
| 3.2 | 实现 `CognitiveEngine._cognitive_loop()` | 2d | 循环可运行，不阻塞 |
| 3.3 | 实现 `submit_user_turn()` | 1d | 用户消息进入循环，返回结果 |
| 3.4 | 集成 `PerceptionLayer` | 1d | 感知事件驱动循环 |
| 3.5 | 集成 `AttentionModule` | 1d | 显著性筛选生效 |
| 3.6 | 集成 `UnifiedMemorySystem` | 1.5d | 记忆查询/写入生效 |
| 3.7 | 实现 `ReasoningEngine`（LLM 调用） | 2d | 复用现有 LLM provider |
| 3.8 | 实现 `ExecutionLayer`（工具调用） | 2d | 复用现有 Tool provider |
| 3.9 | 对接 daemon lifespan | 1d | engine.start() 在 lifespan 中运行 |
| 3.10 | 测试 | 2d | 端到端 turn 可完成 |

**交付物**：`CognitiveEngine` 可完成一轮用户对话。

---

### Phase 4: PlanningSystem + TaskScheduler（1.5 周）

**目标**：实现 HTN 规划和任务 DAG。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 4.1 | 实现 `GoalManager` | 1d | 目标可创建/删除/优先级排序 |
| 4.2 | 实现 `Task` model + 状态机 | 0.5d | PENDING→RUNNING→COMPLETED |
| 4.3 | 实现 `TaskScheduler` | 2d | DAG 依赖 + 优先级 + 重试 |
| 4.4 | 实现 `PlanningSystem`（HTN） | 2d | 目标分解为任务 DAG |
| 4.5 | 对接 `ExecutionLayer` | 1d | 计划可执行 |
| 4.6 | 测试 | 1d | 覆盖率 > 80% |

**交付物**：系统可分解目标为任务 DAG 并执行。

---

### Phase 5: LearningSystem（内置进化）（1.5 周）

**目标**：将进化系统从外挂改为内置。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 5.1 | 实现 `PatternDetector` | 1.5d | 可检测重复操作模式 |
| 5.2 | 实现 `ExperimentLoop` | 1.5d | 观察→假设→实验→验证 |
| 5.3 | 实现 `SkillDistiller` | 1d | 模式 → skill 提案 |
| 5.4 | 对接现有 `EvolutionController` | 1d | 实验成功走 promotion gate |
| 5.5 | 对接 `SkillRegistry` | 1d | skill 可注册/版本化 |
| 5.6 | 测试 | 1d | 覆盖率 > 80% |

**交付物**：系统可自主实验并生成 skill 提案。

---

### Phase 6: 整合与切换（1 周）

**目标**：新旧架构可配置切换，现有测试通过。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 6.1 | `TurnOrchestrator` 兼容层完善 | 1d | 现有 router 可调新引擎 |
| 6.2 | 配置切换逻辑 | 1d | `config.cognitive_engine.enabled` 生效 |
| 6.3 | 现有测试保持通过 | 2d | `pytest tests/` 全绿 |
| 6.4 | 端到端测试 | 1d | 新架构可完成完整对话 |
| 6.5 | 性能基准 | 1d | 新架构延迟不超过旧架构 20% |

**交付物**：用户可通过配置开关启用新架构。

---

### Phase 7: 全面迁移 + 旧代码清理（1 周）

**目标**：默认启用新架构，删除旧代码。

| # | 任务 | 工作量 | 验收标准 |
|---|------|--------|----------|
| 7.1 | 默认启用新架构 | 0.5d | `cognitive_engine.enabled` 默认 true |
| 7.2 | 迁移所有 router | 2d | 所有 API 走新引擎 |
| 7.3 | 删除 `legacy/` 代码 | 1d | 代码清理 |
| 7.4 | 删除 `agent_loop.py` | 0.5d | 确认无引用 |
| 7.5 | 文档更新 | 1d | ARCHITECTURE.md 更新 |
| 7.6 | 最终测试 | 1d | 全量测试通过 |

**交付物**：新架构成为唯一运行时。

---

## 6. 总时间线

```
Week 1:  Phase 0  (基础设施)
Week 2:  Phase 1  (统一记忆系统)
Week 3:  Phase 1  (统一记忆系统，续)
Week 4:  Phase 2  (感知 + 注意力)
Week 5:  Phase 3  (认知引擎核心)
Week 6:  Phase 3  (认知引擎核心，续)
Week 7:  Phase 4  (规划 + 任务 DAG)
Week 8:  Phase 5  (内置进化)
Week 9:  Phase 6  (整合切换)
Week 10: Phase 7  (全面迁移)

总计：10 周
```

---

## 7. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 重构范围过大，无法交付 | 中 | 极高 | 每 Phase 可独立回滚；配置开关默认旧架构；10 周分 7 个 Phase |
| CognitiveEngine 性能不如 AgentLoop | 中 | 高 | Phase 6 设性能基准；管道开销用缓存抵消；内部调用不走事件总线 |
| 现有测试在迁移中大量断裂 | 高 | 高 | TurnOrchestrator Protocol 保证接口兼容；新旧并行期间所有测试保持通过 |
| UnifiedMemorySystem 数据一致性 bug | 中 | 高 | 统一 ID 系统；写入事务；定期一致性检查；回滚机制 |
| 注意力机制误过滤重要事件 | 中 | 中 | 显著性阈值可调；用户可手动提升事件优先级；审计日志 |
| 自主实验产生破坏性行为 | 低 | 极高 | 实验在隔离 worktree 中运行；危险操作仍需审批；人工 gate |

---

## 8. 成功标准

| 标准 | 验收方式 |
|------|----------|
| 新架构可完成所有现有功能 | 现有测试全绿 |
| 认知循环可自主运行 24h | 长时间运行测试 |
| 用户可感知"系统更懂我" | 主动回忆准确率 > 70% |
| 任务完成效率提升 50%+ | A/B 测试（新旧架构对比） |
| 新 skill 生成周期缩短 70% | 对比实验 |
| 架构可扩展性：新增认知阶段 < 1 天 | 工程验证 |

---

## 9. 立即执行的第一步

**今天就可以做的事**（1 小时）：

```bash
# 1. 新建 cognition 包
mkdir -p xmclaw/cognition/{perception,attention,memory,reasoning,planning,execution,learning}
touch xmclaw/cognition/__init__.py

# 2. 新建 legacy 归档
mkdir -p xmclaw/legacy/daemon

# 3. 移动 agent_loop.py（保留转发）
cp xmclaw/daemon/agent_loop.py xmclaw/legacy/daemon/agent_loop.py
# 编辑 xmclaw/daemon/agent_loop.py，顶部加 deprecation warning

# 4. 新建接口协议
cat > xmclaw/cognition/interfaces.py << 'EOF'
from typing import Protocol, runtime_checkable

@runtime_checkable
class TurnOrchestrator(Protocol):
    async def run_turn(self, session_id: str, message: str, **kwargs): ...
    async def cancel_session(self, session_id: str) -> None: ...
    async def pop_last_turn(self, session_id: str) -> None: ...
EOF

# 5. 提交
git add -A
git commit -m "Phase 0: Bootstrap cognition/ package + TurnOrchestrator Protocol"
```

---

**End of doc.**

> **核心决策回顾**：
> 1. AgentLoop 拆分为认知管道（CognitiveEngine）
> 2. MemoryManager 升级为 UnifiedMemorySystem（功能分层 × 维度索引）
> 3. 22 个手动 lifespan 任务 → engine.start()
> 4. 进化系统从外挂改为内置（LearningSystem）
> 5. 事件总线从 observability 层升级为通信层
> 6. 新旧架构并行建设，配置切换，安全迁移
