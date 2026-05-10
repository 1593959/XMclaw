---
description: Jarvis Phase 6 — 从「turn-based AgentLoop」转向「持续认知进程」的框架级重构设计
tags: [design, jarvis, architecture, phase-6, framework-level]
date: 2026-05-09
status: design-complete-impl-pending
---

# Jarvis Phase 6 — 持续认知进程

## 0. 一句话本质

**Phase 1-5 让 XMclaw 「具备」认知组件；Phase 6 让 XMclaw 「持续认知」**。

| | Phase 1-5（已 ship） | Phase 6（本文档） |
|---|---|---|
| 触发模型 | request-response | continuous loop |
| 用户消息 | 唯一感知源 | 众多感知源之一 |
| 思考时机 | turn 边界 | 1 Hz baseline 心跳 |
| 自主性 | 0%（不写不动） | opt-in 0-100% slider |
| 缺失模块 | 推理 / 规划 / 自主目标 / 实验循环 / 进程监控 | 全部补齐 |

如果 Phase 1-5 是「给 Agent 装了脑子」，Phase 6 是「让脑子开机不关」。

---

## 1. 当前架构 vs 目标架构

### 1.1 Current（turn-based）

```
WS frame → AgentLoop.run_turn(msg)
              ├─ build prompt
              ├─ LLM hops (max_hops=N)
              ├─ tools
              ├─ HonestGrader
              └─ persist + reply
                                       → idle until next WS frame
```

idle = nothing happens（cron tick / sleep_worker 在固定时刻短促醒来，没有持续观察）。
CognitiveState 在 turn 之间存在，但**不主动推动行为**。

### 1.2 Target（continuous cognitive process）

```
┌──────────────────────────────────────────────────────────────────┐
│                 Cognitive Daemon (always running)                │
│                                                                  │
│   ┌───────────────── PerceptionBus ─────────────────┐            │
│   │   user_msg / file_event / process_event /       │            │
│   │   network_pulse / time_tick / internal_event    │            │
│   └────────────────────┬────────────────────────────┘            │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   AttentionFilter                      │   ← SalienceWeights │
│   │   (top-K by salience, drop the rest)   │     已 ship Phase 1 │
│   └────────────────────┬───────────────────┘                     │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   WorkingMemory (7±2 chunks)           │   ← 已 ship Phase 1 │
│   └────────────────────┬───────────────────┘                     │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   ReasoningEngine                      │   ← Phase 6.2 NEW   │
│   │   causal / analogical / counterfactual │                     │
│   │   / meta                               │   uses MemoryGraph  │
│   └────────────────────┬───────────────────┘     + StrategyBank  │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   Planner (HTN)                        │   ← Phase 6.3 NEW   │
│   │   goal → sub-goals → actions           │   uses Skills as    │
│   │   plan repair on failure               │   action library    │
│   └────────────────────┬───────────────────┘                     │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   ActionDispatcher                     │                     │
│   │   route to AgentLoop.run_turn(...) OR  │                     │
│   │   directly invoke skill (autonomous)   │                     │
│   └────────────────────┬───────────────────┘                     │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   ResultObserver                       │   ← uses HonestGrader│
│   │   grade + write outcome to MemoryGraph │     已 ship Phase 1 │
│   └────────────────────┬───────────────────┘                     │
│                        ▼                                         │
│   ┌────────────────────────────────────────┐                     │
│   │   LearningLoop                         │   ← Phase 6.5 NEW   │
│   │   pattern detection → experiment       │     extends         │
│   │   queue → adoption decision            │     evolution_loop  │
│   └────────────────────┬───────────────────┘                     │
│                        │                                         │
│                        └─────► back into PerceptionBus ─────►(loop)│
└──────────────────────────────────────────────────────────────────┘
```

**核心设计点**：

1. **PerceptionBus 统一入口** — 所有外部刺激都先成为 percept，再经注意力筛选。当前 WS 消息 / 文件事件 / cron 触发各自独立的代码路径，重构后**统一**进 percept_bus，由 AttentionFilter 决定哪些值得动作。

