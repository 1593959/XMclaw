# XMclaw → 贾维斯：代码级重构实施计划（v2）

> **文档性质**: 可执行开发文档（living document）  
> **基线版本**: v1.0.0  
> **目标版本**: v1.1.0 "Jarvis"  
> **日期**: 2026-05-20  
> **调研深度**: 4路并行审计（Plugin/Backup/Eval + TUI/CLI/Scripts + Static Frontend + Runtime/Channels/MCP）+ 全量代码走查  
> **原则**: 每次 Phase 独立可交付，内部模块高内聚、跨模块通过 EventBus 松耦合

---

## 执行摘要

本次深度调研发现 **XMclaw v1.0.0 的代码成熟度远超预期**：大量 Phase 6 贾维斯模块已完整实现（ReasoningEngine、SelfExperimentLoop、HTNPlanner、MCP Hub、Docker Runtime、MagicDocs、Speculation、Metacognition Pipeline），但存在系统性 **"代码存在 ↔ 运行时未接线"** 的断层。核心问题不是"缺代码"，而是"缺接线"。

**三大关键发现：**

1. **默认全 OFF 问题**：`evolution.enabled=false`, `memory_v2.enabled=false`, `continuous_loop.enabled=false`, `autonomy_level=0`。Fresh install = 普通聊天机器人，无贾维斯体验。
2. **感知层 greenfield**：PerceptionBus、AttentionFilter、Planner、ActionDispatcher 代码完整但 daemon 未消费（模块注释明确标注 "Nothing in the daemon imports it yet" / "wiring follow-up"）。
3. **前端死代码**：VAD 模块（`lib/vad.js`）完整实现但从未被导入；移动端侧边栏 CSS 类名不匹配；5 个页面有路由但无侧边栏导航。

**修正后的实施策略**：从"补代码"转向"接线路 + 清残留 + 开开关"。

---

## 0. 架构总览与模块状态真相

### 0.1 当前架构拓扑（数据流视角）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户层 (Clients)                                │
│  Web UI ──WS──┬── CLI ──RPC──┬── Feishu/TG/Slack ──Adapter──┬── Voice      │
│  (Preact+htm) │  (Textual)   │  (ChannelDispatcher)          │  (STT+TTS)   │
│  22 pages     │  `xmclaw chat`│  Email / Cron                │  VAD=dead    │
└───────────────┴──────────────┴───────────────────────────────┴──────────────┘
        │              │                   │
        └──────────────┴───────────────────┘
                           │
            ┌──────────────▼──────────────┐
            │   FastAPI Daemon (app.py)   │
            │   /agent/v2/{sid}  WS endpoint│
            └──────────────┬──────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼──────┐  ┌────────▼────────┐  ┌─────▼──────────┐
│  AgentLoop   │  │ CognitiveDaemon │  │ ProactiveAgent │
│  (per session)│  │   (1Hz heartbeat)│  │  (30s tick)    │
│              │  │                  │  │                │
│ • run_turn() │  │ ◄─ 理论上应消费  │  │ • Trigger reg  │
│ • compress   │  │   PerceptionBus  │  │ • Cooldown     │
│ • memory inj │  │   AttentionFilter│  │ • Proposal pub │
│ • grader     │  │   ReasoningEngine│  │                │
└───────┬──────┘  │   Planner        │  └────────────────┘
        │         │   ActionDispatcher│         │
        │         │   (实际均未接线)  │         │
        │         └──────────────────┘         │
        │                                      │
┌───────▼──────┐  ┌─────────────┐  ┌─────────▼──────────┐
│  LLMProvider │  │ ToolProvider│  │   EventBus         │
│ (anthropic   │  │(builtin/    │  │ (InProcess + SQLite│
│  /openai/    │  │ browser/mcp/│  │  + FTS5 + WAL)     │
│  minimax)    │  │ composite)  │  │                    │
└──────────────┘  └─────────────┘  └────────────────────┘
        │              │                  │
        │    ┌─────────┼──────────┬───────┘
        │    │         │          │
┌───────▼────▼──┐ ┌───▼────┐ ┌──▼───┐ ┌──────────────┐
│  MemoryMgr    │ │Security│ │Skills│ │ Evolution    │
│(sqlite-vec    │ │(prompt │ │Regis-│ │ Pipeline     │
│ /LanceDB v2)  │ │ scan)  │ │try   │ │(Controller + │
│ KeyInfoExtr.  │ │        │ │      │ │  Orchestrator)│
└───────────────┘ └────────┘ └──────┘ └──────────────┘
```

### 0.2 模块成熟度矩阵（经审计后的真实状态）

| 模块 | 状态 | 代码完成度 | 运行时接线 | 测试覆盖 | 关键缺口 |
|------|------|-----------|-----------|---------|---------|
| `AgentLoop` | **生产级** | 100% | ✅ 完整 | ✅ 高 | — |
| `EventBus` | **生产级** | 100% | ✅ 完整 | ✅ 高 | — |
| `LLMProvider` | **生产级** | 100% | ✅ 完整 | ✅ 中 | — |
| `ToolProvider` (builtin) | **生产级** | 100% | ✅ 完整 | ✅ 高 | — |
| `MemoryManager` (v1) | **生产级** | 100% | ✅ 完整 | ✅ 高 | — |
| `MemoryManager` (v2) | **功能完整** | 100% | ⚠️ 默认关闭 | ✅ 高 | `enabled=false` |
| `KeyInfoExtractor` | **功能完整** | 100% | ⚠️ v2关闭时不运行 | ✅ 中 | — |
| `ContextCompressor` | **功能完整** | 100% | ✅ 完整 | ✅ 高 | `ContextEngine` 未使用 |
| `EvolutionPipeline` | **功能完整** | 100% | ⚠️ 默认关闭 | ✅ 中 | `auto_apply=false` |
| `HonestGrader` | **功能完整** | 100% | ✅ 完整 | ✅ 中 | — |
| `SkillRegistry` | **功能完整** | 100% | ✅ 完整 | ✅ 中 | **无并发锁** |
| `ProactiveAgent` | **功能完整** | 100% | ✅ 完整 | ✅ 中 | — |
| `FileWatcher` | **功能完整** | 100% | ✅ 已接线 | ✅ 中 | 5秒轮询 |
| `ProcessWatcher` | **功能完整** | 100% | ✅ 已接线 | ✅ 低 | — |
| `CognitiveDaemon` | **消费端完整** | 100% | ⚠️ 感知生产者未接线 | ✅ 低 | 见 0.3 |
| `PerceptionBus` | **完整但悬空** | 100% | ❌ 无消费者 | ✅ 低 | "nothing imports it" |
| `AttentionFilter` | **greenfield** | 100% | ❌ 未接线 | ✅ 低 | "greenfield" |
| `ReasoningEngine` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | 4种推理模式全实现 |
| `Planner` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | "Nothing imports it" |
| `HTNPlanner` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | LLM分解+DAG执行 |
| `ActionDispatcher` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | "wiring follow-up B" |
| `TaskScheduler` | **功能完整** | 100% | ⚠️ 1秒轮询 | ✅ 中 | `_wake_scheduler` 无操作 |
| `SelfExperimentLoop` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | A/B+Welch t-test 全实现 |
| `MagicDocs` | **完整但悬空** | 100% | ⚠️ 未验证 | ✅ 低 | Wave 32+ 新功能 |
| `Speculation` | **完整但悬空** | 100% | ⚠️ 未验证 | ✅ 低 | Wave 32+ 新功能 |
| `Metacognition` | **完整但悬空** | 100% | ❌ 未接线 | ✅ 低 | trace/pass/reformer |
| `ComputerUseTools` | **功能完整** | 100% | ⚠️ 默认关闭 | ✅ 低 | 3层安全 |
| `MCP Hub` | **功能完整** | 100% | ✅ stdio | ✅ 中 | SSE/WS/streamableHttp 跳过 |
| `Docker Runtime` | **功能完整** | 100% | ⚠️ 需docker daemon | ✅ 低 | 技能源码未挂载 |
| `Plugin SDK` | **重导出层** | 100% | ✅ 稳定接口 | ✅ 中 | 无动态加载 |
| `Backup` | **功能完整** | 100% | ⚠️ auto_daily=false | ✅ 高 | — |
| `Eval Harness` | **框架完整** | 100% | ⚠️ 未跑过真实benchmark | ✅ 中 | Tier-2 graders未接线 |
| `ChannelDispatcher` | **功能完整** | 100% | ✅ 完整 | ✅ 中 | Email附件跳过 |
| `Daemon Routers` (29个) | **生产级** | 100% | ✅ 完整 | ✅ 高 | 零stub，零NotImplementedError |
| `Frontend` (Web) | **功能完整** | 100% | ✅ 完整 | ❌ 无 | VAD死代码/CSS问题 |
| `Frontend` (TUI) | **生产级** | 100% | ✅ 完整 | ❌ 无 | Textual实现完整 |
| `IntentEngine` | **规则层完整** | 80% | ⚠️ 规则层工作 | ✅ 低 | `_run_llm_layer` = TODO stub |

### 0.3 感知层状态（2026-05-22 更新）

`CognitiveDaemon` 在 `app_lifespan.py` 中已启动（心跳 1Hz），其内部 `tick_once()` 每 tick 运行完整管道：

```
PerceptionBus.drain() → AttentionFilter.tick() → ReasoningEngine.reason()
                                              → Planner.plan()
                                              → ActionDispatcher.execute_plan()
```

**接线状态（经代码审计核实）：**

| 环节 | 状态 | 说明 |
|------|------|------|
| **PerceptionBus** | ✅ 已接线 | `app_lifespan.py` 构造并传入 CognitiveDaemon + AttentionFilter |
| **AttentionFilter** | ✅ 已接线 | `app_lifespan.py` 构造，`tick()` 被 CognitiveDaemon 每 tick 调用 |
| **ReasoningEngine** | ✅ 已接线 | `app_lifespan.py` 构造并传入 CognitiveDaemon |
| **Planner** | ✅ 已接线 | `app_lifespan.py` 构造并传入 CognitiveDaemon |
| **ActionDispatcher** | ✅ 已接线 | `app_lifespan.py` 内联构造并传入 CognitiveDaemon |
| **PerceptSources** | ✅ 已接线 | FileWatcher、ProcessWatcher、WS、Cron 等已 attach |
| **GoalGenerator** | ✅ **本次修复** | 此前未构造（`self._goal_generator is None` 导致 `_spawn_goals()` 短路）。现已接线 |

**结论**：感知层核心管道 **已完整接线**，CognitiveDaemon 每 tick 确实在消费 PerceptionBus、运行推理-规划-执行循环。此前的"悬空"判断是过时的（基于早期代码版本）。唯一缺口 GoalGenerator 已在本次修复中关闭。

### 0.4 关键联动约束（红线）

| 联动关系 | 约束 | 违反后果 |
|----------|------|----------|
| `core/` → `providers/` | **禁止**。core 不得 import providers | 破坏 DAG，循环依赖 |
| `core/` → `skills/` | **禁止**。core 不得 import skills | 进化系统无法独立运行 |
| `utils/` → 其他 `xmclaw.*` | **禁止**。utils 是纯工具层 | 工具层膨胀 |
| `AgentLoop` → `EventBus` | **单向发布**。AgentLoop 只发事件，不直接调用订阅者 | 紧耦合，难以测试 |
| `CognitiveDaemon` → `PerceptionBus` | **生产者-消费者**。daemon 消费，watcher 生产 | 如果 bus 未接线，daemon 空转 |
| `ContextCompressor` → `Message` | **只读**。compressor 不修改原始 Message | 数据完整性破坏 |
| `SkillRegistry` → 并发 | **必须加锁**。`threading.RLock` 保护所有读写操作（`run_in_executor` 多线程场景） | 竞态条件，数据损坏 |

---

## 1. 核心流程逻辑详图

> 本节基于代码级走查（`agent_loop.py` 2099 行、`hop_loop.py` 1458 行、`app.py` 2385 行、`app_lifespan.py` 3037 行、`grader/verdict.py` 447 行、以及 20+ 支撑模块）和 4 路并行审计代理的输出，精确描述每个核心模块的处理逻辑。

---

### 1.1 AgentLoop.run_turn() — 单次对话回合的完整生命周期

**文件**: `xmclaw/daemon/agent_loop.py:683-2099`  
**调用方**: `app.py` WS handler / CronTickTask / TUI / CLI

```
run_turn(session_id, user_message)
│
├─ 0. CancelEvent 注册（用户点击 Stop 时触发）
├─ 1. Hook: USER_PROMPT_SUBMIT（可阻断/改写用户消息）
├─ 2. publish(USER_MESSAGE) ──► EventBus
│
├─ 【Prep Phase】（全部计入 turn_prep_timing）
│   ├─ ProactiveAgent.note_user_message() ── 更新"用户最后说话时间"
│   ├─ AutobioMemory.extract_from_message() ── 正则提取"我是X"/"我在做Y"
│   ├─ Memory v2: regex_extract (bg task) ── KeyInfoExtractor 强制写入 L1
│   ├─ Memory v2: llm_extract (bg task) ── LLMFactExtractor 语义提取
│   ├─ PerceptionBus.push(user_msg_percept) ── 推送感知（如 wired）
│   ├─ CognitiveState.compute_salience() (bg task) ── 计算注意力分数
│   ├─ _maybe_apply_llm_compression() ── 应用挂起的 LLM 压缩（8s cap）
│   ├─ Memory recall ── 四层召回管道：
│   │   ├─ v1 prefetch ── MemoryManager.prefetch() 预取块
│   │   ├─ v1 query ── sqlite-vec 混合检索（embed 2s timeout → fallback LIKE）
│   │   ├─ v2 render_for_prompt() ── LanceDB 结构化事实 + CONTRADICTS/SUPERSEDES
│   │   ├─ Graph proactive_recall ── 关系图谱相关记忆
│   │   └─ UnifiedMemory query ── 多轴（语义+关系+时间）召回
│   ├─ Relevant files picker ── LLM 挑选相关笔记文件（可选，默认关）
│   ├─ Curriculum hint ── 检测到挫败感时注入 propose_curriculum_edit 提示
│   ├─ StrategyBank retrieve ── 检索历史有效策略
│   └─ publish(INNER_MONOLOGUE, turn_prep_timing)
│
├─ 【System Prompt 组装】
│   ├─ Frozen base prompt（按 generation 缓存，避免重复渲染）
│   ├─ Output style prompt（如启用）
│   ├─ Autobio snapshot（默认 5 facts，或主动召回模式提示）
│   ├─ Recent autonomous tasks block
│   ├─ Time block（当前时刻，per-turn mutable）
│   ├─ Git status snapshot（如 workspace 是 git repo）
│   └─ CACHE_BREAKPOINT_MARKER 分隔（Anthropic prompt cache 优化）
│
├─ 【Tool Spec 准备】
│   ├─ list_tools() ── 获取全部工具
│   ├─ Skill prefilter ── 按用户消息筛选 top-12 相关技能（>30 skills 时触发）
│   ├─ Conditional skill activation ── 按最近文件路径 boost 匹配技能
│   └─ skill_browse hint ── 当无技能匹配时注入提示
│
├─ 【Message 构建】
│   ├─ system: 组装后的 system prompt
│   ├─ prior history（跨 turn 持久化到 SessionStore）
│   └─ user: user_message + memory_ctx_block + memory_files_block
│            + unified_recall_block + curriculum_hint_block
│            + curriculum_strategies_block + skill_browse_hint + images
│
├─ 【Mode Router】（2026-05-12 Batch D）
│   └─ 选择 run_mode: instant / thinking / agent / swarm
│
├─ 【Plan-First】（非 instant 模式）
│   └─ PlanFirstGate.is_complex() → plan() ── 复杂查询预分解（15s cap）
│
└─ 【Hop Loop】调用 _run_hop_loop() ──► 详见 1.2
```

**关键设计决策**:
- **Prep Phase 全部是非阻塞或 bg-task**：regex/LLM 提取、salience 计算、compression 都是后台任务，不阻塞用户首 token
- **Memory 召回四层管道**：v1 prefetch → v1 query → v2 facts → graph recall → unified recall，每层都是 best-effort
- **Prompt cache 优化**：frozen base + autobio 在 CACHE_BREAKPOINT_MARKER 之前，time block 之后， Anthropic cache 命中率最大化
- **Tool spec 预过滤**：404 skills 时 tool schema 达 80K tokens，预过滤后降至 ~12 skills，防止 LLM 信号噪声比归零

---

### 1.2 HopLoop — LLM ↔ 工具的多跳循环

**文件**: `xmclaw/daemon/hop_loop.py:212-1458`  
**循环变量**: `for hop in range(self._max_hops)`（默认 `max_hops=5`，可配置）

```
_run_hop_loop()
│
for hop in 0..max_hops-1:
│
├─ 1. Cancel check ── 用户点击 Stop？→ 返回 cancelled
├─ 2. Budget check ── CostTracker.check_budget() ── 超预算 → 返回 budget_exceeded
├─ 3. GoalAnchor injection ── 每 N hops（默认5）或 turn 2+ 的 hop=0
│   └─ 注入合成提醒：原始目标 + 已调用工具 + 剩余 budget + 未完成 plan steps
├─ 4. publish(LLM_REQUEST) ── 带 messages_hash fingerprint
│
├─ 【LLM Streaming】
│   ├─ llm.complete_streaming(messages, tools, on_chunk, on_thinking_chunk,
│   │                         on_tool_block, cancel)
│   ├─ on_chunk: publish(LLM_CHUNK) ──► WS → UI token-by-token
│   ├─ on_thinking_chunk: publish(LLM_THINKING_CHUNK) ──► UI PhaseCard
│   └─ on_tool_block: SpeculationCache 预启动 READ_ONLY_TOOLS（file_read/grep等）
│
├─ 【B-227 Classify-and-Retry】
│   └─ LLM 调用失败时：error_classifier 分类 → 按 reason 退避重试
│      ├─ rate_limit / overloaded → 指数退避重试（最多3次）
│      ├─ context_overflow → 强制压缩后重试
│      └─ 非重试错误 →  surfaced 为 LLM_RESPONSE error
│
├─ 【B-230 Auto-Continue】
│   └─ stop_reason=max_tokens / length 且 content>50 chars
│      → 追加 assistant content + "[B-230 auto-continue]" prompt，重新调用 LLM
│      → 最多 3 次 continue
│
├─ 5. publish(LLM_RESPONSE) ── 最终 assistant message（含 tool_calls 或纯文本）
│
├─ 【If 有 tool_calls】
│   ├─ for each ToolCall in parallel（受 max_concurrent_tools 限制）:
│   │   ├─ publish(TOOL_CALL_EMITTED)
│   │   ├─ publish(TOOL_INVOCATION_STARTED)
│   │   ├─ _invoke_single_tool()
│   │   │   ├─ PreToolUse hook（可 deny 或改写 args）
│   │   │   ├─ asyncio.wait_for(effective_tools.invoke(), 180s)
│   │   │   │   ├─ Timeout → 结构化 failed ToolResult
│   │   │   │   └─ Exception → 结构化 failed ToolResult
│   │   │   ├─ B-17: 瞬态失败重试一次（0.5s delay）
│   │   │   └─ PostToolUse hook（可 redact result）
│   │   ├─ publish(TOOL_INVOCATION_FINISHED)
│   │   ├─ Anti-loop guard ── 检查 stuck_loop_deque（3次相同失败=退出）
│   │   ├─ StepValidator ── 验证此步是否推进目标（可选，默认关）
│   │   ├─ HonestGrader.grade() ── 双信号评分
│   │   ├─ publish(GRADER_VERDICT)
│   │   ├─ PromptInjectionScanner.scan(result) ── SOURCE_TOOL_RESULT
│   │   ├─ Guardian 审批（如启用）
│   │   └─ 结果回注 messages ── 作为 tool/assistant message 追加
│   ├─ Anti-progress guard ── 5 hops 无成功工具调用 → 提示用户
│   └─ continue next hop
│
└─ 【If 无 tool_calls（terminal）】
   ├─ Anti-loop guard: 检查重复 assistant content
   ├─ _persist_history() ── SessionStore.save() + 内存更新
   ├─ MemoryManager.sync_turn() ── 本 turn 的记忆同步
   ├─ Post-sampling hooks（bg）
   ├─ Unified memory auto-put（bg）
   ├─ publish(TURN_FINISHED)
   └─ return AgentTurnResult(ok=True)
```

**关键安全/防呆机制**:
| 机制 | 位置 | 行为 |
|------|------|------|
| **CancelEvent** | hop boundary | 用户点击 Stop 后，当前 hop 完成即退出，不中断 in-flight stream |
| **Budget cap** | hop boundary | 每 hop 前检查 CostTracker，超预算立即退出 |
| **Tool timeout** | `_invoke_single_tool()` | 180s wall-clock，可配置。超时返回结构化错误 |
| **B-227 Retry** | LLM 调用 | 按错误类型分类退避重试，context_overflow 时自动压缩 |
| **B-230 Auto-continue** | LLM 响应 | max_tokens 截断时自动续写，最多3次 |
| **Stuck loop guard** | 工具调用后 | 3次相同(tool_name, error_signature) → 合成"stuck in a loop"退出 |
| **No-progress guard** | 5 hops | 无成功工具调用 → 提示用户任务可能过复杂 |
| **Prompt injection scan** | 工具结果 | SOURCE_TOOL_RESULT 扫描，blocked 则跳过该结果 |
| **GoalAnchor** | 每 N hops | 长工具链防漂移，注入目标提醒 |
| **Speculation** | streaming | 预执行 READ_ONLY_TOOLS，缓存供 Phase B 命中 |

---

### 1.3 EventBus — 事件驱动的神经系统

**架构**: `InProcessEventBus`（内存 pub/sub）+ `SqliteEventBus`（SQLite WAL + FTS5 持久化）

**文件**: `xmclaw/core/bus/memory.py`, `xmclaw/core/bus/sqlite.py`, `xmclaw/core/bus/events.py`

**InProcessEventBus 处理逻辑**:
```python
class InProcessEventBus:
    def publish(event: BehavioralEvent):
        for sub in self._subs:
            if not sub.predicate(event): continue
            task = asyncio.create_task(sub.handler(event))  # 并行 fan-out
```
- **Best-effort 交付**：handler 异常被捕获，不传播给 publisher
- **并行 fan-out**：每个 handler 是独立的 asyncio.Task
- **Predicate-based 过滤**：subscriber 通过 predicate 函数选择感兴趣的事件

**SqliteEventBus 持久化逻辑**:
- 每个事件先 `INSERT INTO events`（WAL 模式，synchronous=NORMAL）
- 触发器自动同步 `events_fts`（FTS5 全文索引）和 `sessions` 表
- Subscriber 只读取已持久化的事件 → crash 时不会丢失已发布的 event

**核心事件类型（~35种）**:

| 事件类型 | 发布者 | 订阅者 | 频率 |
|---------|--------|--------|------|
| `USER_MESSAGE` | AgentLoop | WS forwarder, 日志, 分析 | 每 turn |
| `LLM_REQUEST` | HopLoop | 分析, 成本追踪 | 每 hop |
| `LLM_CHUNK` / `LLM_THINKING_CHUNK` | HopLoop | WS forwarder → UI | 每 token |
| `LLM_RESPONSE` | HopLoop | WS forwarder, Grader | 每 hop |
| `TOOL_CALL_EMITTED` | HopLoop | 日志, 分析 | 每 tool call |
| `TOOL_INVOCATION_STARTED` / `FINISHED` | HopLoop | Grader, 日志, 分析 | 每 tool call |
| `GRADER_VERDICT` | HopLoop | EvolutionOrchestrator, 日志 | 每 tool call |
| `SKILL_CANDIDATE_PROPOSED` / `PROMOTED` / `ROLLED_BACK` | Evolution | WS forwarder（全局广播） |  sporadic |
| `PROACTIVE_PROPOSAL` | ProactiveAgent | WS forwarder（全局广播） | ~每 30s |
| `COGNITIVE_DAEMON_TICK` | CognitiveDaemon | WS forwarder | ~1 Hz |
| `MEMORY_RECALL` / `MEMORY_OP` | MemoryManager | WS forwarder（Trace page） | 每 turn/op |
| `CONTEXT_COMPRESSED` | HopLoop | WS forwarder | 触发时 |
| `SESSION_LIFECYCLE` | WS handler | 日志, 分析 | 每 session |
| `CONFIG_RELOADED` | app_lifespan | WS forwarder | 配置变更时 |
| `ANTI_REQ_VIOLATION` | AgentLoop/HopLoop | WS forwarder, 日志 | 违规时 |

**WS 事件转发逻辑** (`app.py:1942-1973`):
- 每个 WS 连接创建一个 subscriber，predicate = `event.session_id == 当前session_id`
- 全局事件（SKILL_PROMOTED, PROACTIVE_PROPOSAL 等）额外放行
- 订阅者收到事件后 `ws.send_text(json.dumps(event))`
- 会话历史事件在连接时 replay（`replayed: true` 标记）

---

### 1.4 Memory 系统 — 单栈 V2（LanceDB）

> **Phase 7 (2026-05-23/24)**: V1 双栈架构已退役。原 V1 `UnifiedMemorySystem` +
> sqlite-vec 用户 fact 存储被物理删除（commit 70e0ee6）；用户态 facts
> 全部走 V2 `MemoryService` + LanceDB。
> `xmclaw/providers/memory/sqlite_vec.py` 保留但 **不再用于用户 fact**
> ——它现在只服务于 workspace 索引（`MemoryFileIndexer` 写 file_chunk /
> code_chunk）。

#### 1.4.1 workspace 索引（sqlite-vec，非用户 fact）

**文件**: `xmclaw/daemon/memory_indexer.py` + `xmclaw/providers/memory/sqlite_vec.py`

工作区文件索引（MEMORY.md / USER.md / 用户配置的 workspace 路径）经
`MemoryFileIndexer` 切块 + 嵌入 + 写到 sqlite-vec。`memory_search` 工具
查 file_chunk / code_chunk 时走这条路。这与下文的"用户态 facts"是
两个 orthogonal 的概念 —— file_chunk 是大量内容片段（千 KB 级），
fact 是少量原子知识单元（句子级）。

#### 1.4.2 用户态 facts（V2 LanceDB）

**文件**: `xmclaw/memory/v2/service.py`

**核心数据模型**:
- `Fact`: id, text, kind(identity/preference/goal/lesson/...), scope(user/project/session), layer(working/long_term), embedding
- `Relation`: source_id, target_id, kind(SAME_TOPIC/CONTRADICTS/SUPERSEDES/CAUSED_BY), strength
- `Entity`: 桥接提到同一现实对象的 facts

**写入流程** (`remember()`）:
1. 计算 deterministic id: `kind:scope:hash12(text)`
2. `EmbeddingService.embed()` → 带 LRU cache(1024) + 3次重试 + 指数退避
3. **矛盾检测**: KNN 搜索 top-3 same-kind facts，distance<0.25 → 标记 CONTRADICTS
4. `upsert` 到 VectorBackend（`merge_insert`，idempotent）
5. 如有 source_event_id → 添加 CAUSED_BY 边指向 `event:<id>`
6. `evidence_count >= 3` → 自动晋升 working → long_term

**自动提取管道**（AgentLoop.run_turn 中触发）:
```
用户消息
├── Layer 1: KeyInfoExtractor（regex，同步/后台）
│   ├── URL 模式 → kind=url
│   ├── 账号/密码模式 → kind=credential
│   ├── 数字目标 → kind=goal
│   └── explicit-remember → kind=preference
│   └── 立即 remember() + render_persona_after_writes()
│
└── Layer 2: LLMFactExtractor（LLM，纯后台）
    ├── 语义身份提取（"做电商" → industry）
    ├── 隐含事实（"月底前" → deadline）
    ├── 领域知识
    └── 软偏好
    └── remember() + render_persona_after_writes()
```

**召回流程** (`render_for_prompt()`):
1. KNN 搜索当前 user_message 的语义邻居（k=8）
2. 对每条 hit，fetch 1-hop neighbors（CONTRADICTS + SUPERSEDES）
3. 渲染为结构化块：USER 档案 / PROJECT 档案 / DECISIONS / 带矛盾标记的 facts

#### 1.4.3 Autobiographical Memory：结构化用户档案

**文件**: `xmclaw/cognition/autobiographical_memory.py`

- SQL 表：people, projects, typed_facts（身份/偏好/目标/决策）
- 每 turn 渲染为 markdown snapshot（默认 max_facts=5）
- 注入 system prompt（frozen base 和 autobio 之间，cache-friendly）

#### 1.4.4 Phase M — 记忆 UI/可观测重构

> **状态**: 🟡 进行中 (2026-06-05 起)。围绕成熟的 v2 核心重构，不推倒重写。
> 计划见 plan 文件 buzzing-twirling-blossom。

- [x] **M1** 前端记忆页结构重构：7 标签→3 段式分组（我的记忆/Agent 记忆/调试）；
      L1 事实维护按钮收进 `⚙ 维护` 抽屉；事实按 bucket 分组折叠 + 矛盾/失效置顶
      高亮（`groupFacts`）。文件：`pages/Memory.js`、`pages/_panels/memory_facts_v2.js`、
      `styles/instrument.css`。
- [x] **M1.2** FactRow 仪表化：kind/scope/layer 做成 chip、conf 迷你条、证据
      ×N、过期/矛盾高亮；**改正改为行内编辑**（textarea + Ctrl/⌘+Enter 保存，
      替掉 window.prompt）。文件：`pages/_panels/memory_facts_v2.js` + `instrument.css`。
- [x] **M2** 后端 `/api/v2/memory/v2/overview` 聚合接口（总数 + by_kind/scope/
      layer/bucket + 矛盾/过期/forgotten/superseded + recent10 + embedder），
      前端 refreshStatus 附带拉取并入 status，读数条多显示「矛盾 / 已过期」。
      文件：`routers/memory_v2.py`、`pages/_panels/memory_facts_v2.js`、
      `tests/integration/test_v2_memory_v2_router.py`（新增 overview 测试）。
- [x] **M3** 召回卡「💭 想起 N 条」（写/读对称）。后端早已发 `MEMORY_RECALL`
      事件（agent_loop:2147，带 hits=[{id,text,kind,scope,layer,distance}]），
      只缺前端：reducer 新增 memory_recall case 把 hits 挂到当前 user 消息，
      MessageList 渲染 RecallMemo（可展开）。**附带修复**：记忆写入卡 MemoryMemo
      原在没人 import 的 MessageBubble.js（死代码），当前 nebula UI 根本没显示
      —— 一并接回 MessageList（assistant 消息下渲染 memoryMemos）。文件：
      `lib/chat_reducer_secondary.js`、`components/molecules/MessageList.js`、
      `styles/instrument.css`。
- [x] **M-fix1** 记忆污染根因：内部反思会话（goal-from-percept-/reflect:/
      autonomous:/_system: …）不再抽取 lesson/preference 写入用户记忆。反思
      每轮重跑、自言自语反复入库是「记忆越积越脏」的根因。文件：
      `xmclaw/daemon/post_sampling_hooks.py`（两个抽取 hook 的 `is_enabled`
      加 `_is_internal_session` 门）+ `tests/unit/test_v2_post_sampling_hooks.py`。
- [ ] **M4**（stretch）召回管线收敛为 `MemoryRecallPipeline`（只重组不改语义）

#### 1.4.5 Group — 多 Agent 群聊聊天室（编排工作流）

> **状态**: 🟡 进行中 (2026-06-06 起)。**逻辑判错 + 正确范式调研见
> `docs/audit/MULTI_AGENT_LOGIC_AUDIT_2026.md`**（旧 G1 实现按错范式做，已重写）。
> 用户拍板"4 种编排策略都要"：群聊 / 固定流水线 / 主管派活 / 目标驱动·自主。

- [x] **G1** 房间运行时：`GroupRoom` 模型 + `GroupRoomRegistry`(落盘 `~/.xmclaw/v2/rooms/`)。
- [x] **G2** WS/路由接入 + 共享记忆接线 + `routers/rooms.py`。
- [x] **G-重做** 统一编排内核 `RoomOrchestrator`（4 策略，按正确范式）：
      ① `chat` AutoGen 式（共享历史 + LLM 选讲者 + 增量喂入，**不再清历史塞 blob**）；
      ② `sequential` CrewAI 顺序接力；③ `supervisor` 主管 LLM 动态分派；
      ④ `autonomous` Magentic-One 双台账 + 内循环 5 问 + 卡住重规划。
      全部**限定房间参与者内**(修审计 #5)、结构化人格选择、明确终止、LLM 缺失优雅降级。
      退役旧 `group_orchestrator.py`/`workflow_room.py`(按错范式)。
      文件：`daemon/room_orchestrator.py`、`daemon/group_room.py(strategy 字段)`、
      `daemon/routers/rooms.py`、`static/pages/Rooms.js(4 策略选择器)`、
      `tests/unit/test_room_orchestrator.py(14)`、`tests/integration/test_rooms_router.py`。
- [x] **G3** 前端群聊房间 UI：多讲者 transcript + 参与者侧栏(实时状态点) + @点名
      (点名字/手打 `@aid` 强制该 agent 先发言，后端 `_parse_mention` 接) + 4 策略统一
      渲染(非群聊也显示分步 transcript + 最终结果块)。`static/pages/Rooms.js`。
- [x] **G4** 多 agent 控制面板：`pages/Agents.js` 显示人设(🎭 role/🎯 goal/📖 backstory)
      徽章 + 创建表单加结构化人格输入 + 6 个预设模板带 role/goal/backstory。
- [x] **G5** 结构化人格落地：agent config 顶层 role/goal/backstory/style →
      `_compose_persona` 幂等合成进 system_prompt(agent 真按人设说话)，并经 summary
      透出供编排器 `_persona_of` 选讲/派活。`routers/agents.py`、`tests/unit/test_agent_persona.py(5)`。
- [ ] **G5b** 预算/token 上限 + 死循环额外检测(autonomous 已有 stall→replan)。

**Group 进度日志**
2026-06-06: G1 落地 — 房间模型/注册表/编排器 + 单测 (commit 待填)
2026-06-06: G 重做 — 用户判错"逻辑跑偏"，调研 AutoGen/Magentic-One/CrewAI/LangGraph 真实
  实现，重写为统一 `RoomOrchestrator`(4 策略)，修历史模型+参与者限定+真接 LLM 选讲/派活/
  台账，退役旧两内核；28 测试通过 (commit a5d1c18)
2026-06-06: G3+G4+G5 — 群聊 UI(参与者侧栏+实时状态+@点名) + 多 agent 面板人设展示/创建 +
  结构化人格落 config(_compose_persona 合成 system_prompt)；@点名后端接 _parse_mention；
  20 测试通过 (commit 待填)

---

#### 1.4.4 进度日志（接上）

2026-06-05: M1 落地 — 标签分组 + 维护抽屉 + bucket 分组置顶 (commit 81c5e3f)
2026-06-06: 后台任务面板折叠重复条目 ×N (commit 4504f5c)
2026-06-06: M-fix1 — 内部反思会话跳过记忆抽取，断掉污染根因 (commit 待填)

---

### 1.5 ToolProvider 链 — 工具调用全流程

**文件**: `xmclaw/providers/tool/builtin.py`（746 行）, `xmclaw/providers/tool/mcp_hub.py`, `xmclaw/skills/tool_bridge.py`

**Provider 层级**（由内到外）:
```
CompositeToolProvider
├── BuiltinToolProvider ── 40+ 内置工具
│   ├── filesystem: file_read, file_write, list_dir, glob, grep
│   ├── shell: bash（enable_bash 控制）
│   ├── web: web_fetch, web_search（enable_web 控制）
│   ├── memory: memory_search, remember, memory_compact
│   ├── persona: update_persona, recall_user_prefs
│   ├── voice: voice_synthesize, voice_transcribe
│   ├── canvas: canvas_artifact_create/update
│   ├── worktree: enter_worktree, exit_worktree
│   ├── plan_mode: enter_plan_mode, exit_plan_mode
│   ├── db: sqlite_query
│   ├── journal: journal_append, journal_recall
│   ├── todo: todo_read, todo_write
│   ├── user: ask_user_question, learn_about_user
│   └── system: agent_status, apply_patch, schedule_followup, ...
├── SkillToolProvider ── 动态技能工具（skill_*）
│   └── SkillRegistry ── 注册/升级/回滚/历史
├── McpHub ── 多 MCP 服务器工具
│   └── MCPBridge × N（stdio 传输）
│       └── 64-char 工具名 mangling: {server_uid}__{tool_name}
├── BrowserToolProvider ── Playwright 浏览器自动化
└── MemoryBridge ── memory 工具桥接
```

**工具调用安全链**:
```
LLM emits ToolCall
    │
    ▼
PreToolUse Hook（可 deny / 改写 args）
    │
    ▼
asyncio.wait_for(invoke(), 180s)
    │
    ▼
ToolProvider.invoke()
    │
    ▼
实际工具执行
    │
    ▼
PostToolUse Hook（可 redact result）
    │
    ▼
PromptInjectionScanner.scan(result, SOURCE_TOOL_RESULT)
    │
    ▼
Guardian 审批（如启用）
    │
    ▼
HonestGrader.grade()
    │
    ▼
结果回注 messages
```

**SkillRegistry 处理逻辑**:
- `register(skill_id, version, spec, impl)` → 追加到 `_skills` dict
- `promote(skill_id, version)` → 将 version 设为 active HEAD
- `rollback(skill_id, version)` → 回退到历史版本
- `_persist()` → JSON 文件序列化（当前非原子写入）
- **竞态风险**：无 asyncio.Lock，并发 register/promote 可能损坏

---

### 1.6 WebSocket / HTTP — 前端与 Daemon 的通信协议

**文件**: `xmclaw/daemon/app.py:1791-2304`, `xmclaw/daemon/static/lib/ws.js`

#### WS 端点: `/agent/v2/{session_id}`

**连接建立流程**:
```
Client ──WS──► /agent/v2/{sid}?token={hex}&agent_id={id}
    │
    ▼
Origin check（B-355）── 防御 ClawJacked CVE
    │
    ▼
Auth check（pairing token 或 Bearer header）
    ├─ 失败 → close(4401 unauthorized)
    └─ 成功
        │
        ▼
Agent 选择（agent_id query param）
    ├─ "main" 或省略 → primary agent (app.state.agent)
    └─ 其他 → MultiAgentManager 查找
        └─ 未找到 → close(4404 agent not found)
        │
        ▼
Supersede 旧连接（B-348）
    ├─ 同一 session 已有 WS → 发送 superseded 帧 → close(4408)
    └─ 新连接成为 active_ws_for_session
        │
        ▼
Replay 历史事件（如 session 有 prior events）
    ├─ session_replay:start 帧
    ├─ 逐条 replayed BehavioralEvent
    └─ session_replay:end 帧
        │
        ▼
Subscribe EventBus（ predicate: session_id 匹配 或 全局事件类型）
    │
    ▼
publish(SESSION_LIFECYCLE, phase=create)
```

**消息帧格式**（双向 JSON）:
```json
// Client → Server
{"type": "user", "content": "...", "ultrathink": false,
 "plan_mode": false, "output_style": "default",
 "images": ["data:image/png;base64,..."],
 "correlation_id": "...", "llm_profile_id": "..."}

// Server → Client (BehavioralEvent)
{"id": "...", "ts": 1234567890.0, "session_id": "...",
 "agent_id": "...", "type": "llm_chunk", "payload": {"delta": "..."},
 "correlation_id": "...", "parent_id": "...", "schema_version": 1}
```

**WS 客户端重连逻辑** (`ws.js`):
- 指数退避：500ms → 1s → 2s → 4s ... 30s cap + 20% jitter
- 断线期间消息入队（max 64），重连后 flush
- 不可重连码：4401(auth), 4403(origin), 4408(superseded)
- 状态机：disconnected → connecting → connected → reconnecting

#### HTTP 端点（29 个 router）

| 前缀 | 功能 |
|------|------|
| `/api/v2/agents` | MultiAgent CRUD |
| `/api/v2/sessions` | Session 列表/搜索/删除 |
| `/api/v2/memory` / `/memory/v2` | v1/v2 记忆操作 |
| `/api/v2/skills` | Skill 注册/升级/回滚/历史 |
| `/api/v2/evolution` | 进化提案查看/审批 |
| `/api/v2/cron` | Cron job CRUD |
| `/api/v2/channels` | 通道配置（Feishu/TG/Slack/Email） |
| `/api/v2/files` | 文件上传/下载 |
| `/api/v2/system` | Daemon 重启/升级 |
| `/api/v2/dashboard` | 仪表板 overview |
| `/api/v2/sync` | UI 状态同步 |
| ... | ... |

---

### 1.7 HonestGrader — 双信号评分系统

**文件**: `xmclaw/core/grader/verdict.py`（Sprint 3 Iron Rule #1 重写）

**核心原则**: 任何晋升需要 ≥2 个独立信号；单信号绝不晋升（Hermes 的教训）

**Signal A — 确定性检查**（同步，权重重归一化）:
| 检查项 | 权重 | 说明 |
|--------|------|------|
| `ran` | 0.40 | 工具产生非平凡输出（拒绝 empty/"ok"/"done"） |
| `returned` | 0.20 | 原始输出存在 |
| `type_matched` | 0.20 | 声明的返回类型匹配（None=不适用，不加分） |
| `side_effect_observable` | 0.20 | fs/memory/bus 可观测副作用 |

**Signal B — 独立信号**（异步，第一个非 None 的获胜）:
| 信号 | 实现状态 | 说明 |
|------|---------|------|
| `UserFollowupSignal` | ✅ 完整 | 检查用户后续消息的正/负反馈 |
| `HoldoutTestSignal` | ⚠️ Stub | 预留的 holdout 测试信号 |
| `CrossJudgeSignal` | ⚠️ Stub | 预留的交叉评审信号 |

**组合逻辑**:
```python
if ind_score is None:
    final = det_score
    promote_eligible = False          # Iron Rule #1: 单信号不晋升
else:
    final = 0.6 * det_score + 0.4 * ind_score
    promote_eligible = (
        det_score >= 0.6 and          # 确定性门槛
        ind_score >= 0.5              # 独立信号门槛
    )
```

**事件流**:
```
HopLoop 完成 tool invocation
    │
    ▼
HonestGrader.grade(event, history=session_events)
    │
    ▼
publish(GRADER_VERDICT, verdict.to_payload())
    │
    ▼
EvolutionOrchestrator.subscribe(GRADER_VERDICT)
    │
    ▼
累积评分 → 达到阈值 → publish(SKILL_CANDIDATE_PROPOSED)
```

---

### 1.8 CognitiveDaemon — 1Hz 心跳认知管道

**文件**: `xmclaw/cognition/cognitive_daemon.py`（47KB）

**设计 Pipeline**（心跳每 tick 执行）:
```python
async def tick():
    percepts = await perception_bus.drain()          # 1. 收集感知
    filtered = await attention_filter.tick(percepts) # 2. 注意力筛选
    reasoning = await reasoning_engine.reason(       # 3. 推理判断
        mode=auto, query=..., llm=..., graph=..., bank=...
    )
    if reasoning.needs_action:
        plan = await planner.plan(reasoning)         # 4. 规划
        await action_dispatcher.execute_plan(plan)   # 5. 执行
    
    # 周期性任务（每 N ticks）
    if tick_count % goal_interval == 0:
        goals = await goal_generator.generate()
    if tick_count % experiment_interval == 0:
        await self_experiment_loop.tick()