2. **Heartbeat-driven** — 后台 task 每 1Hz 醒来排空 percept_bus（默认 1Hz；高 salience 事件可自唤醒）。idle 时几乎零 CPU；高 salience 时立刻反应。

3. **AgentLoop 角色转变** — 从「入口」变「执行器」。Cognitive daemon 决定「该不该 run_turn」+ 「带什么 goal run_turn」，AgentLoop 仍是 LLM-tool loop 的实现，但不再是触发源。

4. **自主性 slider** — `autonomy_level: 0..100` 配置项。0 = 纯被动（等同于 Phase 5 行为），100 = full Jarvis（主动推送、主动执行）。中间档位用 `action_threshold` 控制 salience 阈值。

---

## 2. 模块清单 + Phase 拆分

### 2.1 7 个新模块（Phase 6.1 - 6.7）

| Phase | 模块 | 文件 | LOC 估 | 工时 | 依赖 |
|---|---|---|---|---|---|
| 6.1 | PerceptionBus + AttentionFilter | `xmclaw/cognition/perception_bus.py` | 400 | 1 周 | CognitiveState（已有） |
| 6.2 | ReasoningEngine（4 type） | `xmclaw/cognition/reasoning.py` | 600 | 1.5 周 | MemoryGraph + StrategyBank |
| 6.3 | HTN Planner | `xmclaw/cognition/planner.py` | 700 | 1.5 周 | SkillRegistry + ReasoningEngine |
| 6.4 | GoalGenerator（自主目标） | `xmclaw/cognition/goal_generator.py` | 350 | 1 周 | Planner + AutonomyPolicy |
| 6.5 | SelfExperimentLoop | `xmclaw/cognition/self_experiment.py` | 500 | 1 周 | evolution_loop.py 扩展 + experiment_result 表 |
| 6.6 | ProcessWatcher | `xmclaw/cognition/process_watcher.py` | 250 | 3 天 | psutil（new dep） |
| 6.7 | CognitiveDaemon（整合） | `xmclaw/cognition/cognitive_daemon.py` | 500 | 1 周 | 以上全部 |

**总计 ~3300 LOC，~7-8 周**。

### 2.2 周边改动

| 文件 | 改动类型 | 估 |
|---|---|---|
| `xmclaw/daemon/app.py` | lifespan 起 CognitiveDaemon | +30 行 |
| `xmclaw/daemon/agent_loop.py` | 暴露 `run_turn_with_goal(goal, ...)` 让 ActionDispatcher 调用 | +50 行 |
| `xmclaw/core/bus/events.py` | +新 EventType: PERCEPT_GENERATED / GOAL_PROPOSED / PLAN_GENERATED / AUTONOMOUS_ACTION_ATTEMPTED | +30 行 |
| `daemon/config.example.json` | `cognition.continuous_loop.{enabled, autonomy_level, action_threshold, heartbeat_hz}` | +30 行 |
| `xmclaw/daemon/static/pages/Cognition.js` | 加面板：当前 attention focus / active goals / autonomy slider | +100 行 |
| `pyproject.toml` | `cognition-process = ["psutil>=5.9"]` 可选 extra | +1 行 |

---

## 3. 详细模块设计

### 3.1 PerceptionBus（Phase 6.1）

```python
@dataclass(frozen=True)
class Percept:
    id: str                              # uuid
    source: str                          # "ws" / "file" / "process" / "time" / "internal"
    kind: str                            # "user_msg" / "file_modified" / "process_oom" / ...
    timestamp: float
    payload: dict                        # source-specific
    suggested_salience: float | None     # source 自评（attention 可覆盖）
    correlation_id: str | None           # 关联到 session / goal / plan

class PerceptionBus:
    """单一入口的 percept 队列，多生产者 / 单消费者
    （消费者 = AttentionFilter 的 ingest 任务）。
    """
    def __init__(self, max_buffer: int = 1024) -> None: ...
    async def push(self, p: Percept) -> None: ...
    async def drain(self) -> list[Percept]: ...
    async def subscribe(self, fn: Callable[[Percept], Awaitable[None]]) -> str: ...
```