```

**安全设计**: 每个 pipeline step 独立 try/except，异常记录但不传播

**当前状态**: 消费端完整，但感知生产者未接线 → **空转**
- `FileWatcher` 已推 EventBus + cognitive_state，但 **不推 PerceptionBus**
- `ProcessWatcher` 同理
- WS 消息不推 PerceptionBus（`percept_sources.py` wiring follow-up A）
- Cron tick 不推 PerceptionBus

---

### 1.9 ProactiveAgent — 30 秒主动扫描

**文件**: `xmclaw/cognition/proactive_agent.py`

**Tick 流程**（每 30s，quiet hours 23:00-07:00 跳过）:
```python
async def tick():
    for trigger in self._triggers:
        if trigger.should_fire():
            proposal = trigger.propose()
            await bus.publish(EventType.PROACTIVE_PROPOSAL, proposal)
            break  # 每 tick 最多一个提案
```

**内置触发器**:
| 触发器 | 条件 | 提案类型 |
|--------|------|---------|
| `idle_check_in` | 用户 30min 无消息 | "需要我做什么？" |
| `system_health` | 检测到异常指标 | 健康报告 |
| `calendar_reminder` | 日历事件临近 | 提醒 |
| `stale_project` | 项目 N 天无活动 | 跟进建议 |
| `cron` | 定时任务 | 执行报告 |
| `daily_digest` | 每日固定时间 | 日报 |

**Iron Rule #2**: 纯 Python 触发器（无 LLM per tick），LLM 只在触发后调用

---

### 1.10 EvolutionPipeline — 运行时流式进化

**文件**: `xmclaw/cognition/evolution_loop.py`（480 行）, `xmclaw/core/evolution/orchestrator.py`, `xmclaw/core/evolution/controller.py`

**设计哲学**: 进化不直接改写文件，而是写入 `proposals/` 目录，由人类或更高权限代理审批后应用（Iron Rule #2: staged adoption）

**组件架构**:
```
EvolutionOrchestrator（观察者）
├── 订阅 EventBus: GRADER_VERDICT, SKILL_CANDIDATE_PROPOSED
├── 累积评分 → 达到阈值 → 触发评估
└── 与 EvolutionController 交互

EvolutionController（决策者）
├── 接收 candidate 评估请求
├── 查询 registry: active_version, head_version
├── 应用 GraderVerdict.promote_eligible 作为晋升门槛
└── 决策: promote / rollback / 保持观察

EvolutionLoop（后台循环）
├── SkillPromoter ── 分析工具使用频率
│   └── 使用 ≥3 次 → propose "skill_promote"
├── SystemPromptEvolver ── 分析失败模式
│   └── 同一错误 ≥2 次 → propose "prompt_evolve"
├── PerformanceAnalyzer ── 成本/延迟分析
│   └── 异常延迟 → propose "perf_tuning"
└── PatternExtractor ── 模式提取
    └── 高频模式 → propose "pattern_extract"

SelfExperimentLoop（A/B 验证）
├── 假设 → 构建 baseline vs treatment
├── 并行运行 → 收集指标
├── Welch's t-test（stdlib-only，无 scipy）
└── 决策: adopt / reject / extend / abort
```

**进化生命周期**:
```
工具调用完成
    │
    ▼
HonestGrader.grade() → GraderVerdict
    │
    ▼
EvolutionOrchestrator.observe(verdict)
    ├── 按 skill_id 累积评分窗口
    ├── 统计: plays, successes, deterministic_scores
    └── 达到 min_plays + evidence 阈值？
        │
        ├─ 否 → 继续观察
        └─ 是 → 触发评估
            │
            ▼
        EvolutionController.evaluate(candidate)
            ├── 查询 registry 当前 HEAD
            ├── 对比 candidate 与 HEAD 的评分分布
            ├── 检查 promote_eligible（双信号门槛）
            │   ├─ False → publish(PROMOTION_BLOCKED)，记录原因
            │   └─ True  → 决策
            │       │
            │       ├─ auto_apply=false（默认）
            │       │   → publish(SKILL_PROMOTION_RECOMMENDED)
            │       │   → 等待人工审批（xmclaw evolve review）
            │       │
            │       └─ auto_apply=true
            │           → registry.promote(skill_id, version)
            │           → publish(SKILL_PROMOTED)
            │           → 全局广播（所有 WS 客户端可见）
            │
            ▼
        SelfExperimentLoop（如启用）
            ├── 注册假设: "candidate beats HEAD on metric X"
            ├── 运行 A/B: 10% 流量 → candidate, 90% → HEAD
            ├── 收集 N 样本 → Welch's t-test
            └── p<0.05 且 effect>0 → staged adopt event
```

**关键配置参数**:
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `evolution.enabled` | `false` | 主开关 |
| `evolution.auto_apply` | `false` | 自动应用进化提案 |
| `evolution.min_plays` | 5 | 评估所需最小调用次数 |
| `evolution.success_rate_threshold` | 0.6 | 成功率门槛 |

**补充组件（Phase 5 完整进化链条）**:

```
EvolutionOrchestrator（观察者/汇总）
├── 订阅 EventBus: GRADER_VERDICT, SKILL_CANDIDATE_PROPOSED
├── 累积评分 → 达到阈值 → 触发评估
└── 与 EvolutionController 交互

EvolutionController（决策者/4阈值门控）
├── 接收 candidate 评估请求
├── 查询 registry: active_version, head_version
├── 应用 GraderVerdict.promote_eligible 作为晋升门槛
│   ├── success_rate ≥ threshold
│   ├── deterministic_score ≥ 0.8
│   └── 双信号缺一不可
└── 决策: promote / rollback / keep_observing

ReflectiveMutator（提案生成器）
├── 监听 registry 变化 / user feedback / error patterns
├── 分析调用失败堆栈 → 生成 fix candidate
├── 分析高频模式 → 提取通用 skill
├── 产出: {"name", "description", "code", "rationale"}
└── publish SKILL_CANDIDATE_PROPOSED

EvolutionAgent（Headless observer）
├── 聚合多来源 GRADER_VERDICT
├── 按 skill_id × version 维护 EWMA 评分
├── 达到评估阈值 → evaluate() → 触发 debounced trigger
└── 无 EventBus 接线（独立后台线程消费 verdict 队列）

MutationOrchestrator（自动突变引擎）
├── 监听 GRADER_VERDICT（performance < threshold）
├── 识别表现不佳的技能 → 触发 mutation
├── 调用 ReflectiveMutator 生成 fix variant
└── 注册新 variant → registry 作为候选版本

ProposalMaterializer（决策实体化）
├── 订阅 SKILL_CANDIDATE_PROPOSED
├── 验证 decision="propose"（非 "ignore"/"defer"）
├── 检查 registry 是否已有同名不同实现 skill
│   └── 有 → 注册为新 version；无 → 注册为新 skill
└── 写入 ~/.xmclaw/skills/<id>.jsonl

EvolutionEvaluationTrigger（防抖调度器）
├── Debounced scheduler（cooldown 60s）
├── 避免 mutation storm：同 skill 60s 内只触发一次评估
└── 聚合多次 verdict → 批量评估

VariantSelector（A/B 流量分配器）
├── UCB1 算法: score = avg_reward + C × √(ln N / n_i)
├── 动态分配流量: 10% candidate / 90% HEAD（默认）
├── AgentLoop 每 turn 调用 select() 决定使用哪个版本
└── 与新 agent 创建绑定: 新 session → 重新计算分配

PromotionPolicy（晋升策略配置）
├── AUTO_ON_PASS_ALL  ── 全部通过则自动晋升
├── HUMAN_REQUIRED_MAJOR ── major 版本需人工审批
├── HUMAN_REQUIRED_ALL   ── 任何晋升都需人工审批
└── 默认: HUMAN_REQUIRED_MAJOR（Iron Rule #2 的 soft 版本）

SkillRegistryView（注册表层级视图）
├── 暴露只读接口: list_skill_ids(), get_active(skill_id)
├── 隐藏内部版本历史（防止外部直接操作 _head）
├── 被 SkillToolProvider 和 SkillPrefilter 消费
└── 保证并发安全（但底层 registry 无 lock）
```

**完整进化闭环**:
```
ReflectiveMutator.propose() ──► SKILL_CANDIDATE_PROPOSED
                                    │
                                    ▼
                         ProposalMaterializer.verify()
                                    │
                                    ▼
                         SkillRegistry.register(candidate)
                                    │
                                    ▼
                         VariantSelector (UCB1) 分配流量
                                    │
                           ┌────────┴────────┐
                           ▼                 ▼
                     HEAD (90%)         Candidate (10%)
                           │                 │
                           ▼                 ▼
                    AgentLoop 调用      AgentLoop 调用
                           │                 │
                           ▼                 ▼
                    HonestGrader.grade()  HonestGrader.grade()
                           │                 │
                           └────────┬────────┘
                                    ▼
                         EvolutionOrchestrator.observe()
                         EvolutionAgent (EWMA 聚合)
                                    │
                                    ▼
                         EvolutionController.evaluate()
                         (4阈值门控 + promote_eligible)
                                    │
                           ┌────────┴────────┐
                           ▼                 ▼
                      promote              keep_observing
                           │                    │
                           ▼                    │
                    PromotionPolicy.check()    │
                           │                    │
                  ┌────────┴────────┐          │
                  ▼                 ▼          │
              auto_apply          manual       │
                  │                 │          │
                  ▼                 ▼          │
           registry.promote()   SKILL_PROMOTION_RECOMMENDED
                  │                 │          │
                  ▼                 ▼          │
           SKILL_PROMOTED       等待 xmclaw evolve review
                  │                            │
                  └────────────────────────────┘
                                    │
                                    ▼
                           SelfExperimentLoop (A/B)
                           ├── 假设: "candidate beats HEAD"
                           ├── Welch's t-test (stdlib)
                           └── p<0.05 → staged adopt event
```

---

### 1.11 Security 系统 — 三层防护

**文件**: `xmclaw/security/prompt_scanner.py`, `xmclaw/security/policy.py`, `xmclaw/security/guardian.py`, `xmclaw/security/undo_cabinet.py`

XMclaw 的安全架构是**分层防御**，每层在不同阶段介入：

#### Layer 1: PromptInjectionScanner — 输入/输出扫描

**处理逻辑**:
```python
# 扫描源类型
SOURCE_USER_MESSAGE      # 用户直接输入
SOURCE_TOOL_RESULT       # 工具返回结果
SOURCE_MEMORY_RECALL     # 记忆召回块
SOURCE_FILE_CONTENT      # 文件读取内容
SOURCE_WEB_CONTENT       # 网页抓取内容

# 扫描流程
scan(text, policy, source)
├── 规则匹配（regex + heuristics）
│   ├── "ignore all previous instructions" 变体
│   ├── 角色扮演注入（"you are now DAN"）
│   ├── 分隔符逃逸（```, </system>）
│   └── 间接注入（记忆/文件中的恶意内容）
├── 决策
│   ├── block ── 内容被替换为 [BLOCKED: reason]
│   ├── flag  ── 标记但放行（DETECT_ONLY 模式）
│   └── pass  ── 无异常
└── 如 blocked/flagged → publish(PROMPT_INJECTION_DETECTED)
```

**调用点**:
- `AgentLoop.run_turn`: 用户消息提交前
- `HopLoop`: 每个 tool result 回注 messages 前
- `Memory recall`: 每条召回 chunk 渲染前（B-61）

#### Layer 2: Guardian — 权限审批

**文件**: `xmclaw/providers/tool/guarded.py`

**处理逻辑**:
```python
class GuardianToolProvider(ToolProvider):
    # 包装任意 ToolProvider，在 invoke 前插入审批
    
    async def invoke(call):
        decision = await guardian.check(call)
        ├─ PermissionLevel.ALLOW → 直接执行
        ├─ PermissionLevel.ASK   → 阻塞，等待用户确认
        └─ PermissionLevel.BLOCK → 返回拒绝结果
```

**权限级别**（当前 3 级，竞品 Claude Code 有 7 级）:
| 级别 | 行为 | 示例工具 |
|------|------|---------|
| `ALLOW` | 无需审批 | file_read, list_dir, web_search |
| `ASK` | 每次询问 | file_write, bash, apply_patch |
| `BLOCK` | 完全禁止 | 未配置或显式黑名单 |

#### Layer 3: UndoCabinet — 操作可逆

**文件**: `xmclaw/security/undo_cabinet.py`

**处理逻辑**:
```python
# 每个可逆操作在执行前记录 undo 信息
record_undo(action_type, action_id, undo_fn, metadata)

# 支持的操作类型
- file_write  → 记录原文件内容 → undo: 恢复原内容
- file_delete → 记录被删文件 → undo: 重新创建
- bash        → 记录命令 → undo: 有限（显式不覆盖）
- memory_put  → 记录旧值   → undo: 删除或恢复

# 调用路径
BuiltinTools.invoke()
    │
    ▼
undo_cabinet.record_undo(...)  # 执行前
    │
    ▼
实际工具执行
    │
    ▼
如失败 → undo_cabinet.undo(action_id)  # 自动回滚
```

**限制**: bash 命令因多样性过大，设计上**不自动逆转**（line 15 注释明确说明）

---

### 1.12 Daemon 启动顺序 — app_lifespan.py

**文件**: `xmclaw/daemon/app_lifespan.py`（3037 行）

**初始化顺序**（`_lifespan()` 内）:
```
1. 启动 sweep_task（日志清理）
2. 启动 backup_scheduler（备份调度）
3. 启动 events_retention_task（事件保留策略）
4. 启动 CronTickTask（60s 间隔，如 agent wired）
5. 启动 MemoryFileIndexer（MEMORY.md / USER.md 向量索引）
6. 构造 PersonaStore + migrate markdown → DB
7. 构造 MultiAgentManager（ Epic #17）
8. 构造 Primary AgentLoop（从 config）
   ├── LLMProvider
   ├── ToolProvider（builtin + skill + mcp + browser + memory）
   ├── MemoryManager
   ├── HonestGrader
   ├── CostTracker
   └── ...
9. 启动 CognitiveDaemon（如 cognition.enabled）
   ├── FileWatcher（如 continuous_loop.enabled）
   ├── ProcessWatcher（如 continuous_loop.enabled）
   ├── EvolutionLoop（如 evolution.enabled）
   ├── ProactiveAgent（如 proactive.enabled）
   ├── TaskScheduler（如 wired）
   ├── HTNPlanner（如 wired）
   └── SelfExperimentLoop（如 wired）
10. 启动 ChannelDispatcher（Feishu/TG/Slack/Email/Cron adapters）
11. 启动 Memory v2 Service（如 cognition.memory_v2.enabled）
12. 启动 ReflectionMaterializer（如 wired）
13. 启动 AutobiographicalMemory（如 wired）
14. 启动 Security components（prompt scanner, guardian）
15. yield（FastAPI 开始接受请求）
16. shutdown: 按相反顺序停止所有组件
```

**关键依赖关系**:
- AgentLoop 必须在 CronTickTask 之前构造（cron runner 调用 `agent.run_turn()`）
- CognitiveDaemon 必须在 FileWatcher/ProcessWatcher 之后启动（消费它们的事件）
- PerceptionBus 必须在 CognitiveDaemon 之前构造，但 **感知生产者接线是独立 follow-up**

---

### 1.13 系统联动关系总图

> 本节专门描述 **记忆 / 进化 / Chat / 技能 / 工具调用** 五大子系统之间的交叉调用、数据流和状态共享。

#### 总览图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              五大子系统联动全景                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────┐        ┌──────────┐        ┌──────────┐                     │
│   │  Chat    │◄──────►│  Agent   │◄──────►│  Tool    │                     │
│   │  System  │  WS    │  Loop    │ invoke │ Provider │                     │
│   │ (Web/TUI)│        │          │        │  Chain   │                     │
│   └────┬─────┘        └────┬─────┘        └────┬─────┘                     │
│        │                   │                   │                            │
│        │  EventBus         │  Memory Inject    │  Skill Discovery           │
│        │  (pub/sub)        │  (read)           │  (read)                    │
│        │                   │                   │                            │
│        ▼                   ▼                   ▼                            │
│   ┌──────────┐        ┌──────────┐        ┌──────────┐                     │
│   │ EventBus │◄───────│ Memory   │◄───────│ Skill    │                     │
│   │ (SQLite) │ write  │ System   │ write  │ System   │                     │
│   └────┬─────┘        └────┬─────┘        └────┬─────┘                     │
│        │                   │                   │                            │
│        │  GRADER_VERDICT   │  Fact Extract     │  Promote/Rollback          │
│        │  SKILL_CANDIDATE  │  (bg task)        │  (evidence-gated)          │
│        │                   │                   │                            │
│        ▼                   ▼                   ▼                            │
│   ┌──────────┐             │              ┌──────────┐                     │
│   │Evolution │─────────────┘─────────────►│Evolution │                     │
│   │Orchestr. │  observe & accumulate      │Controller│                     │
│   └──────────┘                            └──────────┘                     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

#### A. 记忆系统 ↔ 其他系统联动

**A1. 记忆 → AgentLoop（写入路径）**

```
用户消息到达 AgentLoop.run_turn()
    │
    ├─► AutobioMemory.extract_from_message() ──► 结构化提取"我是X"/"我在做Y"
    │                                              (同步, 不阻塞)
    │
    ├─► Memory v2 regex extract (bg task) ──► KeyInfoExtractor
    │       ├─ URL 模式       → kind=url
    │       ├─ 账号/密码模式   → kind=credential
    │       ├─ 数字目标       → kind=goal
    │       └─ explicit-remember → kind=preference
    │       └──► memory_service.remember() ──► LanceDB upsert
    │           └──► render_persona_after_writes() ──► 更新 IDENTITY.md/USER.md
    │
    └─► Memory v2 LLM extract (bg task) ──► LLMFactExtractor
            ├─ 语义身份        → kind=identity
            ├─ 隐含事实        → kind=preference/goal
            └─ 软偏好         → kind=preference
            └──► memory_service.remember() ──► LanceDB upsert
                └──► render_persona_after_writes()
```

**关键**: 两次提取都是 **background asyncio.Task**，不阻塞用户首 token。

**A2. 记忆 → AgentLoop（读取路径）**

```
AgentLoop 组装 user message 时
    │
    ├─► v1 MemoryManager.prefetch() ──► 预取块（hindsight 等外部 provider）
    │
    ├─► v1 MemoryManager.query(layer="long", hybrid=True)
    │       ├─ 有 embedder: 向量相似度 + keyword RRF
    │       ├─ 无 embedder: fallback LIKE 子串匹配
    │       └─ 过滤: 排除当前 session / <60s / file_chunk / archived
    │       └──► 渲染为 <memory-context> 块 (~2KB cap)
    │
    ├─► v2 MemoryService.render_for_prompt(k=8)
    │       ├─ KNN 搜索语义邻居
    │       ├─ fetch 1-hop CONTRADICTS/SUPERSEDES
    │       └──► 渲染: USER档案 + PROJECT档案 + DECISIONS + 矛盾标记
    │
    ├─► MemoryGraph.proactive_recall(limit=3)
    │       └──► 关系图谱相关记忆 → 追加到 <memory-context>
    │
    ├─► UnifiedMemory.query(semantic=user_message)
    │       └──► 多轴（语义+关系+时间）召回 → <unified-recall> 块
    │
    └─► AutobioMemory.summarize_for_prompt(max_facts=5)
            └──► 结构化用户档案 → 注入 system prompt (cache-friendly 位置)
```

**A3. 记忆 → EventBus**

| 事件 | 发布者 | 触发条件 |
|------|--------|---------|
| `MEMORY_OP` | MemoryManager | 每次 put/query 操作 |
| `MEMORY_RECALL` | UnifiedMemory | 每 turn 召回后（即使 hits=[]） |
| `USER_PROFILE_UPDATED` | ProfileExtractor |  persona 文件被修改 |

**A4. 记忆 ↔ CognitiveDaemon**

- CognitiveDaemon 每 tick 可读取 MemoryGraph 进行关系推理
- FileWatcher 检测到 MEMORY.md / USER.md 变化 → 触发 MemoryFileIndexer 重新索引
- 但 **当前未接线**: CognitiveDaemon 不主动写入记忆

---

#### B. 进化系统 ↔ 其他系统联动

**B1. 进化 → EventBus（订阅/发布）**

```
EvolutionOrchestrator.start() [当 auto_apply=True]
    │
    └─► bus.subscribe(SKILL_CANDIDATE_PROPOSED, _on_proposal)
            │
            └─► 收到 proposal → registry.promote(skill_id, version, evidence=...)
                    │
                    ├─► 成功 → publish(SKILL_PROMOTED) → 全局广播
                    └─► 失败 → 静默（exception 传播，无 phantom event）
```

**B2. 进化 → SkillRegistry**

```
SkillRegistry 数据结构:
    _skills:    {(skill_id, version): Skill}       ← 所有版本
    _versions:  {skill_id: [version, ...]}         ← 版本列表
    _head:      {skill_id: version}                ← 当前 HEAD
    _history:   {skill_id: [PromotionRecord, ...]} ← 只增历史
    _manifests: {(skill_id, version): SkillManifest} ← 元数据

EvolutionOrchestrator.promote(skill_id, to_version, evidence)
    │
    ├─► registry.promote() ──► 移动 HEAD 指针
    ├─► 写入 ~/.xmclaw/skills/<id>.jsonl（持久化）
    └─► publish(SKILL_PROMOTED) ──► EventBus

EvolutionOrchestrator.rollback(skill_id, to_version, reason)
    │
    ├─► registry.rollback() ──► 回退 HEAD 指针
    └─► publish(SKILL_ROLLED_BACK)
```

**B3. 进化 → HonestGrader（观察链路）**

```
HopLoop 完成 tool invocation
    │
    ├─► HonestGrader.grade(event, history=session_events)
    │       ├─ Signal A: deterministic checks (ran/returned/type/side_effect)
    │       └─ Signal B: UserFollowupSignal (检查用户后续反馈)
    │
    └─► publish(GRADER_VERDICT)
            │
            └─► EvolutionOrchestrator  subscriber
                    │
                    ├─ 按 skill_id 累积评分窗口
                    │   (plays, successes, deterministic_scores)
                    │
                    └─ 达到 min_plays + evidence 阈值？
                        ├─ 否 → 继续观察
                        └─ 是 → EvolutionController.evaluate(candidate)
                                    │
                                    ├─ promote_eligible=False
                                    │   → publish(PROMOTION_BLOCKED)
                                    │
                                    └─ promote_eligible=True
                                        ├─ auto_apply=false
                                        │   → publish(SKILL_PROMOTION_RECOMMENDED)
                                        │   → 等待人工审批
                                        └─ auto_apply=true
                                            → EvolutionOrchestrator.promote()
                                            → publish(SKILL_PROMOTED)
```