**Backward compat**：当 `cognition.continuous_loop.enabled = false`（默认），PerceptionBus 不被消费 — 现有 WS / cron / file_watcher 路径完全不变。当启用，新增 percept producers 把事件 push 进 bus，old paths 仍然功能性工作（不会出现「按了 Stop 还在动」）。

**集成点**：
- WS 用户消息 → `PerceptionBus.push(Percept(source="ws", kind="user_msg", ...))` + 仍然走 AgentLoop（不是替换，是观察）
- file_watcher 已有 → 加一个 subscriber 转 percept
- cron tick → percept
- 内部 goal 完成 → percept（让系统知道自己刚干完一件事）

### 3.2 AttentionFilter（Phase 6.1）

```python
class AttentionFilter:
    def __init__(
        self,
        cognitive_state: CognitiveState,
        bus: PerceptionBus,
        action_threshold: float = 0.6,
        top_k: int = 7,                  # working memory cap (7±2)
    ) -> None: ...

    async def tick(self) -> list[Percept]:
        """从 bus 拿 percepts，computeS salience，更新 working memory，
        返回 salience >= action_threshold 的 percept 子集（actionable）。"""
        percepts = await self._bus.drain()
        scored = await asyncio.gather(*[
            self._cognitive_state.compute_salience(
                p.payload.get("content", ""),
                urgency=self._infer_urgency(p),
                novelty=await self._novelty(p),
            )
            for p in percepts
        ])
        # 更新 working memory（top-K by score, evict lowest）
        # 返回 actionable
        return [p for p, s in zip(percepts, scored) if s >= self._action_threshold]
```

**关键**：salience formula 已经在 `state.py`（`w1*urgency + w2*relevance + w3*novelty - w4*fatigue`）。AttentionFilter 只是把它接入到 percept 流。

### 3.3 ReasoningEngine（Phase 6.2）

4 种推理 + 元推理网关：

```python
class ReasoningEngine:
    def __init__(
        self, llm: Any, graph: MemoryGraph, bank: StrategyBank | None,
        evolution_tier: str = "unknown",
    ) -> None: ...

    async def causal(
        self, hypothesis: str, evidence: list[str],
    ) -> CausalConclusion:
        """A 导致 B？查 graph 的 CAUSED_BY 边 + LLM 验证 + confidence。"""

    async def analogical(
        self, current_situation: str, top_k: int = 3,
    ) -> list[Analogy]:
        """这跟我之前哪些情况像？查 graph 找历史 Event，graph 邻居 + StrategyBank
        retrieve 双路召回，LLM 排序。"""

    async def counterfactual(
        self, decision_point: str, alternative: str,
    ) -> CounterfactualResult:
        """如果当时做了 X 而不是 Y，会怎样？纯 LLM，但提供 graph 历史
        相似情况作 grounding，避免裸幻觉。"""

    async def meta(self, query: str) -> MetaCognitionResult:
        """我现在知道得够多吗？答案：知识缺口在哪，下一步应该 perceive
        什么 / experiment 什么。"""

    async def reason(
        self, query: str, mode: Literal["auto", "causal", "analogical",
                                          "counterfactual", "meta"],
    ) -> ReasoningResult:
        """Top-level entry. mode='auto' → 元推理决定走哪条。"""
```

**Iron Rule #3 接入**：Constructor 已有 `evolution_tier`；weak-tier 跳过 causal / counterfactual / meta（这三依赖 LLM 高质量推理；analogical 的 graph-召回 部分仍能跑，给空 LLM 排序时按 graph 距离）。

**输出形态**：所有推理结果都是 evidence-bearing — 必须能解释「我为什么这么想」+ 「不确定度」。

### 3.4 HTN Planner（Phase 6.3）

```python
@dataclass(frozen=True)
class Goal:                              # 已 ship state.py
    id: str
    name: str
    description: str
    priority: int
    parent_goal_id: str | None
    sub_goal_ids: tuple[str, ...]
    completion_criteria: dict
    deadline: float | None

@dataclass(frozen=True)
class Plan:
    id: str
    goal_id: str
    steps: tuple[PlanStep, ...]
    status: Literal["draft", "executing", "completed", "failed", "repaired"]
    confidence: float

@dataclass(frozen=True)
class PlanStep:
    id: str
    action_kind: Literal["llm_turn", "skill_invoke", "tool_call", "wait_for_percept"]
    payload: dict                        # action-specific
    depends_on: tuple[str, ...]          # 同一 plan 内的 step ids
    expected_outcome: str                # 用于 repair 时判断「是不是按预期执行」
    retry_policy: dict

class Planner:
    async def plan(self, goal: Goal) -> Plan:
        """HTN 分解：goal → sub-goals → atomic actions。
        action 优先 match 已有 skill；否则降级到 llm_turn。"""

    async def execute(self, plan: Plan, dispatcher: ActionDispatcher) -> PlanResult:
        """逐步执行 + 监控 + repair。"""

    async def repair(self, failed_plan: Plan, failure: PlanStepFailure) -> Plan:
        """重生成 — 已知失败原因 + 历史 repair 案例（graph）。"""
```

**HTN 简化**：不实现完整 SHOP/SHOP2 — 用「LLM 生成步骤 + skill registry 匹配 + retry」近似。完整 HTN 推到 Phase 7。

### 3.5 GoalGenerator + AutonomyPolicy（Phase 6.4）

```python
class AutonomyPolicy:
    """自主等级 0-100 → 一组阈值 + 行为开关。"""
    @classmethod
    def from_level(cls, level: int) -> "AutonomyPolicy":
        # 0: 完全手动；50: 主动提示但不动作；100: 无人监督 OK
        ...

    autonomous_action_per_hour_cap: int          # 0 / 5 / unlimited
    can_modify_files: bool
    can_send_messages: bool
    can_run_long_processes: bool
    proactive_notification_enabled: bool
    self_experiment_enabled: bool
    weekly_summary_enabled: bool

class GoalGenerator:
    """3 类自主目标 — 受 AutonomyPolicy 闸控。"""
    async def maintenance(self) -> list[Goal]:
        """系统健康类：清理临时文件 / 压缩老 journal /
        检查 daemon.log 异常率"""

    async def exploration(self) -> list[Goal]:
        """学习类：尝试新工具 / 测试某个 skill 边界 /
        阅读用户最近修改的代码"""

    async def social(self) -> list[Goal]:
        """关系类：用户连续 3 天没 chat → 主动 check-in /
        感知到 deadline 临近 → 提醒"""
```

**安全网**：
- 任何 autonomous action 都进 `autonomous_action_log` SQLite 表（不可篡改）
- UI 「Cognition」页有 "kill switch" + audit log
- 每个 autonomous action 写入 MemoryGraph 一个 `Event` 节点 + `CAUSED_BY: GoalGenerator` 边
- 默认 `autonomy_level = 0`（向后兼容，等同 Phase 5 行为）

### 3.6 SelfExperimentLoop（Phase 6.5）

扩展 Phase 1 的 `evolution_loop.py`，加 experiment_result 表 + 对照组 + 推广决策：

```python
@dataclass(frozen=True)
class Experiment:
    id: str
    hypothesis: str                      # 自然语言
    intervention: dict                   # 改了什么 (skill v / prompt v / threshold)
    metric: str                          # "task_pass_rate" / "mean_turns" / "user_followup_negativity"
    baseline_metric_value: float
    holdout_set_size: int
    started_at: float

@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    treatment_value: float
    baseline_value: float
    delta: float
    delta_p_value: float                 # 简单 t-test 即可
    decision: Literal["adopt", "reject", "extend", "abort"]
    decision_reason: str

class SelfExperimentLoop:
    async def propose(self) -> Experiment | None: ...
    async def execute(self, exp: Experiment) -> ExperimentResult:
        """复用 Sprint 4 BenchmarkRunner — 跑两次（baseline + treatment）
        相同 task case，t-test。"""
    async def adopt(self, result: ExperimentResult) -> None:
        """Iron Rule #2: 走 staging → 4-gate → registry.promote 路径"""
```