**B4. 进化 → AgentLoop（技能可用性）**

```
AgentLoop 每 turn:
    │
    ├─► effective_tools.list_tools()
    │       └─► CompositeToolProvider.list_tools()
    │               ├─ BuiltinTools (41 tools)
    │               ├─ SkillToolProvider ──► SkillRegistry.list_skill_ids()
    │               │       └──► 只有 HEAD 版本被暴露
    │               └─ MCP Hub tools
    │
    └─► SkillPrefilter.select_relevant_skills(user_message, tool_specs, top_k=12)
            ├─ 评分: name-substring(2x) + description(1x) + trigger(0.5x)
            ├─ 条件激活: 最近文件路径匹配 manifest.paths → boost
            └─ 如果无技能匹配 → 注入 skill_browse hint
```

**关键**: 进化修改 registry HEAD 后，**下一 turn** AgentLoop 的 `list_tools()` 自动看到新技能。无重启、无重新加载。

**D3. VariantSelector 联动细节**

```
AgentLoop.run_turn() 选择技能版本时
    │
    └─► VariantSelector.select(skill_id, session_context)
            │
            ├─ 新 session → 重新计算 UCB1
            │       score = avg_reward + C × √(ln N / n_i)
            │
            ├─ 返回 version_id → AgentLoop 调用该版本
            │
            └─ 数据流:
                    ┌─ plays[skill_id][version] += 1
                    ├─ rewards[skill_id][version] += grader_score
                    └─ N_total += 1
```

**D4. SkillRegistryView 联动细节**

```
SkillToolProvider.list_tools()
    │
    └─► SkillRegistryView.list_skill_ids()  (只读)
            │
            ├─ 遍历 registry._head → 获取 active version
            ├─ 过滤 UNTRUSTED 状态（默认不暴露）
            └─ 返回 ToolSpec 列表

SkillPrefilter.select_relevant_skills()
    │
    └─► SkillRegistryView.get_active(skill_id)  (只读)
            └─ 获取 manifest → 评分用 description/trigger
```

---

#### C. Chat系统 ↔ 其他系统联动

**C1. 端到端数据流**

```
用户在前端输入消息
    │
    ▼
Preact Composer ──► onSend()
    │
    ▼
wsHandle.send({type: "user", content: "...", images: [...], ultrathink: false})
    │
    ▼
WS Client (lib/ws.js) ──► 指数退避重连 + 断线消息队列(max 64)
    │
    ▼
Server: app.py /agent/v2/{sid} WS endpoint
    │
    ├─ Origin check (B-355)
    ├─ Auth check (pairing token / Bearer)
    ├─ Agent 选择 (agent_id query param → MultiAgentManager)
    ├─ Supersede 旧连接 (B-348)
    ├─ Replay 历史事件
    └─ Subscribe EventBus
    │
    ▼
收到 user frame → active_agent.run_turn(session_id, content, user_images=...)
    │
    ▼
AgentLoop → HopLoop → LLM + Tools
    │
    ▼
每步 publish(EventType.XXX) ──► EventBus
    │
    ▼
WS Forwarder subscriber ──► ws.send_text(json.dumps(event))
    │
    ▼
WS Client ──► onEvent(envelope)
    │
    ▼
store.setState({chat: applyEvent(chat, envelope)})
    │
    ▼
chat_reducer.js:
    ├─ USER_MESSAGE      → 添加/更新用户气泡
    ├─ LLM_REQUEST       → 创建 thinking 气泡
    ├─ LLM_CHUNK         → 追加 token 到 assistant 内容
    ├─ LLM_THINKING_CHUNK→ 追加到 thinking 区域
    ├─ TOOL_CALL_EMITTED → 创建 tool_use 消息
    ├─ TOOL_INVOCATION_FINISHED → 更新 tool 状态 + 结果
    ├─ GRADER_VERDICT    → (secondary reducer) 显示评分芯片
    ├─ SKILL_PROMOTED    → (secondary reducer) 显示进化闪光
    └─ PROACTIVE_PROPOSAL → 添加系统提案消息
    │
    ▼
Preact re-render ──► DOM 更新
```

**C2. 前端状态持久化**

```
localStorage keys:
    xmc_active_sid        → 当前 session id
    xmc_sid_list          → 历史 session 列表
    xmc_active_agent_id   → 当前 agent ("main" or sub-agent)
    xmc_composer_draft    → composer 草稿
    xmc_chat_{sid}        → 每 session 的 messages 快照

Boot 序列 (app.js:254-288):
    1. fetchPairingToken() → /api/v2/pair
    2. 恢复 activeSid from localStorage (或生成 newSid)
    3. connectFor(sid, token, agentId)
    4. hydrateChatHistory(sid) → /api/v2/sessions/{sid} REST 恢复
    5. rehydratePendingQuestions() → /api/v2/pending_questions
    6. fetchAgentsForPicker() → /api/v2/agents
```

**C3. 多 Agent 切换**

```
用户点击 Agent Picker → switchAgent(agentId)
    │
    ├─ persistActiveAgentId(agentId)
    ├─ disposeWs() + connectFor(newSid, token, agentId)
    └─ WS 连接带上 ?agent_id={id}

Server (app.py:1842-1852):
    requested_agent_id = ws.query_params.get("agent_id")
    if requested_agent_id and requested_agent_id != "main":
        ws_obj = agents_manager.get(requested_agent_id)
        active_agent = ws_obj.agent_loop  # 不同的 AgentLoop 实例
```

**C4. TUI Protocol Mismatch（~~已知问题~~ → ✅ 已修复）**

```
【修复前 — TUI 发送的帧格式】
    tui/app.py:on_submit()
    └── ws.send(json.dumps({"action": "submit", "message": text}))

【修复后 — TUI 发送的帧格式】
    tui/app.py:on_submit()
    └── ws.send(json.dumps({"type": "user", "content": text}))

【Daemon WS Handler 期望的格式】
    app.py:handle_agent_ws()
    └── frame["type"] == "user" → agent.run_turn(content=frame["content"])
```

**修复内容**: `tui/app.py` 的 `_on_user_send()` 方法已将帧格式从
`{"action": "submit", "session_id": "...", "message": text}` 改为
`{"type": "user", "content": text}`，与 Web 前端和 Daemon WS Handler 的期望完全一致。
TUI 消息现在完整走 AgentLoop 工具循环。

---

#### D. 技能系统 ↔ 其他系统联动

**D1. 技能生命周期全景**

```
【阶段 1: 创建/注册】
    │
    ├─ 人工编写 SKILL.md → ~/.xmclaw/v2/skills_user/<name>/
    ├─ 或 Agent 调用 skill_propose (UNTRUSTED 初始状态)
    └─ UserSkillsLoader 扫描 → SkillRegistry.register(skill, manifest)
            │
            └─► _skills[(skill_id, version)] = skill_instance
            └─► _head[skill_id] = version (如果是第一个版本)

【阶段 2: 发现】
    │
    └─ AgentLoop.list_tools() ──► SkillToolProvider.list_tools()
            └─► 遍历 registry HEAD → 生成 ToolSpec
                    ├─ tool name: skill_<id> (`.` → `__`)
                    └─ description: manifest.description

【阶段 3: 调用】
    │
    └─ LLM 选择 skill_demo__read_and_summarize
            │
            ▼
    CompositeToolProvider.invoke(call)
            │
            ▼
    SkillToolProvider.invoke(call)
            │
            ├─ 反向映射 skill_demo__read_and_summarize → demo.read_and_summarize
            ├─ registry.get(skill_id) → Skill instance
            └─ skill.run(SkillInput(args=call.args))
                    │
                    └─► 执行 skill 的 MarkdownProcedure / Code / Plugin

【阶段 4: 进化】
    │
    ├─ SkillPromoter 观察工具调用频率 → propose
    ├─ HonestGrader 评分 → GRADER_VERDICT
    ├─ EvolutionController 评估 → promote / rollback
    └─ EvolutionOrchestrator 执行 → 修改 registry HEAD
            │
            └─► 下一 turn AgentLoop 自动使用新版本

【阶段 5: 回滚】
    │
    └─ skill_rollback(skill_id, version) → registry.rollback()
            └─► HEAD 回退到旧版本
            └─► publish(SKILL_ROLLED_BACK)
```

**D2. SkillPrefilter 联动细节**

```
AgentLoop.run_turn() 构建 tool_specs 时
    │
    └─► select_relevant_skills(user_message, all_tool_specs, top_k=12)
            │
            ├─ Tokenise query + each skill description
            │   (CJK char-level, ASCII word-level)
            │
            ├─ Score = 2×name_overlap + 1×desc_overlap + 0.5×trigger_match
            │
            ├─ Conditional activation (G-05):
            │   extract_recent_paths(prior_messages, lookback=8)
            │   → 如果 skill.manifest.paths 匹配最近文件路径 → boost
            │
            └─ Return top-12 skills + meta-tools (skill_browse 等)
                    │
                    └─► 如果 0 真实技能匹配 → 注入 skill_browse hint
```

---

#### E. 工具调用逻辑 ↔ 其他系统联动

**E1. ToolProvider 组装顺序（由内到外）**

```
build_tools_from_config(cfg) 返回的 provider 是一个洋葱:

最外层 (最后被调用, 最先处理):
    GuardedToolProvider ── 安全审批 (如 security.guardians.enabled)
        │
        ├─ CompositeToolProvider
        │   ├─ ErrorAwareRetryProvider ── LLM 引导的语义重试
        │   │   ├─ CompositeToolProvider
        │   │   │   ├─ BuiltinTools ── 41 个内置工具 (always)
        │   │   │   ├─ BrowserTools ── Playwright (optional)
        │   │   │   ├─ LSPTools ── LSP (optional)
        │   │   │   ├─ ComputerUseTools ── GUI 自动化 (optional)
        │   │   │   ├─ MediaTools ── 麦克风/摄像头 (optional)
        │   │   │   ├─ ComposioToolProvider ── 7000+ SaaS (optional)
        │   │   │   ├─ CalendarToolProvider ── ICS 日历 (optional)
        │   │   │   ├─ CodebaseToolProvider ── 代码库索引 (optional)
        │   │   │   └─ SkillToolProvider ── 动态技能工具
        │   │   │       └─ SkillRegistry (HEAD 版本)
        │   │   └─ MCP Hub ── 多 MCP 服务器 (optional)
        │   │       └─ MCPBridge × N (stdio JSON-RPC)
        │   └─ SubagentToolProvider ── 并行子代理 (optional)
        │
        └─ UndoCabinet ── 内置在 file_write/file_delete/memory_put 等工具内部
```

**E2. 单次工具调用完整调用栈**

```
LLM 在 hop N 输出 ToolCall(id, name, args)
    │
    ▼
HopLoop: for call in response.tool_calls:
    │
    ├─ publish(TOOL_CALL_EMITTED)
    ├─ publish(TOOL_INVOCATION_STARTED)
    │
    ▼
_invoke_single_tool(call, effective_tools, session_id)
    │
    ├─ 1. PreToolUse Hook (如果 _hook_engine 存在)
    │       ├─ decision=deny → 返回 failed ToolResult
    │       └─ updated_input → 改写 args
    │
    ├─ 2. asyncio.wait_for(effective_tools.invoke(call), 180s)
    │       │
    │       ▼
    │   GuardedToolProvider.invoke (如启用)
    │       ├─ denied_list? → block
    │       ├─ approval_service.consume_approval? → bypass
    │       ├─ engine.guard(tool_name, params) → severity
    │       └─ policy.action_for(severity)
    │           ├─ DENY    → block
    │           ├─ APPROVE → return "NEEDS_APPROVAL:<id>"
    │           └─ ALLOW   → 继续
    │       │
    │       ▼
    │   CompositeToolProvider.invoke
    │       ├─ _router.get(call.name) → O(1) 路由
    │       ├─ miss → live re-scan (处理动态技能)
    │       └─ child.invoke(call)
    │           │
    │           ▼
    │       具体 Provider (Builtin/Skill/MCP/Browser...)
    │           │
    │           ├─ BuiltinTools: 直接执行 Python 函数
    │           ├─ SkillToolProvider: registry.get(id).run(SkillInput)
    │           ├─ MCPBridge: JSON-RPC tools/call → subprocess
    │           └─ BrowserTools: Playwright 操作
    │
    ├─ 3. B-17: 瞬态失败重试一次 (0.5s delay)
    │
    ├─ 4. PostToolUse Hook (如启用)
    │       └─ updated_input → redact result content
    │
    ├─ 5. publish(TOOL_INVOCATION_FINISHED)
    │
    ├─ 6. PromptInjectionScanner.scan(result, SOURCE_TOOL_RESULT)
    │       ├─ blocked → 跳过该结果, 不注入 messages
    │       └─ flagged → 标记但放行
    │
    ├─ 7. Guardian 审批 (如启用, 第二层)
    │
    ├─ 8. HonestGrader.grade(event)
    │       └─ publish(GRADER_VERDICT)
    │
    └─ 9. 结果 → 作为 tool message 追加到 messages
```

**E3. UndoCabinet 联动细节**

```
UndoCabinet 不是 ToolProvider 包装器，而是**内置在工具实现内部**:

file_write 工具:
    ├─ 写入前: undo_cabinet.record("file_write", path, old_content)
    └─ 执行: 写入新内容
        │
        └─ 如果后续出错 → undo_cabinet.undo(action_id) → 恢复旧内容

file_delete 工具:
    ├─ 删除前: 读取并保存内容
    ├─ record("file_delete", path, content)
    └─ 执行: 删除文件

memory_put 工具:
    ├─ 更新前: record 旧值
    └─ 执行: 写入新值

限制: bash 命令因多样性过大，设计上不自动逆转
```

---

#### F. 联动关系速查表

| 从 → 到 | 联动方式 | 数据 | 频率 |
|---------|---------|------|------|
| Memory → AgentLoop | 函数调用 + bg task | memory_ctx_block, autobio_block | 每 turn |
| Memory → EventBus | publish | MEMORY_OP, MEMORY_RECALL | 每 turn/op |
| AgentLoop → Memory | 函数调用 (bg) | extract_and_remember | 每 turn |
| Evolution → SkillRegistry | 函数调用 | promote/rollback HEAD | sporadic |
| Evolution → EventBus | publish/subscribe | SKILL_PROMOTED, GRADER_VERDICT | sporadic |
| AgentLoop → SkillRegistry | 读取 | list_tools(), get() | 每 turn |
| SkillRegistry → AgentLoop | 回调 (动态) | HEAD 变化 → 下 turn 可见 | 每次 promote |
| AgentLoop → EventBus | publish | ~15 种事件 | 每 turn/hop |
| EventBus → Frontend | WS send | BehavioralEvent JSON | 实时 |
| Frontend → AgentLoop | WS recv | user frame | 每消息 |
| ToolProvider → AgentLoop | 返回 | ToolResult | 每 tool call |
| HonestGrader → Evolution | EventBus | GRADER_VERDICT | 每 tool call |
| Guardian → ToolProvider | 包装器拦截 | ALLOW/ASK/BLOCK | 每 tool call |
| UndoCabinet → Tool | 内部钩子 | undo record | 每次破坏性操作 |
| CognitiveDaemon → PerceptionBus | 消费 | Percept drain | 1Hz |
| ProactiveAgent → EventBus | publish | PROACTIVE_PROPOSAL | ~30s |
| FileWatcher → EventBus | publish | FILE_SYSTEM_EVENT | 5s 轮询 |
| FileWatcher → CognitiveState | 直接更新 | attention focus | 5s 轮询 |

---

## Phase 1: 开箱即贾维斯（默认翻转 + 域清理 + 前端修复）

> **目标**: 新用户第一次启动就能感受到"这是个贾维斯"  
> **时间**: 2-3 天  
> **风险**: 低（配置变更 + 字符串替换 + 小修复）  
> **联动影响**: 无架构改动

### 1.1 默认配置翻转

- [ ] **文件**: `daemon/config.example.json`
- **变更**:
  ```json
  // BEFORE:
  "evolution": { "enabled": false, ... }
  "cognition": {
    "enabled": true,
    "memory_v2": { "enabled": false },
    "continuous_loop": { "enabled": false },
    "proactive": { "enabled": true, ... }
  }

  // AFTER:
  "evolution": { "enabled": true, "auto_apply": false, ... }
  "cognition": {
    "enabled": true,
    "memory_v2": { "enabled": true },
    "continuous_loop": { "enabled": true, "autonomy_level": 50 },
    "proactive": { "enabled": true, ... }
  }
  ```
- **验收**:
  - [ ] `xmclaw doctor` 通过所有检查
  - [ ] 启动日志显示 `cognitive_daemon.started`、`proactive_agent.started`、`evolution_loop.started`
  - [ ] `daemon/config.json` 新字段不破坏旧配置解析

### 1.2 清理"陪玩店"业务域残留

- [ ] **文件清单**:
  - `xmclaw/memory/v2/key_info_extractor.py`（模块 docstring）
  - `xmclaw/memory/v2/llm_extractor.py`（identity 示例）
  - `xmclaw/memory/v2/entity.py`（URL 示例）
  - `xmclaw/memory/v2/service.py`（CJK 分词示例、bi-gram 注释）
  - `xmclaw/memory/v2/embedding.py`（docstring 示例）
  - `xmclaw/memory/v2/llm_topic.py`（CJK bigram 示例）
  - `xmclaw/daemon/agent_loop.py`（memory_search 示例，~1749 行）
  - `xmclaw/daemon/prompt_builder.py`（诚实模式示例，~153-176 行）
  - `scripts/cleanup_smoke_test_facts.py`（DIRECT_POST_TEXTS）
- **方法**: 将所有"陪玩店" / "pw310.wxselling.com" / "月流水" / "游戏陪玩俱乐部" 替换为中性示例

### 1.3 SkillRegistry 并发锁（竞态修复）

- [ ] **文件**: `xmclaw/skills/registry.py`
- **变更**: 为 `register()`, `promote()`, `demote()`, `remove()`, `_persist()` 添加 `asyncio.Lock`
- **注意**: `_persist()` 当前使用非原子文件写入，需改为 `tempfile + rename` 模式

### 1.4 前端修复（审计发现）

- [ ] **VAD 模块激活**: `lib/vad.js` 完整实现但从未被导入。需在 `app.js` 或 `Chat.js` 中条件导入
- [ ] **移动端 CSS 修复**: `styles/mobile.css:50` 的 `.is-open` 需与 `AppShell.js:248` 的 `.is-mobile-open` 统一
- [ ] **alert() 替换**: `memory_facts_v2_graph.js:202,205` 的原始 `alert()` 应改用项目内部的 `toast` / `confirmDialog` 模式
- [ ] **Service Worker 增强**: 当前仅缓存 shell。API 响应缓存策略可后续迭代

### 1.5 密钥安全注释

- [ ] **文件**: `daemon/config.json`
- **方法**: 在文件顶部添加 JSON 注释（或 `_comment` 字段）说明此文件为测试环境专用，生产环境应使用环境变量

---

## Phase 2: 感知层接线（CognitiveDaemon 管道贯通）

> **目标**: 让 CognitiveDaemon 的 1Hz 心跳真正有意义  
> **时间**: 3-5 天  
> **风险**: 中（需改动 `app_lifespan.py` 启动顺序）  
> **前置**: Phase 1 完成

### 2.1 PerceptionBus 生产者接线（wiring follow-up A）

- [ ] **文件**: `xmclaw/daemon/app_lifespan.py`
- **变更**: 在 `FileWatcher` / `ProcessWatcher` 已有事件推送的基础上，增加向 `PerceptionBus.push()` 的推送
- **已有基础**: `FileWatcher` 已推 `EventBus(FILE_SYSTEM_EVENT)` + `cognitive_state attention focus`。需增加 `PerceptionBus.push(FilePercept(...))`
- **新增源**:
  - WebSocket 消息 → `PerceptSource.ws`
  - Cron tick → `PerceptSource.time`
  - 系统健康检查 → `PerceptSource.internal`
  - 网络变化 → `PerceptSource.network`（可选）

### 2.2 CognitiveDaemon 消费端完善

- [ ] **文件**: `xmclaw/cognition/cognitive_daemon.py`
- **变更**: 确保 `tick()` 中调用 `PerceptionBus.drain()` 而非当前可能的空实现
- **验证**: 添加日志 `percepts_drain_count=N` 以便观察感知流

### 2.3 AttentionFilter → CognitiveDaemon 接线

- [ ] **文件**: `xmclaw/cognition/cognitive_daemon.py`
- **变更**: 在 `tick()` 中， drained percepts 先经过 `AttentionFilter.tick(percepts)` 筛选
- **注意**: `AttentionFilter` 当前标注为 greenfield，但代码完整，只需调用

### 2.4 ReasoningEngine → CognitiveDaemon 接线

- [ ] **文件**: `xmclaw/cognition/cognitive_daemon.py`
- **变更**: 筛选后的 percepts 送入 `ReasoningEngine.reason(mode=auto, query=..., llm=..., graph=..., bank=...)`
- **注意**: `ReasoningEngine` 是 provider-free 设计，接受 duck-typed 参数，无需硬依赖

### 2.5 Planner + ActionDispatcher 接线（wiring follow-up B）

- [ ] **文件**: `xmclaw/cognition/cognitive_daemon.py`
- **变更**:
  ```python
  # 在 tick() 中，如果 reasoning 判断需要动作：
  plan = await self.planner.plan(reasoning_result)
  await self.action_dispatcher.execute_plan(plan)
  ```
- **注意**: `Planner` 是单步反应式规划器，`HTNPlanner` 是独立模块用于 Goal 分解。CognitiveDaemon 使用前者。

### 2.6 验收标准