**关键复用**：Sprint 4 的 `Runner` + `SuiteResult` 已经能跑 A/B —— 这里只是把 baseline 和 treatment 用同一个 suite 跑，做差。**不需要新 harness**。

experiment_result 持久化到 `~/.xmclaw/v2/experiments.db`，UI Cognition 页加面板。

### 3.7 ProcessWatcher（Phase 6.6）

```python
class ProcessWatcher:
    def __init__(self, bus: PerceptionBus, watch_specs: list[ProcessWatchSpec]) -> None: ...

    async def start(self) -> None:
        """每 30s 轮询 watched processes，CPU/memory/zombie/exit detection。"""

    async def watch(self, pid: int, description: str, alert_thresholds: dict) -> str:
        """加监控；返回 watch_id。"""
```

**懒依赖**：`psutil` 通过 `cognition-process` extra 引入。`from xmclaw.cognition.process_watcher import ProcessWatcher` 不强制依赖；只有 `start()` 时 import psutil。

### 3.8 CognitiveDaemon（Phase 6.7）

```python
class CognitiveDaemon:
    """主循环。daemon lifespan 启动一个 task 跑 _run() forever。"""

    def __init__(
        self, bus: PerceptionBus, attention: AttentionFilter,
        reasoning: ReasoningEngine, planner: Planner,
        dispatcher: ActionDispatcher, learner: SelfExperimentLoop,
        autonomy: AutonomyPolicy, heartbeat_hz: float = 1.0,
    ) -> None: ...

    async def _run(self) -> None:
        while not self._stop:
            actionable = await self._attention.tick()
            for percept in actionable:
                await self._handle(percept)
            await asyncio.sleep(1.0 / self._heartbeat_hz)

    async def _handle(self, p: Percept) -> None:
        # 1. reason about it
        result = await self._reasoning.reason(p.payload.get("content", ""), mode="auto")
        # 2. if a goal pops out, plan it
        for goal in result.suggested_goals:
            if self._autonomy.can_act(goal):
                plan = await self._planner.plan(goal)
                await self._dispatcher.execute(plan)
        # 3. record into MemoryGraph for learning
        await self._learner.observe(p, result)
```

---

## 4. Backward Compatibility 矩阵

| `cognition.enabled` | `cognition.continuous_loop.enabled` | `autonomy_level` | 行为 |
|---|---|---|---|
| false | (irrelevant) | (irrelevant) | **完全 Phase 5 之前行为**（默认）。Cognition 包不 import，零 overhead。 |
| true | false | (irrelevant) | **当前 Phase 1-5**。CognitiveState / MemoryGraph / proactive_recall 工作；不持续循环。 |
| true | true | 0 | Phase 6 模块全 ship，但 GoalGenerator 不 fire，autonomous_action 全部禁用。**主动观察 + 提示 OFF**。 |
| true | true | 50 | Daemon 持续观察 + 主动**提示**用户（不动作）。"我注意到你刚改了 X，需要 Y 吗？" |
| true | true | 100 | full Jarvis：自主行动（受 audit log + rate limit + kill switch 保护）|

**默认 `cognition.continuous_loop.enabled = false`**。打开它的人需要显式接受："I want my XMclaw to think continuously". 这跟 evolution.enabled 一样的"诚实门槛"。

---

## 5. 风险 + 缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 持续循环 CPU / 电池占用 | 高 | 中 | heartbeat 默认 1Hz；空 percept_bus 时 sleep；attention_threshold 调高让大部分 tick 早出 |
| 自主行动失控 | 中 | 高 | autonomy_level 默认 0；autonomous_action_per_hour 上限；audit log；kill switch；所有 action 必须经 staging gate（Iron Rule #2） |
| LLM 成本爆炸 | 高 | 高 | reasoning + planning 的每次调用都 cache by (input_hash, output) 进 MemoryGraph；72h 内同样 input 直接复用结果 |
| 测试性 | 高 | 中 | CognitiveDaemon 必须支持 manual-tick mode（测试驱动 tick 而不是真 heartbeat）；所有模块都 LLM-injectable |
| 对现有架构的破坏 | 中 | 高 | Phase 6 全部 opt-in；turn-based AgentLoop 路径完全保留；continuous_loop 只是 OBSERVE existing path 然后 augment |
| 设计本身错误（vault of "AI can think for me"） | 低-中 | 灾难性 | autonomy_level=100 必须**双重确认**（config flag + CLI 命令）；UI 显眼显示当前 level；任何 autonomous action 都通知用户（除非用户显式静音） |

---

## 6. 第一刀切哪儿（最小可演进的开端）

如果你只能动一周，选 **Phase 6.1 (PerceptionBus + AttentionFilter)**：

- LOC 最小（~400）
- 不动任何现有路径，只是新增观察层
- 立刻能在 UI 「Cognition」页**可视化**当前 attention focus（"系统现在在关注这些东西"）— 这是用户从「啊有这个功能」到「我感觉它真在思考」的最大感知差
- 后续 Phase 6.2-6.7 都依赖它，不会白做

第一周的退出标准：
1. PerceptionBus + AttentionFilter ship + 30+ test
2. WS 消息 / file_watcher 事件 / cron 都进 bus（既有路径不受影响）
3. UI Cognition 页加 「当前注意力」面板（~50 LOC JS）
4. 配置 `cognition.continuous_loop.enabled` 默认 false；启用后只观察不动作

---

## 7. 跟 master plan 的对齐

| master plan 缺口 | 本设计的 Phase | 状态 |
|---|---|---|
| ProcessWatcher（"那个训练跑了 2 小时了"） | 6.6 | 有计划，3 天工时 |
| 自主实验循环 | 6.5 | 有计划，复用 Sprint 4 harness |
| 推理引擎（因果/类比/反事实/元） | 6.2 | 有计划，1.5 周 |
| 规划系统 HTN | 6.3 | 有计划，简化版 1.5 周 |
| 自主目标 + 自主行动 | 6.4 | 有计划 + 多重安全网 |
| memory_search ↔ graph 邻居扩展 | 通过 Phase 6.2 ReasoningEngine.analogical 间接覆盖 | 顺带做 |

---

## 8. 决策点（需要 user 拍板的事）

1. **是否同意框架级转向**："turn-based + idle" → "continuous + tickable"。
2. **`autonomy_level` 默认值**：0（完全保守）vs 50（主动提示）。建议 0。
3. **`heartbeat_hz` 默认**：1Hz（保守）vs 4Hz（响应快）vs 0.2Hz（省电）。建议 1Hz，可调。
4. **新依赖**：`psutil`（ProcessWatcher 必需）— 接受成 `cognition-process` opt-in extra？
5. **从哪个 Phase 开始**：建议 6.1 → 6.2 → 6.6 → 6.5 → 6.3 → 6.4 → 6.7。理由：先建 perception 基础（6.1），再补容易缺口（6.6 ProcessWatcher / 6.5 SelfExperiment 复用 Sprint 4），再啃硬骨头（6.2 reasoning / 6.3 planner / 6.4 autonomy / 6.7 整合）。

---

## 9. 跟 Anti-req 的对齐检查

| Anti-req | Phase 6 是否破坏 |
|---|---|
| #1 not-a-chatbot | 加深 — 系统现在真的不是 chatbot 了 |
| #5 lossless turn boundary | 不变 — turn 仍然原子 |
| #8 auth on WS + HTTP | 不变 |
| #11 thin provider layer | 不变 — Reasoning / Planning 在 core/ 不在 providers/ |
| #12 evidence-gated promote | 加强 — SelfExperimentLoop 每个 adopt 都走 staging gate |
| #14 protocol compat | 不变 — 新增 EventType 不破坏老订阅者 |

---

*版本 v0.1 — 2026-05-09*
*状态：设计完成，等待开工决策*
*依赖：Phase 1-5（已 ship at commits 0b33325 / 746ec4d / 2b295b2）*