- [ ] 启动日志显示 `perception_bus.ready`, `attention_filter.ready`, `reasoning_engine.ready`
- [ ] 文件系统变化触发 CognitiveDaemon tick 并产生日志
- [ ] `tests/unit/test_cognitive_daemon.py` 通过（新增/更新）

---

## Phase 3: 贾维斯能力解锁（让写好的代码真正工作）

> **目标**: 激活已完整实现但悬空的 Phase 6 模块  
> **时间**: 4-6 天  
> **风险**: 中（模块交互复杂）  
> **前置**: Phase 2 完成

### 3.1 SelfExperimentLoop 接入 CognitiveDaemon

- [ ] **文件**: `xmclaw/cognition/cognitive_daemon.py`
- **变更**: 每 N ticks 调用 `SelfExperimentLoop.tick()`
- **已有基础**: A/B 测试框架完整（Welch's t-test，stdlib-only），持久化到 `~/.xmclaw/v2/experiments.db`

### 3.2 HTNPlanner 接入 GoalGenerator

- [ ] **文件**: `xmclaw/cognition/goal_generator.py`（或新建）
- **变更**: 当 `GoalGenerator.generate()` 产生 Goal 时，调用 `HTNPlanner.decompose(goal)` 分解为 Task DAG
- **约束**: max_depth=3, max_sub_goals=6, max_total_cost_usd=1.0

### 3.3 TaskScheduler 增强

- [ ] **文件**: `xmclaw/cognition/task_scheduler.py`
- **现状**: `_wake_scheduler` 是 no-op，依赖 1 秒轮询
- **改进**: 实现事件驱动唤醒（`asyncio.Event`），当新 task submit 时唤醒调度器

### 3.4 MagicDocs 验证与启用

- [ ] **文件**: `xmclaw/cognition/magic_docs.py`
- **现状**: 检测 `# MAGIC DOC: <title>` 头，每 turn 结束后 schedule_updates，spawn 背景 sub-task
- **验证**: 创建一个测试 markdown 文件，确认 daemon 能自动更新
- **注意**: 无持久化状态，daemon 重启后需重新扫描文件重建 tracked 状态

### 3.5 Speculation 验证与启用

- [ ] **文件**: `xmclaw/cognition/speculation.py`
- **现状**: 在 LLM 流式输出时预执行 READ_ONLY_TOOLS（file_read, glob, grep, list_dir, web_search）
- **验证**: 运行含工具调用的对话，观察 `hop_loop Phase B` 是否命中缓存
- **注意**: 流式结束时 drain cache，取消仍在运行的 speculation

### 3.6 Metacognition Pipeline 接入

- [ ] **文件**: `xmclaw/cognition/metacognition/`
- **模块**: trace / pass / reformer，全部功能完整
- **变更**: 在 `AgentLoop.run_turn()` 的关键节点插入 metacognition hooks

### 3.7 Intent Engine LLM层补齐

- [ ] **文件**: `xmclaw/cognition/intent_engine.py`
- **现状**: `_run_llm_layer()` 是 TODO stub，规则层工作正常
- **变更**: 实现 LLM-based intent classification 作为规则层的 fallback

### 3.8 验收标准

- [ ] SelfExperimentLoop 能在后台运行 A/B 测试并记录结果
- [ ] HTNPlanner 能将 Goal 分解为可执行的 Task DAG
- [ ] TaskScheduler 在 task submit 时立即响应（非 1 秒延迟）
- [ ] MagicDocs 能自动更新标记文件
- [ ] Speculation 命中率达到可接受水平（>30%）

---

## Phase 4: 评估体系打通（Benchmark CI 化）

> **目标**: 让 HonestGrader 不再"自说自话"，有外部 benchmark 验证  
> **时间**: 3-4 天  
> **风险**: 低-中（适配工作）  
> **前置**: Phase 1-3 完成

### 4.1 Benchmark Runner 接线

- [ ] **文件**: `xmclaw/eval/harness.py`
- **现状**: 框架完整（Suite + Runner + TaskResult），但从未跑过真实 benchmark
- **变更**: 创建 `scripts/run_benchmarks.py` CLI，接受 `--suite` 参数

### 4.2 适配器完成

- [ ] **LongMemEval 适配器**: 完成 TODO，接入真实语料
- [ ] **TerminalBench 适配器**: 完成 TODO，接入 docker 运行时
- [ ] **SWE-bench 适配器**: 完成 TODO（如 scope 允许）
- [ ] **LoCoMo 适配器**: 完成 TODO

### 4.3 Tier-2 Sandbox Graders 自动接线

- [ ] **现状**: Tier-2 sandbox graders 未自动接线，Tier 1 是启发式评分
- [ ] **变更**: 在 Eval Harness 中自动检测 sandbox 环境并启用 Tier-2

### 4.4 CI 集成

- [ ] **GitHub Actions workflow**: 每周/每日定时运行 benchmark，结果上传到 artifact

### 4.5 验收标准

- [ ] `python -m xmclaw.eval.run --suite=longmemeval` 能跑通
- [ ] Benchmark 结果可比较（保存历史记录）
- [ ] CI 中 benchmark 不阻塞 PR（信息性）

---

## Phase 5: 能力补齐与差异化强化

> **目标**: 补齐竞品已有的能力，强化 XMclaw 的硬差异化  
> **时间**: 5-7 天  
> **风险**: 中-高  
> **前置**: Phase 1-4 完成

### 5.1 MCP 传输层扩展

- [ ] **文件**: `xmclaw/providers/tool/mcp_hub.py`
- **现状**: stdio 完整实现，SSE/WS/streamableHttp 被显式跳过
- **变更**: 实现 SSE 传输（优先级最高，Claude Desktop 已支持）

### 5.2 Docker Runtime 技能挂载

- [ ] **文件**: `xmclaw/skills/docker_runtime.py`
- **现状**: 容器隔离、网络隔离、资源限制完整，但技能源码未挂载进容器
- **变更**: 在 `run_in_container()` 中将技能源码目录 mount 为 read-only volume

### 5.3 Plugin SDK 动态加载

- [ ] **文件**: `xmclaw/plugin_sdk/`
- **现状**: 只有重导出层（`__init__.py`），没有动态插件发现/加载代码
- **变更**: 实现 entry-point 发现 + 动态 import + 版本兼容性检查
- **注意**: 需维护 `FROZEN_SURFACE` 兼容性契约

### 5.4 Email 附件支持

- [ ] **文件**: `xmclaw/daemon/channels/email.py`
- **现状**: 被显式跳过，无提取或转发
- **变更**: 实现附件提取（基础 MIME 解析）

### 5.5 ComputerUseTools 安全审计

- [ ] **文件**: `xmclaw/cognition/computer_use.py`
- **现状**: 3层安全（provider off → BLOCK → FAILSAFE）
- **审计**: 确认 pyautogui FAILSAFE 在跨平台场景下可靠

### 5.6 硬差异化强化

| 差异化点 | 当前状态 | 强化方向 |
|---------|---------|---------|
| **运行时流式进化** | EvolutionLoop 完整但默认关闭 | Phase 1 打开后，增加进化可视化（Web UI 面板） |
| **HonestGrader** | 完整且工作 | 增加 grader verdict 历史趋势图 |
| **ProactiveAgent** | 完整且工作 | 增加更多触发器（代码审查提醒、依赖更新提醒） |
| **MCP 多服务器** | stdio 完整 | Phase 5.1 扩展 SSE |
| **SelfExperiment** | 完整但悬空 | Phase 3 激活后，增加实验报告 UI |
| **MagicDocs** | 完整但悬空 | Phase 3 验证后，推广到更多文档类型 |

---

## Phase 6: 用户体验打磨与 v1.1.0 发布

> **目标**: 产品级打磨，发布 v1.1.0 "Jarvis"  
> **时间**: 3-4 天  
> **风险**: 低  
> **前置**: Phase 1-5 完成

### 6.1 前端完善

- [ ] **5个隐藏页面导航**: `/agents`, `/channels`, `/tools`, `/security`, `/docs` 添加侧边栏入口（或说明为什么隐藏）
- [ ] **Config 编辑器**: 在 Web UI 中提供安全的 config.json 只读查看器（或带验证的编辑器）
- [ ] **i18n 架构**: 虽然当前全中文，但建议预留 i18n 钩子（不强制翻译）
- [ ] **Dashboard 进化面板**: 展示进化进度、技能候选列表、实验结果

### 6.2 品牌一致性

- [ ] TUI 默认 agent_name: "Jarvis" → 确认是否需要与 CLI/Web 统一
- [ ] 前端 `index.html` 自称 "Phase 0 scaffold" → 更新为 v1.1.0 稳定版描述

### 6.3 文档与发布

- [ ] 更新 `README.md` 定位描述（从"陪玩店工具"到"自主进化核心的 AI Agent"）
- [ ] 更新 `CHANGELOG.md` v1.1.0 条目
- [ ] 发布 GitHub Release，附带 benchmark 报告

---

## Phase 7: Memory V1→V2 收口（双栈合一）

> **状态**: ✅ 完成 (2026-05-24)
> **目标**: 彻底退役 V1 (`xmclaw/memory/unified.py` + sqlite_vec + MemoryGraph)，所有读写收口到 V2 (`MemoryService` + LanceDB)。消灭双 import 入口 / 双数据目录 / `memory_search` 双查桥接。
> **时间**: 3 周（阶段 1 ~1 周 facade 收口 + 阶段 2 ~2 周后端替换）
> **风险**: 中（用户已有真实数据需要迁移，需要回滚预案）
> **前置**: 无（独立于 Phase 1-6）；与 Phase 1-6 并行推进，但 §7.A 完成前不动后端
> **决策记录**: 2026-05-23 用户拍板方案 A，否决方案 B（V1 降为 V2 backend）。决定性因素：用户"统一路径原则"——B 永久保留 LanceDB + SQLite 双引擎、写路径仍分两条只是被 facade 遮盖；A 是唯一根治"写在 /a 读在 /b"的方案。

### 7.0 现状诊断（why this Phase exists）

- **V1**：`xmclaw/memory/unified.py` 暴露 `UnifiedMemorySystem(query/put/delete)`，底层 sqlite_vec（向量）+ MemoryGraph（图）+ temporal 索引，4 层语义（working/short_term/long_term/procedural），有 TTL/retention，跨轴写带补偿（`UnifiedWriteError`）。21 个 callsite。
- **V2**：`xmclaw/memory/v2/` 暴露 `MemoryService(remember/recall/relate/neighbors)`，底层 LanceDB（vector + graph），Fact + Relation 类型化模型（kind/scope/layer），contradicts 检测，确定性 ID，LLM 抽取管道。31 个 callsite。
- **当下并存代价**：
  1. 数据双写：`~/.xmclaw/v2/memory.db` + `~/.xmclaw/v2/facts/` 各活各的
  2. `memory_search` 工具在 [app_lifespan.py:2218-2257](../xmclaw/daemon/app_lifespan.py:2218) 需 hot-wire 桥接才能同时查两套
  3. 心智模型分裂：调用方要知道"lesson 走 V2、procedural 走 V1"
- **`v2/__init__.py:20-23`** 写明 "kept untouched during migration ... until Phase 5 swap" — Phase 5 swap 从未发生，本 Phase 即兑现该承诺。

### 7.1 设计 spec（MEMORY_EVOLUTION_REDESIGN 内嵌版）

> 原 `docs/MEMORY_EVOLUTION_REDESIGN.md`（1274 行）于 commit f4e57c8 退役并入本文件。代码内引用该路径的 4 处（`v2/__init__.py:6`、`v2/service.py:8`、`v2/models.py`、`app_lifespan.py:2178`）后续 PR 一并改为指向本节。

#### 7.1.1 四层金字塔（L0→L1→L2→L3）

```
L0 events        events.db (不变)                 — 原始事件流（observer 写入）
L1 facts         ~/.xmclaw/v2/facts/  (LanceDB)   — 类型化 Fact + Relation（V2 当前已覆盖）
L2 experience    ~/.xmclaw/v2/experiences/        — 多 fact 聚合的"经历单元"（本 Phase 新增）
L3 skills        SkillRegistry (已存在)           — 可执行能力，由 L2 提炼
```

- **L0 → L1**：`KeyInfoExtractor`（regex 同步路径）+ `LLMFactExtractor`（后台路径），见 `agent_loop.run_turn`
- **L1 → L2**：`ExperienceDistiller`（待建），按主题/时间窗聚合相关 facts → Experience 单元
- **L2 → L3**：`SkillDreamCycle`（已存在），从 Experience 中提炼候选 skill

#### 7.1.2 Fact / Relation 模型（V2 现状即终态）

- `Fact(id, text, kind, scope, layer, embedding, evidence_count, contradicts[], superseded_by, created_at, updated_at)`
- `id = kind:scope:hash12(text)` 确定性，内容去重免费
- `kind` ∈ {identity, preference, goal, lesson, url, credential, decision, fact, topic, ...}
- `scope` ∈ {global, user, project, session, persona}
- `layer` ∈ {working, long_term} —— **本 Phase 新增 procedural**
- `Relation(source_id, target_id, kind, strength)`, `kind` ∈ {SAME_TOPIC, CONTRADICTS, SUPERSEDES, CAUSED_BY, RELATED_TO, MENTIONS}

#### 7.1.3 后端契约（替换 V1 sqlite_vec + MemoryGraph 后必须等价）

| V1 能力 | V2 当前 | 本 Phase 补齐方式 |
|---|---|---|
| TTL / max_items / max_bytes per layer | ❌ | §7.5 新增 `MemoryService.sweep()` + `retention` 配置段 |
| `query_by_time_range(start, end)` | 二次过滤 | §7.6 LanceDB 上加 `ts` 列索引，新增 `recall(time_range=...)` 参数 |
| `FactLayer.procedural` | ❌（只 working/long_term）| §7.7 新增 `procedural` enum 值 + 不参与 TTL 回收 |
| 跨轴写补偿 (`UnifiedWriteError`) | 单后端写 | §7.8 LanceDB merge_insert 已天然原子；图边写失败回滚 vector 写 |
| 时序 axis 作为 first-class | ❌ | §7.6 同上 |

### 7.A 阶段 1：Facade 收口（~1 周）

> **目标**：用户态只剩 V2 一个 import 入口；V1 继续跑、但只作 internal backend；`memory_search` 双查桥接退役。**完成后用户体感 V1/V2 之争已经消失。**

- [x] **7.A.1 callsite 盘点（半天）** — 完成报告 `docs/audit/AUDIT_2026-05-23_phase7_memory_v1_callsites.md`，25 个文件分 9 类，输出 §7.A.2 必须补的 7 个 P0 shim 清单 + §7.A.3 的 6 步迁移顺序。
- [x] **7.A.2 在 V2 加 shim API** — 完成 6/7 P0 shim（FactLayer.PROCEDURAL / MemoryService.delete / MemoryServiceWriteError / legacy_node_type_to_kind / recall(time_range=) / LLMFactExtractor.extract_candidates + LLMCandidate）。shim #4 (query_layer for short_term) 弃用：reflection_cycle 改用 recall(only_layer="working", time_range=...) 表达。commits 8163ff2 + dbe0bf1。
- [x] **7.A.3 callsite 逐个迁移** — 6 个 step 全完成：
  - step 1/6 reflection_cycle.py (commit 4ad31bd)
  - step 2a/6 agent_loop.py recall path (commit e86d199)
  - step 3a/6 hop_loop.py auto-put (commit 6bc2705)
  - step 4/6 routers/memory.py (commit 293afbd)
  - step 5/6 factory.py + lifespan canonical wire (commit ea30063)
  - step 6/6 删 V1 别名 + V1 fallback 分支 (commit 31a7487)
- [x] **7.A.4 退役桥接代码** — 在 step 6/6 一并完成。`BuiltinTools.set_memory_v2_service()` 沿用旧名（V2 时代正式名称），未来若改 rename 走 §7.B。
- [x] **7.A.5 删旧 `__init__` 导出** — 暂未做。V1 `xmclaw/memory/unified.py` / `_id.py` / `extractor.py` 还在被 reflection_cycle 测试 import (`tests/unit/test_v2_memory_unified.py` Layer 1 测试)、还有 `xmclaw/daemon/routers/memory.py` 用 `from xmclaw.memory import ...`。**移到 §7.B.4** 做（连同物理删除）。
- [x] **7.A.6 跨前后端测试** — `tests/integration/test_v2_phase7_memory_router.py` 10 个跨前后端测试覆盖 unified_query + unified_put。CLAUDE.md 硬约束达成。

### 7.B 阶段 2：后端替换（~2 周）

> **目标**：底层数据完全跑在 LanceDB，sqlite_vec + MemoryGraph 退役；用户数据一次性迁移；`memory.*` 配置段退役。

- [x] **7.B.1 补 V2 缺的 4 块能力** (commit 0adcdb8 + §7.A.2 backfill)
  - **TTL / retention** ✅：`MemoryService.sweep(ttl=, max_items=, max_bytes=)` 落地 + app_lifespan 1h 后台 loop；config 段 `cognition.memory_v2.retention.{sweep_interval_s, ttl, max_items, max_bytes}`；默认 mirror V1 `memory.retention`；procedural 层 + identity/persona_manual kinds 永久 exempt。
  - **`procedural` 层** ✅ (§7.A.2 done)：`FactLayer.PROCEDURAL` 枚举值 + sweep 不触碰。render_for_prompt 优先级提升留到 §7.B 后续 polish。
  - **temporal 索引** ✅ (§7.A.2 done)：`recall(time_range=(start, end), ...)` 参数原生支持；reflection_cycle 已切过去（§7.A.3 step 1）。LanceDB scalar index on `ts_last` 是性能优化，P2。
  - **写入原子性** ✅ (§7.A.2 done)：`MemoryServiceWriteError` 类落地；`delete()` 完整实现 vector→graph rollback；`remember()` 的原子性增强是 P2（当前 LanceDB merge_insert 已是 atomic 单步写）。
- [x] **7.B.2 数据迁移脚本** (commit fbd6d29)
  - ✅ 覆盖：lessons (已存在) + persona_manual (已存在) + **persona_bullet (新增)**
  - ✅ 显式 skip + 计数：file_chunk / code_chunk / 未知 kind / malformed
  - ✅ dry-run 是默认行为（无 --execute 就是 scan-only）
  - ✅ 自动 backup：--execute 时默认拷贝 memory.db → memory.db.pre-phase7.bak (idempotent)，可 --no-backup 关闭
  - ✅ `verify` 子命令：对比 V1 user-facing 行数 vs V2 fact count（容忍 V2 dedup collapse）
  - 9 个新测试覆盖 scan 分类 + backup 行为
- [x] **7.B.3 一次性迁移 + 切换** (2026-05-24)
  - ✅ scan 发现 149 user-facing rows (8 persona_manual + 141 generic: 107 preference / 25 procedure / 9 _no_kind_auto_extract); 2 transient curriculum_proposal 显式 skip; 2801 workspace chunks (file_chunk + code_chunk) 正确 skip
  - ✅ Backup: `~/.xmclaw/v2/memory.db.pre-phase7.bak` (33MB) — 回滚 target
  - ✅ Execute: 148/149 自动通过；1 个失败 (LEARNING.md，V2 profiles 不识别此文件名) 已用 one-shot script 救为 `lesson:project:cbfeb5e29c76` (procedural layer，sweep-exempt)
  - ✅ Verify: V2 fact count 从 1816 → 1916 (净增 100；其余被 V2 dedup 折叠，与预期一致)
  - 时长：18.8 分钟（141 generic rows × ~8s/row，Ollama embedding cold-start 拖慢首批）
- [x] **7.B.4 退役 V1 代码** (commit 70e0ee6)
  - ✅ 删 `xmclaw/memory/unified.py` / `_id.py` / `extractor.py`
  - ✅ 删 4 个 V1 测试文件（unified / unified_write / extractor / agent_loop_unified_memory）
  - ❌ **不删** `sqlite_vec.py` + `manager.py` —— 它们是 workspace 索引后端
    （`MemoryFileIndexer` 写 file_chunk / code_chunk），与用户态 fact 存储无关
  - ✅ 更新 `xmclaw/memory/__init__.py` 只 re-export V2；V1 名字 import 直接报 ImportError
  - ✅ 更新 `xmclaw/memory/AGENTS.md` + `CLAUDE.md` 反映新结构
  - 仍待做（§7.B.3 后）：删 `config.example.json` 的 `memory.*` 顶级段
- [x] **7.B.5 文档收尾** (2026-05-24)
  - ✅ JARVIS_PLAN §1.4 改写："双栈架构" → "单栈 LanceDB"，§1.4.1 改述 sqlite-vec 现在的 workspace 索引角色
  - ✅ `config.example.json` 的 `memory.*` 顶级段保留（workspace 索引仍需），但注释改写澄清 scope = 只为 workspace 索引，用户态 facts 已搬 V2
  - ✅ 附录 B 技术债务清单更新（见下方表）

### 7.C 验收标准

- [x] `grep -r "UnifiedMemorySystem\|from xmclaw.memory.unified" xmclaw/ tests/` 零结果（§7.B.4 commit 70e0ee6 文件级删除）
- [x] V1 用户态 fact 已迁出 `~/.xmclaw/v2/memory.db`（§7.B.3 commit pending；该 sqlite 文件仍服务 workspace 索引）
- [x] `config.example.json` `memory.*` 段保留作 **workspace 索引专用**（scope 已在注释中明确）；用户态 fact 配置位移到 `cognition.memory_v2.*`
- [x] cross-suite 测试 180+ 全通过
- [ ] `xmclaw doctor` 加 "memory_v1_retired" 检查项（**作为 §7.B 收尾后的 nice-to-have**，留 Phase 6 polish 处理）
- [x] 用户实测：149/149 V1 user-facing rows 全部 reach V2 (Phase 7.B.3 commit pending)

### 7.D 风险与回滚

- **R1 用户数据丢失**：迁移前自动 backup；verify 子命令 hash 比对；失败 rollback 到 `memory.db.pre-phase7.bak`
- **R2 LanceDB 性能崩盘**：阶段 1 不动后端，先观察一周 callsite 都收口后 LanceDB 单独承压情况
- **R3 callsite 漏迁**：阶段 1 末加 import-direction 检查，禁止 `xmclaw/` 下任何文件 import V1 modules

### 7.E 进度日志

- 2026-05-23: Phase 7 立项 + 元文档收口（CLAUDE.md / JARVIS_PLAN 7 节 / memory/AGENTS.md）(commit 2beb326)
- 2026-05-23: §7.A.1 V1 callsite 盘点完成 — 25 文件 9 分类，输出 7 P0 shim + 6 步迁移序 (commit 1e37d7d)
- 2026-05-23: §7.A.2 partial — 4 个低风险 shim 落地 (FactLayer.PROCEDURAL / MemoryService.delete / MemoryServiceWriteError / legacy_node_type_to_kind)，20 个新测试全通过 + 61 个老测试无回归 (commit 8163ff2)
- 2026-05-23: §7.A.2 complete — recall(time_range=...) + LLMFactExtractor.extract_candidates 落地 (commit dbe0bf1)。shim #4 (query_layer for short_term) 弃用 — reflection_cycle 改用 `recall(only_layer="working", time_range=(N hours ago, None))` 表达 V1 short_term walk 语义，保持 V2 二层模型干净。27 shim tests + 76 V2 memory regression tests 全通过。
- 2026-05-23: §7.A.3 step 1/6 — reflection_cycle 迁到 V2 MemoryService (commit 4ad31bd)。consolidate_memory 改用 deduplicate + recall(working/time_range) + remember(long_term)，删除 V1 duck-typed 钩子。16 reflection tests + 78 cross-suite regression 全通过。
- 2026-05-23: §7.A.3 step 2a/6 — AgentLoop.run_turn recall 路径切到 V2 MemoryService (commit e86d199)。capability-detect 双轨：V2 走 `<memory-recall>` 块；V1 通过 deprecated `unified_memory=` 别名仍可用（warning + 走 `<unified-recall>` 旧路径）。step 2b 别名删除待 step 6/6 测试重写后做。87 cross-suite 全通过。
- 2026-05-23: §7.A.3 step 3a/6 — hop_loop.run_hop auto-put 路径切到 V2 MemoryService (commit 6bc2705)。capability-detect 双轨：V2 走 LLMFactExtractor.extract_candidates → 多次 MemoryService.remember；V1 保持原路径。MEMORY_PUT_AUTO 事件载荷在 V2 路径下用 kind/scope 替换 node_type。52 cross-suite 全通过。
- 2026-05-23: §7.A.3 step 4/6 — /memory/unified_query + /memory/unified_put router 内部走 V2 MemoryService (commit 293afbd)。URL 保留前端兼容；返回 schema 加 kind/scope/distance，retain V1 score+matched_axes 字段。short_term 层折叠为 working；legacy node_type 经 helper 映射到 V2 kind。新增 10 个跨前后端测试 + skip 4 个老 V1 TestClient 测试（迁移到新文件）。73 passed + 4 skipped + 0 regression。
- 2026-05-23: §7.A.3 step 5/6 — factory.py 停止构造 V1 UnifiedMemorySystem + MemoryExtractor，AgentLoop 改用 memory_service=/memory_recall_top_k= 新关键字；app_lifespan 把 V2 service 挂到 agent._memory_service (新规范名) + 保留 _memory_service_v2 / _unified_memory 过渡别名 (commit ea30063)。recall_top_k 配置位移 memory.unified_recall.top_k → cognition.memory_v2.recall_top_k (旧块有 deprecation warning)。71 factory tests + 73 cross-suite 全通过。
- 2026-05-23: **§7.A.3 step 6/6 — §7.A 全部完成** (commit 31a7487)。删除 AgentLoop 的 unified_memory / unified_recall_top_k 弃用构造参数；删除 _unified_memory / _unified_recall_top_k / _memory_service_v2 self-attr 别名；删除 agent_loop recall 块 V1 elif 分支；删除 hop_loop auto-put V1 fallback；app_lifespan 单一 _memory_service 挂载；老 test_v2_agent_loop_unified_memory.py 整体 skip（§7.B.4 删除）；新增 test_v2_phase7_agent_loop_memory.py 7 个 V2 等价测试。**142 tests pass + 4 skipped + 0 regression**。§7.A 用 21 commits 收口完成，下一步进入 §7.B 后端替换。
- 2026-05-24: §7.A 全部 push 到 main（30 commits 上去）。
- 2026-05-24: **§7.B.1 完成** (commit 0adcdb8)。`MemoryService.sweep()` 实现 TTL + max_items + max_bytes 三轴 retention（mirror V1 `prune` + `evict`）；app_lifespan 加 1h 后台 sweep loop；config.example 加 `cognition.memory_v2.retention.*` 段。procedural 层 + identity/persona_manual kinds 永久 exempt。171 cross-suite 全通过。
- 2026-05-24: **§7.B.2 完成** (commit fbd6d29)。migration script 扩展：新增 persona_bullet 覆盖、自动 backup（--execute 时默认拷贝 .pre-phase7.bak）、`verify` 子命令、显式 skip 计数（file_chunk / code_chunk / 未知 kind）、--verbose 开关。9 个 scan + backup 测试通过。**§7.B.3 待用户 OK 才能跑**（涉及用户真实数据迁移）。
- 2026-05-24: **§7.B.4 完成** (commit 70e0ee6)。物理删除 V1 用户态 fact 模块：`xmclaw/memory/unified.py` + `_id.py` + `extractor.py` + 4 个老测试文件，共 2799 行减除。`xmclaw/memory/__init__.py` 只 re-export V2；V1 名字 import 直接报 ImportError。**注**：`sqlite_vec.py` + `manager.py` 不删 — 它们是 workspace 索引后端，跟用户态 fact 无关。
- 2026-05-24: **§7.B.5 部分完成** (commit 846db10)。JARVIS_PLAN §1.4 从"双栈架构"改写为"单栈 V2 LanceDB"，§1.4.1 说明 sqlite-vec 现在专门做 workspace 索引（不再服务用户 fact）。
- 2026-05-24: §7.B.3 prep — migration script 扩展覆盖 (commit d1599c1)。dry-run 发现 141 个 generic kind rows（107 preference + 25 procedure + 9 _no_kind_auto_extract）老脚本会 silently 丢；新增 `_GENERIC_KIND_TARGETS` 映射表 + _no_kind_auto_extract 救援逻辑 + V2 router 新增 `layer` 字段 + `/facts/count` 端点。+5 测试。daemon 重启加载新代码。
- 2026-05-24: **§7.B.3 LIVE 迁移成功** (no commit — touches local data only)。execute on real `~/.xmclaw/v2/memory.db`：148/149 自动通过 + 1 个 LEARNING.md 手动救援为 procedural fact。V2 fact count 从 1816 → 1916 (净增 100；其余被 V2 dedup 折叠)。耗时 18.8 min。Backup at `~/.xmclaw/v2/memory.db.pre-phase7.bak`。
- 2026-05-24: **§7.B.5 收尾 + Phase 7 ✅ 完成** (本 commit)。`memory.*` config 注释改写澄清 scope = workspace 索引专用；附录 B 加 #17 V1 退役条目；§7.C 验收 checkbox 全勾（doctor 检查项留 Phase 6 polish）。**Phase 7 历时 2 天，26 commits，约 5000 行 net 变化（含 2799 行 V1 删除）**。
- 2026-05-30: **MemoryCurator §7.F 立项 + Curator 1 落地** (commit d2f9960)。用户反馈"我要的机制，是全方位管理记忆，不只是去重"+"向量 dedup 以前也是这个，但是没用啊"。日志诊断双根因：(1) 后台 dedup 调度按"每 24 sweep"≈每天，但开发期 daemon 每 ~30min 重启使 sweep 计数器归零 → tick **从未触发**；(2) 手动 dedup O(N²) Python cosine + N 次图写在 1760-fact store 上撑爆 180s tool wall-clock，中途 abort。修复：`xmclaw/memory/v2/curator.py` 新增 `MemoryCurator` + `CurationReport`，**每个 pass 都 time-budgeted + 批处理**（检查 deadline、增量提交、干净返回），大 store 多次收敛而非 all-or-nothing 超时。Report 诚实 by construction（no-op → `honest_summary_zh()` 返回空串，守护进程保持沉默）。已实现 dedup + prune 两 pass，11 测试通过。
- 2026-05-30: **Curator 3 完成 — 接后台 + wall-clock 持久化调度 + 诚实汇报** (commit 待填)。app_lifespan 退役失效的 `dedup_every_n_sweeps`/`llm_dedup_every_n_sweeps` 两个 sweep-count tick(重启清零→从未触发的根因),改为独立 `_memory_v2_curator_loop`:warmup 后按 `check_interval_s` 轮询,但**是否该 curate 由 wall-clock 时间戳(`~/.xmclaw/v2/curator_state.json`)判定**,扛住每 30min 重启。curator.py 加 `load/save_last_curate_ts` + `is_curation_due`(corrupt→ts=0→due,never raises)。真做完(`report.did_anything`)才发 PROACTIVE_PROPOSAL 诚实汇报,no-op 静默。config schema + config.example 加 `cognition.memory_v2.curator.*` 块,shutdown 取消 task。+4 schedule 测试(23 全通过)。**至此 MemoryCurator(Curator 1–3)三件套完成:time-budgeted 不超时 + 全维度 pass + restart-proof 调度 + 诚实汇报。**
- 2026-05-31: **Phase 8 ⑦+⑪ — 三因子 recall 排序 + 召回强化** (commit cfd6d8a)。render_for_prompt 排序从"只看 relevance(cosine)"升级为 Generative-Agents 三因子 relevance+recency+importance(权重 1/1/1)。recency=ts_last 指数衰减(半衰期 7 天),importance=confidence。召回强化(MemoryBank):被注入的 query-relevant fact 后台 bump ts_last,30min 防写放大。+12 测试,732 无回归。
- 2026-05-31: **技能 §② — 轨迹→技能归纳(Voyager add_new_skill)**(本 commit)。补 XMclaw 之前缺的能力:只会改良已有技能(GEPA/ReflectiveMutator),不会从成功任务**无中生有**造新技能。新增 `xmclaw/skills/inductor.py`:`SkillInductor.induce(轨迹→LLM 判断是否值得固化+是否已被覆盖→合成 name/description/when_to_use/body)`,守卫(成功+≥2 distinct 工具+有目标)+硬去重(名字冲突)+ LLM 宁缺毋滥 skip;`trajectory_from_messages` 从 session 消息纯函数提取轨迹(目标/工具序列/结果/成功标志,识别中断 placeholder→ok=False);`write_induced_proposal` 写**未信任** `.proposed` SKILL.md(proposed_by=induction,**永不自动晋升 HEAD**,走 HonestGrader 证据门 anti-req #12,复用 skill_propose 格式)。app_lifespan 加保守后台 loop(读近期非内部会话→induce→写 proposed,wall-clock 调度复用 curator helpers,max_per_pass=1,announce 诚实告知),config `skills.induction.*`(**默认 ON**,2026-05-31 用户决定——产出永远未信任 .proposed、绝不自动上 HEAD,爆炸半径有限;保守参数 max_per_pass=1/每天/拿不准 skip)。+15 测试(守卫/合成/skip/去重/坏 JSON/SKILL.md 渲染/writer 未信任标记/轨迹提取),42 无回归。研究见 SKILL_SYSTEM_SOTA §②。
- 2026-05-31: **技能自主调用 §⑫ — 语义发现(RAG-of-tools)**(commit 0492340/7a0b870)。调研结论:agent"不会自己用技能"的根因在**发现层**——B-238 token-overlap prefilter 自述"CJK query 对英文技能描述命中归零",于是中文场景下相关 `skill_<id>` 工具根本不进 agent 工具列表→看不见→无法自主调用→退回 bash/web。修复(RAG-MCP/语义路由路线):新增 `xmclaw/skills/semantic_index.py:SkillSemanticIndex`,复用记忆系统 `EmbeddingService` 把技能描述嵌成向量(`embed_batch` 缓存,只有新增/改动描述才打 provider),每回合 embed 一次 query 做余弦;`prefilter.select_relevant_skills` 加 `semantic_scores` 参数,`token_score + 3.0×cosine` 融合 → 零 token 重叠也能凭语义过 `>0` 门(path 显式 gate 仍优先,作者意图赢)。agent_loop 在 prefilter 前算 semantic_scores(失败静默回退纯 token,无回归);config `skills.semantic_discovery.{enabled(默认 true),floor(0.30)}`。语言无关,直接根治中文漏召回。+11 测试(含核心 CJK 回归:token-only 漏掉 / semantic 召回),675 技能+agent_loop 测试无回归。调研见 docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md §⑫。
- 2026-05-31: **Phase 8 ⑨ — 写入时 Mem0 式 ADD/UPDATE/DELETE/NOOP 决策**(commit 3fbb970,根因级)。Mem0(arXiv:2504.19413)路线:新增 `MemoryService.remember_with_decision()` —— 对新 fact 检索最近邻(同 scope,cosine 距离 ≤0.40 才算"相关"),让 LLM 选 ADD(全新)/ UPDATE(并入更完整的已有 fact,旧的 supersede)/ DELETE(与旧 fact 矛盾→旧的打 invalid_at 时间失效保留,新的照写)/ NOOP(已知→证据投票)。**成本可控**:只有存在相关邻居才调 LLM,孤立 fact 直接 ADD 零额外开销;无 LLM/embedder 时安全回退到 plain remember()。wire 进 hop_loop 后台抽取路径(不碰同步 regex 路径,零用户延迟),config `cognition.memory_v2.write_decision.enabled`(默认 true)门控。+7 测试,719 无回归。**这是治"1760 条堆积"的根因——管理前移到写入时,curator 退为兜底。Phase 8 动态层四缺口(⑦/⑩/⑨ + ⑬待办)补齐三个。**
- 2026-05-31: **Phase 8 ③+⑩ — Fact 双时间轴 valid_at/invalid_at + 时间失效** (commit 73d5882)。Zep/Graphiti 路线:Fact 加 valid_at/invalid_at(现实有效区间,区别于 ts_first/ts_last 系统时间);0.0 sentinel↔None。LanceDB schema + _MIGRATIONS 迁移(老表 add_columns 默认 0.0);inmemory backend Fact 原生存储。recall 默认过滤 invalid_at≤now 的(post-filter,backend 无关),include_invalidated=True 可取历史。矛盾**不删除**:supersede() + curator 矛盾 pass 改成给**较旧**的一方打 invalid_at(newer wins),保留为历史("2 月喜欢咖啡/5 月戒了"两条都对只是区间不同)。+6 测试,772 无回归。
- 2026-05-30: **记忆系统全栈调研** (docs/audit/MEMORY_SYSTEMS_SOTA_RESEARCH_2026.md)。读 Mem0/Zep-Graphiti/MemGPT-Letta/Generative-Agents/A-MEM/HippoRAG/MemoryBank 真实源码+论文,把记忆系统拆 13 层逐层对照 XMclaw 真实代码。结论:静态层(存储/索引/类型/注入)已头部水平;短板全在**动态层**——⑨ 写入时不做 UPDATE/DELETE(堆积根因)、⑩ 矛盾不用时间失效、⑦ 召回不算 recency/importance、⑬ 无 benchmark 量化。开 **Phase 8** 补这四处。
- 2026-05-30: **Curator 2 完成 — 矛盾检测 + 语义结晶** (本 commit)。`_detect_contradictions_scope`：LLM 扫描同 scope facts 找**直接逻辑冲突**对（X 说 A / Y 说 非 A），双向写 CONTRADICTS 边 + stamp `contradicts` 字段 + 把低置信侧 floor 到 ≤0.4（不删除——矛盾是信息"用户改主意了"，两条都留，只降权 stale 侧）。`_crystallize_scope`：LLM 把多条同主题碎片 facts **合成一条更清晰的规范表述**（区别于 dedup 合并重复：结晶是从碎片提炼更好的单句），新 fact 写入后把碎片 supersede 上去。两 pass 都 deadline-aware + dry-run 安全 + bad-JSON 降级为 no-op。+8 测试（19 全通过），mirror `llm_dedup_scope` 的 _FakeLLM 模式。下一步 Curator 3：替换 app_lifespan 失效的 sweep-count tick，wall-clock 持久化调度（last_curate_ts 落盘扛重启）+ 真做完才诚实主动汇报。

---

## Phase 9: GUI 时代（生成式 UI 双向化 + Computer-use 闭环）

> **状态**: 🟡 进行中 (2026-06-11 起)
> **目标**: 把两个已有半成品推到完全体——(a) Canvas 生成式 UI 从"单向展示"升级为"双向交互"（agent 出 UI → 用户操作 → agent 收到结构化输入）；(b) Computer-use 23 件散装工具补上视觉接地循环 + 安全闸 + 可观测性，成为可靠的"操作任意 GUI"能力。两线在 M3 汇合：computer-use 执行过程用 interactive canvas 实况呈现。
> **时间**: M1 约 1-2 天；M2 约 3-5 天；M3 约 2-3 天
> **风险**: 中（M1 低——纯增量不动现有 5 种 kind；M2 中——鼠标键盘是高危工具，安全闸设计错误会放大爆炸半径）
> **前置**: 无（独立于 Phase 1-6 残留项）
> **决策记录**: 2026-06-11 用户拍板"两个都要"（生成式 UI + computer-use 双线推进）。现状盘点：`builtin_canvas.py`（canvas_create/update/close，5 kinds）+ `CanvasArtifact.js` 渲染链路已在；`computer_use.py` 2800 行 23 工具（OCR/UIA/图像匹配/窗口管理）已在。缺口不在"有没有"，在"闭环没闭"。

### 9.0 现状诊断（why this Phase exists）

- **生成式 UI 是单向的**：`HtmlView` iframe `sandbox="allow-scripts"` 无 postMessage 桥——agent 生成的表单/按钮/滑块点了之后无处可去。Agent 想让用户做选择只能打字问。
- **Canvas 渲染依赖 CDN**：mermaid / Chart.js 从 esm.sh 现拉，违反 local-first 原则，断网时 canvas 半残（`vis-network` 已 vendor，是正确先例）。
- **Computer-use 有手有眼缺脑内闭环**：定位全靠 OCR/模板匹配/UIA，对非标准控件无解；无"截图→视觉模型给坐标→动作→再截图验证"的标准循环；无按动作分级的确认机制（点击/输入应需放行，截图可随意）；用户在 Web UI 看不到 agent 正在看什么、点什么。

### 9.M1 Canvas 双向化（生成式 UI 闭环）

- [x] **9.M1.0 断链修复（开工时发现）**：现役 nebula 渲染器 `MessageList.js` 从未渲染 `message.canvasArtifacts`——canvas artifact 渲染只活在没人 import 的死代码 `MessageBubble.js` 里（nebula 改版漏迁），canvas_create 的产物从未在现役 UI 显示过。已接回（含空鬼泡守卫豁免）。
- [x] **9.M1.1 postMessage 回传桥**：`HtmlView` srcdoc 注入 `window.xmclaw.sendPrompt(text)` / `window.xmclaw.submit(data)` 桥；父页面以 `e.source === iframe.contentWindow` 配对校验（sandbox 维持 allow-scripts、不开 allow-same-origin），经 `sendCanvasAction`（composer_actions）转成 WS 用户消息发回 agent（带 artifact 上下文）。`canvas_create` 的 html kind 工具描述同步更新，教 agent 桥的用法。
- [x] ~~**9.M1.2 `canvas_ask` 工具**~~ **取消（2026-06-11）**：盘点发现 `ask_user_question` 工具 + `QuestionCard` + `answer_question` WS 帧 + Future 阻塞续跑（B-92）已完整覆盖"提问型 UI"，不重复造轮子。
- [x] **9.M1.3 CDN 资产 vendor 化**：mermaid (3.3MB UMD) + Chart.js (207KB UMD) 进 `static/vendor/`，新建共享加载器 `lib/vendor_loaders.js`（本地优先，esm.sh 兜底）；`CanvasArtifact.js` / `cognition_task_dag.js` 收口到共享加载器；断网渲染可用。
- [x] **9.M1.4 跨前后端测试**：`tests/unit/test_v2_phase9_canvas_bridge.py` 8 个 TestClient 端到端测试（断链回归 / props 链 / 桥注入+source 校验+sandbox 安全 / 工具描述 / vendor 资产可达 / 无 esm.sh 直连），挂入 tools + ui 两个 lane。另经浏览器实测：桥双向消息到达（send_prompt + submit 带 artifact 上下文）、vendor mermaid 本地出 SVG、vendor Chart.js 构造器可用、零控制台错误。

### 9.M2 Computer-use 闭环

- [x] **9.M2.1 视觉接地循环**：screen_capture 的截图本就以 vision block 进下一轮（看的能力已在）；本步补的是坐标系——`_ensure_dpi_aware()` 进程级 DPI 感知（Windows 显示缩放下 mss 物理像素 vs pyautogui 逻辑坐标错位 = "点不准"根因），screen_capture 结果回报 `pyautogui_size` + `click_scale` 双保险；mouse_click 描述写入接地循环纪律（截图→读图定位→动作→验证）。
- [x] **9.M2.2 动作分级安全闸**：开工诊断发现安全层对 computer-use **空转**——`utils/security.py` 的 TOOL_CATEGORIES 分级表不在调用链上（无人查）、`_DEFAULT_GUARDED_TOOLS` 不含任何 computer-use 工具、规则型 guardian 对 `mouse_click {x,y}` 这类参数永远零 finding。新增 `ComputerUseActionGuardian`（按动作性质分级：读取类零 finding 放行；操作类按 `security.guardians.computer_use_mode` 出 finding——allow 默认放行 / approve→HIGH→NEEDS_APPROVAL 单次确认 / deny→CRITICAL 拒绝），MUTATING_GUI_TOOLS 名单并入 engine guarded 集合，factory 接线。默认 allow 的理由：provider 本身默认关闭（开启已是显式授权）+ channel 自动化（gui_send_chat）在 approve 下会卡 pending 没人批 + 每次调用有 TOOL_CALL/TOOL_RESULT 事件留痕。**要武装闸门 = 配置一行翻 approve**。
- [x] **9.M2.3 失败重试策略**：mouse_click / click_on_text 新增 `verify_text` / `verify_timeout_s`——动作后轮询 OCR 等成功信号，结果带 `verified: true/false`（false 时附屏上实读样本 + "重新截图再决策"提示），把"点了但没生效"从静默继续变成显式信号；无 OCR 引擎时降级为 `verify_skipped` 不碍动作本身。

### 9.M3 汇合：computer-use 实况面板

- [ ] **9.M3.1 实况 artifact**：computer-use 会话期间自动维护一个 interactive canvas artifact：屏幕缩略图流 + 动作轨迹标注 + 紧急停止按钮（走 M1 回传桥）。

### 9.V 验收标准

- [x] ~~Agent 能用 `canvas_ask` 出选项卡片~~ → 既有 `ask_user_question` + QuestionCard 已覆盖（9.M1.2 取消）
- [x] Agent 生成的 html artifact 里的按钮/表单能经 postMessage 桥把数据发回 agent（端到端测试 + 浏览器实测覆盖）
- [x] 断网状态下 mermaid / chart / table / svg / html 五种 kind 全部正常渲染（渲染器零 esm.sh 直连，vendor 本地加载实测出图）
- [ ] Computer-use 在非 UIA 应用上能凭视觉接地完成"找到并点击"任务；每个鼠标/键盘动作有事件审计记录
- [ ] Web UI 能实时看到 computer-use 的屏幕与动作轨迹,且有紧急停止

### 9.L 进度日志

- 2026-06-11: Phase 9 立项（用户拍板双线推进）。现状盘点完成：canvas 三工具 + 前端渲染链已在但单向；computer_use 23 工具已在但散装。M1 开工。
- 2026-06-11: **9.M1 完成**（本 commit）。断链修复（MessageList 接回 canvasArtifacts）+ postMessage 双向桥（window.xmclaw.sendPrompt/submit → sendCanvasAction → WS 用户消息）+ canvas_ask 取消（ask_user_question 已覆盖）+ mermaid/Chart.js vendor 化（共享 vendor_loaders.js，本地优先 CDN 兜底）。8 个跨前后端测试 + 浏览器实测全绿（桥消息双向到达、本地 mermaid 出 SVG、零控制台错误）。注：test_v2_ui_scaffold 两个失败为存量问题（HEAD 同样失败：8 个文件超 500 行预算 + MessageBubbleParts 双反引号），与本次无关。下一步 9.M2 computer-use 闭环。(commit a14e3d5)
- 2026-06-11: **9.M2 完成**（本 commit）。视觉接地（DPI 感知 + click_scale 坐标回报 + 接地循环纪律进工具描述）+ 动作分级安全闸（ComputerUseActionGuardian，allow/approve/deny 三模式，guarded 集合补全——修复"安全层对 computer-use 空转"）+ 动作后验证（verify_text → verified true/false 显式信号）。16 个新测试 + 150 个 security/factory 回归全过。**修复 broken HEAD**：commit ce7a172 在 factory/engine 引用了 computer_use_guardian 却漏 `git add` 该新文件（fresh clone ImportError），本 commit 补上。下一步 9.M3 computer-use 实况面板。

---

## Phase 10: Mission Control（Web UI / TUI 整体重设计）

> **状态**: 🟡 进行中 (2026-06-11 起)
> **目标**: 把"聊天网页"形态的 Web UI / TUI 重设计为任务中心 Mission Control——任务取代 session 成为一等公民，执行过程（工具/计划/审批）是主舞台，对话降级为指挥通道，工作区（Diff/文件/终端/预览）常驻右栏。Web 与 TUI 同构，共用 WS 协议与事件→条目映射。
> **设计规格**: [docs/MISSION_CONTROL_DESIGN_2026.md](MISSION_CONTROL_DESIGN_2026.md)（信息架构 / 任务聚合模型 / 事件映射表 / 技术栈 / ADR-010）
> **时间**: M1 约 3-5 天；M2 约 5-7 天；M3 约 3-5 天；M4 约 3-4 天
> **风险**: 高（全量 UI 重做；上一轮尝试 ce7a172 因"效果不好"被 cd528c4 整体 revert）
> **前置**: 无（Phase 9 M3 实况面板可在新 UI 上实现，反向不阻塞）
> **决策记录**: 2026-06-11 用户拍板三项：① 核心形态 = 任务中心 Mission Control；② 技术路线 = 引入 Vite + React + TS 现代构建链（**ADR-010 取代 ADR-001/002 "无 Node 构建"约束**，缓解：产物提交进 git/PyPI，最终用户仍零 Node，运行时零 CDN）；③ TUI = Textual 全屏应用（重建既有 `xmclaw/tui/` 骨架）。流程级修正：每个里程碑先给用户看真实运行效果、认可后再进下一步。

### 10.M0 设计冻结

- [x] **10.M0.1 设计文档**：docs/MISSION_CONTROL_DESIGN_2026.md（诊断 / 原则 / 三栏信息架构 / 任务聚合模型 / 事件→时间线映射 / 20+ 页四域收编 / 技术栈 / TUI 规格 / ADR-010）。
- [x] **10.M0.2 约束更新**：CLAUDE.md 标注 Phase 10 构建链决策与 webui/ 工程；`static/AGENTS.md` 加 deprecation 指针。
- [x] **10.M0.3 视觉方向认可**：2026-06-11 用户确认 Web 三栏 + TUI 设计稿方向（"Mission Control 这个设计方向我认可"）。

### 10.M1 Web 骨架

- [x] **10.M1.1 脚手架**：`webui/` Vite 6 + React 19 + TS + Tailwind 4 + zustand（shadcn/ui 推迟到 M2 组件需要时引入）；构建产物 → `xmclaw/daemon/webui_dist/`（提交进 git，gzip ~70KB）；daemon 挂载 `/ui-next/`（SPA fallback + 哈希 asset immutable 缓存）；`vite dev` 反代 `/api` + `/agent` WS。
- [x] **10.M1.2 数据层移植**：`webui/src/lib/{ws,api,reducer}.ts` + `store/app.ts`——重连补发/队列冲洗（B-13）、seq 去重、取消回合守卫（B-269）、工具事件乱序竞态（B-267）、call_id 键名（B-232）、弃流收尾（B-89）、多 hop 不清 pending、截断 finalText 不覆盖流式文本、历史水化（B-60）、pending question 恢复（B-99）、空鬼泡渲染守卫（B-220 对位）全部按语义移植；WS 协议零改动。
- [x] **10.M1.3 任务聚合 router**：`routers/tasks.py` `GET /api/v2/tasks` 只读聚合（SessionStore × bus.query plan/todo/审批/llm 事件 → 状态启发式推导）；bus 不可查询时优雅退化为 chat 态列表；不动 AgentLoop。
- [x] **10.M1.4 三栏布局**：HUD（模型/成本/记忆/连接态）+ 任务栏（状态徽章，/api/v2/tasks 404 时退化本地 sid 列表）+ 计划步骤条（plan_*/todo_updated 双源）+ 活动时间线（用户/陈述/折叠思考块/通用工具卡/内联审批卡）+ 指挥通道（Enter 发送/Esc 打断）+ 工作区四标签骨架（文件树已接 session_workspaces API）；15 个跨前后端测试（_derive 启发式 ×10 + TestClient 真实 URL ×5）。
- [x] **10.M1.5 CI**：python-ci.yml 新增 `webui-build` job（npm ci + build + git diff 校验 dist 与源码一致）；ui lane 收编 webui/** + webui_dist/** + routers/tasks.py。

### 10.M2 执行视图

- [x] **10.M2.1 活动时间线**：markdown 全功能渲染（GFM 表格/代码高亮——治"表格糊成管道符"反馈）+ 思考块折叠 + worker/subagent 可折叠执行组（对位 Claude Code Agent 组）+ 注入块剥离（session-workspace/memory-* 不再当用户消息显示）。安全条目（prompt_injection 等）待补。
- [x] **10.M2.1b 工具卡类型特化渲染**：file_write/apply_patch → 内联红绿 diff 卡（jsdiff 现算 + 行号 gutter + `+N −M` + 中段折叠 + 预览切换 + "在工作区查看"联动右栏）；bash → 终端卡（$ 命令头 + 输出折叠）；file_read/glob/grep/web_search 等 → 单行摘要卡；带截图结果 → 缩略图条；其余兜底 JSON 卡。实测：pomodoro.html +8−0 / LEARNING.md +2−6（apply_patch 混合 diff）/ hello.md 全部正确渲染。TUI rich.syntax 降级 → 10.M4。
- [x] **10.M2.2 计划步骤条**：M1 已落（plan_*/todo_updated 双源）。
- [x] **10.M2.3 内联审批**：M1 已落（审批卡 → answer_question 帧）。
- [x] **10.M2.4 工作区四标签 + 实时预览深度融合**（2026-06-12 用户点名主轴）：预览 tab = canvas artifact 沙箱渲染（canvas_artifact_* 事件驱动）+ 工具截图视觉流（liveShots 封顶 12 帧实时滚入）+ "跟随 agent"模式（新产物自动切预览，文件变更亮 Diff 角标）；Diff tab = commits 时间线 + unified diff 解析着色，workspace_file_changed 自动刷新；文件 tab = 树 + 点击查看（md 渲染/代码原文/图片走 /raw）+ 时间线联动聚焦；终端 tab = bash 输出聚合流（偏差：未用 xterm.js——聚合流已覆盖需求，xterm 留给真 PTY 场景）。
- [x] **10.M2.5 收尾**：安全事件红条（anti_req_violation 终止回合语义 + prompt_injection 警示条）✓；code-split（Markdown chunk 懒加载，主包 gzip 179KB→78KB）✓；canvas 真场景实测 → **揪出两个后端真 bug 并修复**：① `factory._canvas_listener` 在工具 executor 线程上 `asyncio.create_task` 抛 RuntimeError 被吞，CANVAS_ARTIFACT_* 从未发布（构建期捕获主 loop + `run_coroutine_threadsafe` 交回，channel adapter 同模式）；② `system.py health_check(request: "Request")` 字符串注解未 import，FastAPI 当 query 参数 → 端点永远 422（回归测试已锁）。canvas 前端链路待 daemon 重启后复测。

### 10.M3 收编与切换

- [x] **10.M3.1 四域收编（首轮）**：左栏底部四域导航；记忆域（overview 读数条 + kind 分布 + facts 关键词检索，实测 63 事实/长期 10/工作 52）；能力域（技能清单 + 进化 arms 晋升进度条，实测 87 技能/5 arm/2 待晋升）；系统域（health checks 卡 + 日志尾巴，503 degraded body 也渲染）；域页全部懒加载 chunk。深度操作（fact 编辑/技能回滚/配置写入）留 10.M3.1b 迭代。
- [ ] **10.M3.1b 域页深化**：fact 钉选/遗忘/修正操作；技能版本历史与回滚；系统域收编 doctor/备份/配置只读；Cron/Trace 入任务域。
- [x] **10.M3.2 切换**（2026-06-13）：`/ui/` 已切到新 Mission Control（webui_dist），旧 Preact UI 退至 `/ui-legacy/`，`/ui-next/` 保留为兼容别名，`/` → `/ui/`。CLAUDE.md + static/AGENTS.md 标 RETIRED（保留一个 tag 周期后删）。SystemView"旧 Settings"链接改指 `/ui-legacy/settings`。跨前后端测试覆盖四路径（test_ui_switchover_primary_and_legacy）。

### 10.M4 TUI（Textual 重建）

- [ ] **10.M4.1 布局**：HUD + 任务列表 + 计划条 + 活动时间线 + 指挥通道；Diff/文件 modal screen。
- [ ] **10.M4.2 审批快捷键**：y/a/n → `answer_question` 帧；[Esc] 打断。
- [ ] **10.M4.3 废除 QUIET_MS**：回合终止改事件语义判定（与 web reducer 共享逻辑）；`--plain` REPL 同步换判定；textual 收进 `xmclaw[tui]` extra。

### 10.V 验收标准

- [ ] 打开 `/ui/` 第一眼是任务列表与执行状态，而非聊天记录（P1）
- [ ] 一次多工具长任务中：计划步骤实时翻转、工具卡流式出现、审批内联可点、Diff 在右栏实时亮起——全程不需要切页面
- [ ] file_edit 工具卡内联展示语法高亮 diff（行号 + `+N −M` + 红删绿增 + 预览切换），达到 Claude Code 同级观感（§2.3.1）
- [ ] 断网/重连后：队列消息冲洗、历史水化、pending 审批恢复（与旧 UI 行为对照零回归）
- [ ] 最终用户 `pip install xmclaw` 后零 Node、零 CDN 运行新 UI
- [ ] `xmclaw chat` 进入 Textual 全屏：任务/时间线/审批快捷键可用，回合终止不再依赖静默期猜测
- [ ] 每里程碑有用户视觉认可记录（进度日志留痕）

### 10.L 进度日志

- 2026-06-17: **走偏收口续：agnes hub 模型拉取提速 + embedding profile 接线 + 指纹守卫**（用户："还有哪些走偏的；都查"）。① **agnes hub「0 模型/拉取失败」诊断**：直接打 `apihub.agnes-ai.com/v1/models` 其实返回标准 `{"data":[...]}`(5 模型)、后端解析也对；真因是 discover 端点在热路径 `await warm_cache()`——OpenRouter 整库(336)冷刷新 ~8s 阻塞响应，首次"从供应商获取"挂 ~9s。改 fire-and-forget(后台 warm、当前用已缓存/启发式)，discover 立即返回。顺清同文件 pre-init ruff(F401 `_default_model_for`、F841 `exc`)。诚实：未复现"0 模型"硬失败，冷缓存 9s 卡顿是确定 bug、最可能元凶。② **embedding profile 接线**(走偏#2)：`build_embedding_provider` 无显式 `evolution.memory.embedding` 块时回退到标 `embedding` 能力的 LLM profile(显式 cap 或名字启发式 bge/text-embedding/nomic-embed/…)，云端或本地 Ollama 端点皆可——此前标了 embedding 的 profile 无人消费、语义记忆静默退化关键词。**用户定调：不内置本地向量模型(不拉 fastembed/torch)，只接 profile/Ollama**。维度不猜(只在 profile 显式给时传 dimensions)。③ **索引重建保护**：`EmbeddingProvider.fingerprint`(name:model:dim) + 独立 `embedding_guard.py`(sidecar `.embedding_fingerprint`，fresh/match/mismatch 三态，mismatch 不覆盖→告警持续到重建)；app_lifespan embedder 构造处一处校验，模型/维度变了响亮 WARNING(不删索引、不阻塞 boot)。④ 顺手收 app_lifespan 死代码 `_HOME_PATH`(从未定义、`in globals()` 守着恒 None) F821。121 embedding/discovery/memory/factory 测试全绿，ruff + import 方向通过。

- 2026-06-17: **媒体后端按「协议」分派 + MiniMax 全栈 + audio_out 远程 TTS + 超时/延迟修复**（用户："所有视频/生图/语音模型都适配，双端点(提交+轮询)，minimax 系列；学 peers 怎么做"）。① **根因**：`generate_video` 后端原只支持 Replicate，用户的火山 doubao-seedance（OpenAI-key 异步任务 API）不接线 → 模型退而用 `skill_browse` 造静态视频；生图 `Dalle3Provider` 发 DALL-E 专属 `quality/style`，Seedream-on-compat 会 400；`audio_out` profile 完全没接线（只有免费 EdgeTTS）。② **学 peers（OpenClaw/Hermes）= 按协议不按厂商**：媒体生成只有 3 种线协议（OpenAI 兼容同步 / 异步提交+轮询 / 原生同步信封）。新增 `utils/vendor_detect.py`（共享厂商检测，media+voice 共用，符合 import 方向）+ `media/dispatch.py`（`build_image/video_backend`）+ `voice/dispatch.py`（`build_tts_backend`）——新增厂商=加一条分支，不动 factory。③ **后端**：视频 `ark_video.py`（火山方舟提交+轮询）/`minimax_video.py`（MiniMax 3 步 create→query→files/retrieve）/`replicate_video.py`；图片 `openai_compat_image.py`（原 ark_image 改名，`watermark` 改可选避免给纯 OpenAI 端点发未知字段 400）/`dalle3.py`/`minimax_image.py`（`data.image_urls`）；TTS `minimax_tts.py`（`/t2a_v2` hex 解码）/`openai_tts.py`（`/audio/speech`）。④ **audio_out 接线**：`_scan_media_profiles` 加扫 `audio_out`，factory voice 段远程 TTS 优先、EdgeTTS 兜底；火山 seed-tts 原生二进制协议返回 None→落回 EdgeTTS（注明缺口）。`model_capabilities.json` 补 MiniMax 家族（hailuo/image-01/speech-0x/t2v-/i2v-）。⑤ **顺手修 hop_loop 两个 bug**：流式完成超时由「总时长硬上限(600s)」改「停顿/空闲超时」（`_STREAM_STALL_TIMEOUT_S=120` + `_STREAM_HARD_CAP_S=1800`）——正在逐 token 吐大文件不再被误杀；first-token guard 由死等 `_ft_event` 改 `_ft_event`/`_done_event` 竞速——纯工具调用响应不再空等 60s（整套 agent_loop 测试 280s 超时→21s）。⑥ **清理既有问题**：dalle3 未用 import、test F821 `Any`、过时 instant-mode 测试、过时 max_hops(40→100) 测试。182 媒体/语音/factory/agent_loop/timeout 测试全绿，ruff + import 方向通过。**诚实状态**：MiniMax/Ark 后端按官方文档形态实现，未用真 key 验证。⑦ **走偏扫描补 audio_in 远程 STT**（与 TTS 对称的另一半）：`openai_stt.py`(OpenAICompatSTT, `/audio/transcriptions` multipart) + `build_stt_backend`，`_scan_media_profiles` 加扫 audio_in，远程优先、本地 Whisper 兜底。STT 闸门**保守**：仅模型名含 whisper/transcribe/asr 才接远程——多模态聊天模型(gpt-4o-audio)虽带 audio_in 但不是转写端点，不误接。MiniMax/Volcengine ASR(原生文件上传+轮询)仍走本地 Whisper。**另一处疑似走偏(未改)**：`embedding` 能力的 profile 不被 `build_embedding_provider` 消费(只读 evolution.memory.embedding 配置块)——但 embedding 模型一换会让向量索引失效，自动接线有风险，留作显式决策。

- 2026-06-15: **顶级 harness gap 收口批次（用户点名 #1/2/3/6/7）**。对照 Claude Code/Codex/aider/OpenHands 做实证 gap 分析后，逐个补纵深（每项独立 commit + 测试）：
  - **#3 编辑可靠性**（commit c1438d8）：`apply_patch` 精确匹配失败时加 whitespace 容忍回退（整行匹配忽略尾随空白 + LF/CRLF，重锚到文件真实文本，唯一命中即套用，多块歧义中止）+ per-edit `replace_all`。根治"old_text not found→重复同一 stale 编辑到 max_hops"。`_ws_tolerant_spans` helper，5 新测试。
  - **#7 子代理嵌套**（commit 7389fab）：`SubagentToolProvider` 由扁平（嵌套硬阻断）改为有界递归——`max_depth`（默认 2），depth 贯穿 _fanout/_run_one/_do_run_one，嵌套走 `_run_nested_fanout`，独立 semaphore 防父子争用死锁；深度 cap + 并发 + wall-clock 三重防跑飞。3 新测试。
  - #1 Steering / #6 Trace 导出回放 / #2 Checkpoint-rewind：进行中。

- 2026-06-15: **能力路由收口——生成走工具，模型切换只留视觉**（用户对"3 种模式"提问时点出的语义混淆）。澄清+修复一个潜在 bug：`hop_loop._CAPABILITY_BY_TOOL` 和 `SubagentToolProvider._SUBTASK_CAPABILITY_HINTS` 原本把 `generate_image→image_gen`/`generate_video→video_gen`/`speak→audio_out` 也纳入"下一 hop/子代理切换聊天模型"。但生成模型（DALL-E/seedream/seedance）是**生成端点不是聊天模型**——切过去再 `complete()` 会把 chat 请求喂给只做 `images.generate` 的模型，破坏该 hop。修复：两处映射只保留 `vision`（视觉模型仍是聊天模型，截图后切过去解读图像是合法的）；子代理新增 `_NON_CHAT_CAPABILITIES` 守卫，显式 `specialist_models` 传 image_gen/video_gen/audio_out 时**不**切 `.llm`（让子代理用正常聊天模型去 CALL generate_image/video 工具）。生成的唯一真实路径 = 工具调配置后端；模型切换只服务视觉。工具描述同步更正。72 subagent/hop/media/capability 单测通过。

- 2026-06-15: **Stop / 追加指令 真正打断后端**（用户："停止按钮和追加指令只是前端打断，不是真正的后端打断，刷新页面后依旧执行"）。根因：WS 串行循环 `await run_turn` 是**内联**的——① Stop 的 cancel 帧只设协作 `cancel_event`（hop 边界 + 工具 race 才检查），卡在长 LLM 调用时不生效；② 新消息（追加指令）排在 `_frame_q` 里、被内联 run_turn 阻塞，"取消旧回合"代码等旧回合结束后才跑 = 形同虚设；③ 刷新只断前端 socket，后端回合照跑。修复：把回合改成**可取消 task**，注册到 session 级 `active_turn_tasks`（任何连接——含刷新后的新连接——都能够到并取消）。`_hard_cancel_turn` 两段式：先发协作 `cancel_event`（工具 invoker 已 race，干净拆除工具），150ms 宽限后若仍卡住（如不理会 cancel 的 LLM 流）再 `task.cancel()` 硬中断。reader 收到新 user 帧时立即硬取消旧回合（追加指令真抢占）。无论协作正常返回还是硬取消 CancelledError，用 `active_turn_cancelled` 标志确定性发一条 `turn_cancelled`；前端 reducer 新增 `session_lifecycle` 分支据此收尾 pending 气泡 + 残留工具卡。2 个新端到端测试（15s 睡眠工具被 Stop 在 ms 级打断、新消息抢占旧回合）通过；既有 question 类 WS 测试 + cognitive_daemon 测试在本机预先就 hang/fail（stash app.py 验证非本次引入）。

- 2026-06-15: **生图/生视频工具接线（能力路由收尾）**（用户："主模型不会生图，但我配了生图模型，让他生图时应能调用生图模型"）。前序会话已搭好能力路由骨架（`_pending_capability_pick` + `LLMRegistry.pick_by_capability` + SubagentToolProvider 关键词→capability 自动选 specialist + 媒体后端 Dalle3Provider/ReplicateVideoProvider + 工具壳 GenerateImage/VideoToolProvider），但**工厂从未构造/注册这两个工具** → LLM 调 `generate_image` 因工具不存在而失败。本次补齐工厂接线：`_scan_media_profiles` 扫描 profiles 找带 `image_gen`/`video_gen` 能力（显式标签或名字启发式）的模型并解析 api_key/base_url/model（密钥解析对齐 build_llm_profiles：inline→secret→legacy）；`_build_media_tool_providers` 据此构造后端——image 走 OpenAI-images 兼容（Dalle3Provider，支持任意 base_url，覆盖 DALL-E + 兼容端点），video 走 Replicate（profile 指向 replicate 或 `media.replicate` 配置块）；只有后端解析成功才注册工具（无图模型→`generate_image` 不出现，目录干净）。`model_capabilities.json` 补 seedance/doubao-seedance→video_gen、seed-tts/cosyvoice→audio_out、doubao-seedream/gpt-image→image_gen。生成产物经 `metadata.attachments` → `normalize_attachments` → `_ensure_servable` 复制进 uploads 渲染。**回退**前序会话夹带的 `agent.max_hops` 默认 40→100 改动（与本特性无关、翻倍 hop 预算有跑飞/成本风险、破坏 2 个测试）。9 个新接线测试 + 101 factory/media 测试全绿。**诚实状态**：用户当前配置只有 doubao-seedance（volces 异步视频 API，非 Replicate/OpenAI-images 形状）→ 暂无对应后端，video 工具不接线；且无图模型 → `generate_image` 要等用户加一个 OpenAI 兼容图模型才出现。volces/doubao 专用视频后端为后续。

- 2026-06-15: **三连用户实测修复（深思真连后端 / 视觉误报 / 文档附件裂图）**。① **深思 ultrathink 真连 extended_thinking**（用户："我要的是真的连接后端，调整 think，不是单单加提示词"）：原先 `ultrathink` 只到 run_turn 拼提示块（agent_loop:2778），`_run_hop_loop` 调用没透传 → 永远到不了 LLM。本次把 `extended_thinking: bool | None` 按回合参数贯穿 base/openai/anthropic 三个 `complete_streaming` 签名 + hop_loop LLM 调用点（`extended_thinking=ultrathink or None`）+ `_run_hop_loop`/agent_loop 调用链；anthropic provider per-call override（深思 budget 10000，profile 默认 5000），并修 `max_tokens` 必须 > `budget_tokens` 约束（原 8192 < 10000 会 400）。② **视觉误报根因**（用户："它有视觉能力，为什么会提示没有视觉"）：agent 截图后自述"模型看不到图像，用 OCR"——根因是 `openai.py::_model_supports_vision` 保守 allow-list 认不出第三方端点 slug（agnes-2.0-flash），translator 把图像降级成 `[图片 ×N（当前模型不支持图像，未传入）]` 占位 → 模型确实没收到图。修复：profile 配置 `supports_vision` 显式覆盖启发式（贯穿 OpenAILLM/OpenRouterLLM 构造 + factory 两条加载路径 + profiles 路由持久化 + GET 回报），并**统一两套 vision 语义**——factory 中 `caps_set` 含 "vision"（能力标签或 tier）即隐含 `supports_vision=True`，使既有 Phase 11 能力标签 UI 直接兼任视觉开关；ChannelEditor 模型 chip 加 👁 视觉开关。agnes profile 实测 GET 回 `supports_vision: True`。③ **send_media 文档裂图**（xlsx 显示"加载失败"）：builtin_user 按扩展名分类 kind（document/image/video/audio，原 unknown 默认 image → 前端渲染坏 `<img>`），hop_loop 增 `documents` 事件通道，reducer + ToolCards 渲染文件下载 chip（按 mime emoji）。283 vision/factory/profiles + 56 media/hop 测试全绿，webui 构建通过，daemon 重启验证。

- 2026-06-13: **定时任务视图（Cron）**。CronView：列出定时任务（名称/schedule/下次运行/运行次数/启用态/最近错误）+ 立即运行/暂停/恢复/删除（hover）+ 新建表单（名称/周期/指令）。数据 GET/POST /api/v2/cron + /{id}/{pause|resume|trigger} + DELETE。挂系统域第三子标签（健康日志 / 模型管理 / 定时任务），补上任务栏"定时·后台"一直点不开的缺口。端点在运行中 daemon 即时可用（无需重启）。实测空态正确渲染。

- 2026-06-13: **认知域二级页**（XMclaw 自主灵魂进新 UI）。CognitionView：当前目标 / 注意力焦点（含 salience %）/ 自主任务 / 疲劳度 / 显著性阈值+容量读数，数据来自 /cognition/state + /cognition/tasks；cognition.enabled=false 时端点回 503 {reason,how_to_enable} → 渲染启用引导而非空白。作为"能力"域子标签（技能/进化 ↔ 认知/自主），不破坏四域导航。实测吃到真实感知流（clipboard/screen/window 变化 7 个焦点 @55% salience）。33 测试绿。

- 2026-06-13: **Proma 式模型配置 UI**（用户出 Proma 截图为标杆）。把一次性"发现→注册"的 ModelDiscoveryView 重做为两级管理：①渠道列表（profile 按 provider+base_url 分组成"渠道"，每渠道一卡：provider 图标/名称/模型计数/启用开关；Agent 供应商分区）；②渠道编辑器（供应商类型/名称/Base URL+预览/API Key+测试连接+眼睛/启用此渠道/已启用模型 chips/可用模型从供应商获取+手动添加 ID+显示名）。**后端新增 profile `enabled` 字段**：factory 加载时 `enabled:false` 跳过 registry（保留 api_key 不丢），upsert 持久化、GET 返回；新增 `PATCH /api/v2/llm/profiles/{id}/enabled` 原地翻转（只动 flag 不丢其他字段）+ 在内存 registry 即时 apply（禁用 pop / 启用 rebuild 插回，无需重启）。挂进系统域"模型管理"子标签，删除旧 ModelDiscoveryView + 用户加的第 5 个 nav 项（收回四域）。7 个后端 enabled 测试（factory 跳过/back-compat/PATCH 持久化无损/404/GET 报告）+ 前端列表/编辑器结构实测。注：渠道开关端到端需 daemon 重启（运行中实例无 PATCH 路由）。

- 2026-06-12: **任务栏管理 + 模型管理接入**。① TaskRail：>6 任务时出搜索框（标题子串过滤），任务卡 hover 出删除按钮（二次确认 → DELETE /api/v2/sessions/{sid}，删当前会话自动切下一个/新建，乐观移除 + toast）。② 接入用户在建的 ModelDiscoveryView（输入 base_url+api_key → 拉模型 → 多选批量注册），修其编译错误（apiPost 参数序/泛型/主题类名/hotloaded 字段名），挂进系统域"模型管理"子标签——apply 走热加载无需重启即可在 Composer 选用。**broken-HEAD 修复**：app.py 已 import 并注册 `llm_discovery` router 但该文件 + 其 10 个测试未入库（fresh clone ImportError），本 commit 补上。注：`model_discovery.py` 是同前缀 `/api/v2/llm/endpoints` 的未注册死重复，无人引用，未入库（避免路由冲突 landmine）。

- 2026-06-12: **前端 power-user 轮**（移植旧 UI 的高频功能）。① Slash 命令：Composer 首字符 `/` 弹命令面板（↑↓选/Enter/Tab 执行/Esc 关），10 条命令——会话动作（/new /clear /retry /undo /plan /think）+ 域跳转（/memory /skills /system /tasks），retry 回填上条指令、undo 走既有 WS `undo` 帧、clear 仅清本地。② 代码块复制：Markdown `<pre>` hover 出"复制"按钮（agent 频繁输出代码的刚需）。③ 消息悬停操作：assistant 完成态 hover 出"复制/重试"。④ 轻量 Toast 反馈所有 power 动作。实测 slash 过滤+执行切域、代码块复制按钮渲染均通过。

- 2026-06-12: **多模态输入补全（音视频/文件）**。上轮仅图片（ws_image_intake 限 data:image）；本轮新增 `ws_file_intake.py`：文档/代码/音频/视频经 WS 帧 `files` 字段（`{name,mime,data_url}`）落盘到 uploads（文件名保留+消毒+路径穿越拍平），按统一路径哲学**不内联解码**——`build_files_note()` 把磁盘路径 + 工具提示（text→file_read / audio→voice_transcribe / video→view_video）作为 `<user-uploaded-files>` 块注入 user 消息，agent 用既有工具处理。前端 Composer 放开任意文件类型（图片走 vision 8MB / 文件走落盘 48MB 双通道），文件 chip 带类型图标；reducer 剥离注入块不污染显示。8 个后端单测（解码/消毒/kind 分类/超限跳过/note 渲染）+ 前端 chip 实测。

- 2026-06-12: **媒体体验轮**（本 commit，用户实测反馈批次）。① 裂图根因：`/api/v2/media/` 只服务 screenshots/audio/uploads 三目录，但 screenshot 工具把 PNG 存桌面 → URL 必 404。hop_loop `_ensure_servable()` 把不在可服务目录的附件复制进 uploads 再出 URL（screenshot/截图工具产物现可见）。② Lightbox 当前页缩放查看（用户点名"点击放大跳页面"）：图片滚轮缩放+拖拽平移+双击复位，视频原生控件，Esc/遮罩关闭——替代旧的 `<a target=_blank>` 跳页。③ 视频/音频渲染：工具卡 MediaStrip + 工作区视觉流支持 `<video>`/`<audio>`，裂图占位反馈。④ 多模态输入回归（M1 砍掉的能力）：Composer 支持粘贴/拖拽/选择图片，附件预览条+移除，随用户帧 `images` 字段发送（后端 ws_image_intake 仅接 data:image，非图前端拦截给提示）——实测图片发送→后端存 uploads→模型视觉全链路通。前端 22 测试 + hop_loop import 验证通过。

- 2026-06-12: **交互升级轮**（本 commit，用户实测反馈批次）。① /ui-next/ 黑屏 → 根因是无错误边界（B-223 同款），补 ErrorBoundary（崩溃显示堆栈+恢复按钮）+ lazy chunk 加载失败自动刷新兜底；② 左右侧栏拖拽自由缩放（宽度持久化 + 双击恢复默认）；③ 时间线文件名全面可点击直达右栏文件 tab（编辑卡/读取卡），不在工作区时给明确提示；④ "等待审批"误导 → 语义修正（仅当未答提问真挂事件流尾部）+ 文案改"等你回答"+ 回归测试；⑤ 记忆图谱回归：/memory/v2/graph 数据 + 自绘 SVG 力导向（不引 vis-network，4.6KB chunk），kind 配色/点击聚焦邻边/节点拖拽/手动布局位置优先，实测 40 节点 112 边渲染。canvas 预览链路确认前端无责——daemon 进程仍是 factory 修复前代码，待重启复测。终端卡中文乱码 = bash 工具 GBK 解码（后端，另线处理中）。

- 2026-06-12: **视觉打磨轮 + Composer 功能补全**（本 commit，用户反馈"还是太简陋"）。HUD 重做（品牌徽标/指标分组/状态胶囊/执行中呼吸灯）；任务卡升级（状态色条/相对时间/当前活动行/迷你进度条/hover）；时间线（你-X 角色块/用户消息竖线引用/入场动画/流式光标/呼吸灯思考态/空态画面）；Composer 补回 plan_mode + ultrathink + 模型 profile 切换（帧字段与旧 UI 一致，/api/v2/llm/profiles 拉列表）+ 聚焦光晕。全局动效 token（mc-rise/mc-caret/mc-breathe/mc-card hover/:focus-visible）。实测窄宽双视口零控制台错误。

- 2026-06-11: Phase 10 立项（commit d13bbd2）。M0 完成：设计文档 + 三项方向决策 + ADR-010 + 用户视觉方向认可。下一步 10.M1 Web 骨架。
- 2026-06-11: 设计补充（用户出 Claude Code diff 卡截图点名要求）：工具卡按类型特化渲染入规格（设计文档新增 §2.3.1，M2 新增 10.M2.1b + 对应验收项）。file_edit 内联语法高亮 diff 卡是 M2 核心验收观感。
- 2026-06-12: **10.M2.5 收尾 + 10.M3.1 四域首轮完成**（本 commit）。安全红条 + code-split（主包 gzip 78KB）+ 四域导航与记忆/能力/系统域页（全部实测吃真数据：63 事实、87 技能、5 evolution arms）。实测揪出并修复两个后端真 bug：canvas 事件因 executor 线程无 loop 从未发布（修 factory 线程安全发布）、/system/health 因 \"Request\" 字符串注解永远 422（修 import + 回归测试）。另发现 bm25 `_scan_all` AttributeError（独立 bug，已开后台任务卡）。21+71 测试全绿。
- 2026-06-12: **10.M2 主体完成**（commit 3907630）。用户两点反馈全治：① "表格糊成管道符" → react-markdown+GFM+hljs 全功能渲染（实测真 `<table>` 4 行 + 高亮代码块）；② "子代理执行组要像 Claude Code 那样" → worker_*/subagent_* 事件 → 可折叠 Agent 执行组卡。用户点名主轴"实时预览深度融合"落地：canvas artifact 沙箱渲染 + 工具截图视觉流 + 跟随模式 + Diff 角标 + 时间线↔工作区双向联动。diff 卡实测三文件全对（含 apply_patch 混合 diff +2−6）。实测中发现并修复：注入块（session-workspace/memory-*）污染用户消息显示与任务标题——前后端同步剥离（_clean_title + stripInjectedBlocks，截断块宁退 sid 不显半截）。20 个测试全绿。余项进 10.M2.5。
- 2026-06-11: **10.M1 完成**（commit be158a2）。webui/ 脚手架 + 数据层 TS 移植（11 个历史 bug 修复语义全保留）+ /api/v2/tasks 聚合 router + 三栏布局 + /ui-next/ 挂载 + CI webui-build 闸。15 个新测试全绿。**实测验证**：vite dev 反代到运行中的真 daemon——WS 握手、HUD 实数据（模型/记忆数）、发消息 → 流式回复 + 折叠思考块渲染、任务栏对旧 daemon（无 /api/v2/tasks）优雅降级，零控制台错误；顺手修了空鬼泡（渲染层守卫）+ 窄视口侧栏挤压（响应式折叠）。下一步 10.M2 执行视图。

---

## 附录 A: 竞品差异化总结

| 维度 | OpenClaw | Claude Code | Hermes | Letta | **XMclaw** |
|------|----------|-------------|--------|-------|-----------|
| 自主性 | ❌ 用户驱动 | ⚠️ yoloClassifier | ⚠️ 人类审核 | ⚠️ 有限 | **✅ 运行时流式进化** |
| 评估诚实度 | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无 | **✅ HonestGrader** |
| 主动行为 | ❌ 无 | ⚠️ 被动触发 | ❌ 无 | ❌ 无 | **✅ ProactiveAgent** |
| 记忆系统 | 基础 RAG | 基础 | 基础 | MemGPT | **✅ LanceDB + KeyInfoExtractor** |
| 多服务器 MCP | 单服务器 | 无 | 无 | 无 | **✅ 多服务器 + 命名空间隔离** |
| 自实验 | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无 | **✅ A/B + Welch t-test** |
| 权限粒度 | 3级 | 7级+yolo+Denial | 3级 | 3级 | **3级（需追赶）** |
| 默认体验 | 聊天机器人 | 聊天机器人 | 聊天机器人 | 聊天机器人 | **Phase 1 前=聊天机器人** |

---

## 附录 B: 技术债务清单

| # | 债务 | 位置 | 优先级 | 备注 |
|---|------|------|--------|------|
| 1 | ~~SkillRegistry 无并发锁~~ | `skills/registry.py` | ✅ 已修复 | 已添加 `threading.RLock`，覆盖全部读写方法 |
| 2 | **Cognition 模块 greenfield** | `cognition/*.py` | 🔴 高 | Phase 2-3 解决 |
| 3 | ~~默认全 OFF~~ | `config.example.json` | ✅ 已修复 | `evolution.enabled` 翻为 true；`autonomy_level` 翻为 50 |
| 4 | ~~陪玩店域残留~~ | `memory/v2/*`, `agent_loop.py` | ✅ 已修复 | 全部替换为通用业务示例（网店/咨询公司/电商） |
| 5 | ~~VAD 死代码~~ | `static/lib/vad.js` | ✅ 已修复 | 已删除 237 行无引用死代码 |
| 6 | ~~前端 CSS 类不匹配~~ | `mobile.css` vs `AppShell.js` | ✅ 已修复 | `xmc-h-chatpage*`→`xmc-h-chat-frame*`；`xmc-toolcard__image`→`xmc-toolcard__media-img` |
| 7 | **Eval 未跑过真实 benchmark** | `eval/` | 🟡 中 | Phase 4 解决 |
| 8 | ~~TaskScheduler 轮询~~ | `task_scheduler.py` | ✅ 已修复 | 已实现 `_wake_scheduler()` 事件驱动唤醒，新任务提交后立即调度（替代 1s 忙等） |
| 9 | ~~Intent Engine LLM stub~~ | `intent_engine.py` | ✅ 已修复 | 已实现 `_run_llm_layer()`：事件压缩→LLM推理→JSON解析→IntentPrediction |
| 10 | ~~MCP 非stdio传输~~ | `mcp_hub.py` | ✅ 已修复 | 新增 `MCPHttpBridge`（SSE/streamableHttp），`MCPHub` 已支持非 stdio 配置 |
| 11 | **Docker 技能未挂载** | `docker_runtime.py` | 🟢 低 | Phase 5 解决 |
| 12 | ~~Plugin SDK 无动态加载~~ | `plugin_sdk/` | ✅ 已修复 | 新增 `xmclaw/plugins/loader.py`，支持 `xmclaw.plugins` entry-point 发现；channel registry 已接入外部插件 |
| 13 | ~~Email 附件跳过~~ | `channels/email.py` | ✅ 已修复 | `_extract_attachments()` + `_save_email_attachments()`：图片→`raw["images"]`（AgentLoop vision），非图片→`raw["attachments"]` 元数据 |
| 14 | ~~ContextEngine 未使用~~ | `context/` | ✅ 已修复 | `AgentLoop` 新增 `context_engine` 参数；`run_turn` 中调用 `bootstrap`/`assemble`；`_persist_history` 同步到 `engine.after_turn`（渐进式迁移） |
| 15 | **权限系统 3级 vs 竞品 7级** | `security/` | 🟢 低 | 架构差距 |
| 16 | ~~TUI Protocol Mismatch~~ | `tui/app.py` | ✅ 已修复 | 帧格式改为 `{"type":"user","content":text}`，完整走 AgentLoop 工具循环 |
| 17 | ~~Memory V1/V2 双栈并存~~ | `xmclaw/memory/`, `daemon/*` | ✅ 已修复 (Phase 7, 2026-05-24) | V1 `UnifiedMemorySystem` + `_id` + `extractor` 整套删除 (-2799 行)；callsite 全部切到 V2 `MemoryService`；149 个用户 fact 经迁移脚本搬到 V2 LanceDB；sqlite_vec 保留但 scope 收窄为 workspace 索引专用 (file_chunk / code_chunk)。详见 §Phase 7 与 commits 2beb326..d1599c1。|

---

## 附录 C: 已删除的过时文档清单

以下文档已被本文件取代，不再维护：

- `AGENTS_TEMPLATE.md`
- `ARCHITECTURE.md`
- `AUDIT_2026-05-07_conflicts.md`
- `AUDIT_2026-05-07_real_architecture.md`
- `AUDIT_2026-05-10_PATHS_FE_BE.md`
- `AUDIT_PASS_3_FINDINGS.md`
- `BACKUP.md`
- `competitive_gap_analysis.md`
- `competitive_gap_analysis_v2.md`
- `CONFIG.md`
- `DEPLOY.md`
- `DEV_PLAN.md`
- `DEV_ROADMAP.md`
- `DOCTOR.md`
- `EVENTS.md`
- `EVOLUTION.md`
- `EVOLUTION_HONEST_STATE.md`
- `FRONTEND_BACKEND_ALIGNMENT.md`
- `FRONTEND_DESIGN.md`
- `FRONTEND_REWORK.md`
- `HOOKS.md`
- `JARVIS_ARCHITECTURE_V2.md`
- `JARVIS_IMPLEMENTATION_PLAN.md`
- `JARVIS_PHASE_6_DESIGN.md`
- `JARVIS_ROADMAP.md`
- `MEMORY_ARCHITECTURE.md`
- `MEMORY_EVOLUTION_REDESIGN.md` （内容并入本文件 Phase 7.1，2026-05-23）
- `MULTI_AGENT.md`
- `PRODUCT_REDESIGN.md`
- `PROJECT_DEFINITION_2026-05-10.md`
- `REWRITE_PLAN.md`
- `SLEEP_AGENT.md`
- `TOOLS.md`
- `UI_FUNCTION_AUDIT_2026-05-10.md`
- `V2_DEVELOPMENT.md`
- `V2_STATUS.md`
- `WORKSPACE.md`
- `XMCLAW_JARVIS_GAP_ANALYSIS_2026.md`

保留的子目录（资源/档案）：
- `docs/architecture/` — 架构图资源
- `docs/archive/` — 历史档案
- `docs/assets/` — 图片等静态资源
- `docs/codebase/` — 代码库分析

---

*本文档为 XMclaw v1.1.0 "Jarvis" 的唯一权威开发文档。所有开发决策应以此为准。*
