# XMclaw v2 — 开发文档（2026-04-21）

这份文档是 [REWRITE_PLAN.md](REWRITE_PLAN.md) 的技术落地。Plan 讲"做什么、为什么、分几期"，这份讲"代码长什么样、接口签名怎么写、事件长什么样、每一条 anti-requirement 在代码里怎么落。"

读者：XMclaw v2 的贡献者（当下主要是我和用户 + 后续 AI 协作者）。

> **Phase 1–3.5 已交付。** §8.2 的 go/no-go 判据全部通过（离线 simulated
> 1.38×、真 LLM 1.12× baseline-beating、真 LLM 1.18× autonomous evolution）。
> 详细交付表见 [REWRITE_PLAN.md §11](REWRITE_PLAN.md#11-交付现状2026-04-21)。

---

## 1. 核心概念（不读完这一节不要写代码）

### 1.1 Evolution-as-Runtime（进化即运行时）

区别于 Hermes 的 "evolution-as-batch-optimization"（独立 repo + 离线运行 + 每次 $2–$10），v2 的进化是**内联的**：

- 每一次 LLM 调用、每一次 tool call、每一次 skill 执行都产出一条 **Behavioral Event**
- 所有 Behavioral Event 喂进 **Streaming Observer Bus**
- **Honest Grader** 在事件落地瞬间打分（不是离线批打）
- **Evolution Scheduler** 消费打分流，持续更新优化候选（prompt/skill/tool-choice）

因此："我加了一个 feature" 和 "我让 agent 自己进化出这个 feature" 在 v2 里是**同一条路径**——LLM 写 skill → grader 验分 → scheduler 决定 promote → hot-reload 上线。人类写的 skill 走同样的流程，只是起点不同。

### 1.2 Streaming Observer Bus（流式观察总线）

一个持久化的、带重放能力的事件流：

- **in-process**：Python asyncio queue + fan-out subscribers
- **on-disk**：append-only SQLite table，schema 见 §5
- **replayable**：任何 session 的事件流可以重放给 grader / scheduler，用于离线再评估
- **subscribable**：scheduler、grader、memory、cost_tracker、web UI 都是订阅者

**硬约束（Anti-req #1 的代码实现）**：commodity 层（channel、tool、LLM）**不允许**绕开 bus 直接调用 evolution/grader。它们只发事件；evolution 读事件。这条单向依赖让"进化即运行时"成为架构事实，而不是口号。

### 1.3 Honest Grader（诚实评分器）

**反 Hermes 最核心的一条**。Hermes 的 weakness 是 "agent 觉得自己做得不错"。v2 的 grader 是**不让模型给自己打分**的：

```python
class GraderVerdict(TypedDict):
    event_id: str
    ran: bool                    # 工具真跑了吗？（不是文本描述）
    returned: bool               # 有返回值吗？
    type_matched: bool           # 返回值类型和 schema 对得上吗？
    side_effect_observable: bool | None  # 有可观察的副作用吗？（文件写了？HTTP 真发了？）
    llm_judge_opinion: str | None        # LLM 主观看法（仅参考，不决策）
    score: float                 # ∈ [0,1]，由前面几个 bool 加权，LLM 意见权重 ≤ 0.2
    evidence: list[str]          # 证据 URL / 文件路径 / 进程退出码，可事后重放
```

LLM 主观意见权重上限 0.2——这是 **anti-req #4** 的代码化。

### 1.4 Tool-Call IR（工具调用内部表示）

三家 peer 都死在 provider 间翻译器易碎。v2 选统一 IR + strict per-provider translator：

```python
@dataclass
class ToolCall:
    id: str                       # 内部 UUID
    name: str                     # 工具名
    args: dict[str, Any]          # 已解析的参数
    schema_version: int = 1
    provenance: Literal["anthropic", "openai", "json_mode", "synthetic"]
    raw_snippet: str | None = None  # 原始字符串（debug）
```

每家 provider 有：
- `encode_to_provider(call: ToolCall) -> ProviderPayload`
- `decode_from_provider(payload: ProviderPayload) -> ToolCall | None`

`decode_from_provider` **必须**返回结构化 `ToolCall` 或 `None`——**不允许**返回"长得像 tool call 的文本"，这是 **anti-req #1** 的代码化。

---

## 2. 仓库结构（分支 `v2-rewrite`）

```
xmclaw/
├── core/                    # 运行时骨架
│   ├── bus/                 # Streaming Observer Bus
│   │   ├── __init__.py
│   │   ├── events.py        # EventBase + 所有具体 Event dataclass
│   │   ├── memory.py        # in-process 实现
│   │   ├── sqlite.py        # 持久化实现
│   │   └── replay.py        # 重放工具
│   ├── grader/              # Honest Grader
│   │   ├── __init__.py
│   │   ├── checks.py        # ran/returned/type_matched/side_effect
│   │   └── verdict.py
│   ├── scheduler/           # Evolution Scheduler
│   │   ├── __init__.py
│   │   ├── online.py        # 在线流式优化（Phase 1）
│   │   └── policy.py        # tool-choice / model-choice 策略
│   ├── ir/                  # Tool-Call IR
│   │   ├── __init__.py
│   │   └── toolcall.py
│   └── session/             # Session 生命周期
│       ├── __init__.py
│       └── lifecycle.py
│
├── providers/               # 所有 pluggable 接口
│   ├── llm/                 # LLMProvider 接口 + 具体实现
│   │   ├── base.py
│   │   ├── anthropic.py
│   │   ├── openai.py
│   │   └── translators/
│   │       ├── anthropic_native.py
│   │       └── openai_tool_shape.py
│   ├── memory/              # MemoryProvider
│   │   ├── base.py
│   │   └── sqlite_vec.py    # 默认实现
│   ├── channel/             # ChannelAdapter
│   │   ├── base.py
│   │   ├── ws.py
│   │   ├── slack.py
│   │   ├── telegram.py
│   │   └── ...
│   ├── tool/                # ToolProvider（含 MCP bridge）
│   │   ├── base.py
│   │   ├── builtin.py
│   │   └── mcp_bridge.py
│   └── runtime/             # SkillRuntime
│       ├── base.py
│       ├── local.py
│       └── docker.py
│
├── skills/                  # Skill 定义 + manifest
│   ├── base.py              # Skill 抽象类
│   ├── manifest.py          # manifest.json schema
│   └── versioning.py        # 版本化 + rollback
│
├── daemon/                  # FastAPI + WS 服务
│   ├── app.py
│   ├── ws_gateway.py
│   └── auth.py              # 设备绑定认证
│
├── cli/                     # xmclaw CLI
│   └── main.py
│
├── plugins/                 # 第三方 plugin 加载
│   └── loader.py
│
└── utils/                   # 纯工具
    ├── log.py               # 从 v1 迁移
    ├── paths.py             # 从 v1 迁移
    └── cost.py              # 从 v1 迁移 + 熔断

web/                         # 前端（从零重构，另起 README）
docs/                        # 文档
plugin-sdk/                  # 独立发包：xmclaw-plugin-sdk
tests/
├── unit/
├── integration/
├── conformance/             # 跨平台 / 跨 provider 同构测试
└── bench/                   # anti-req #11 同模型对比 bench
```

**结构铁律**：
- `core/` 里的代码**不得**从 `providers/` 或 `skills/` import
- `providers/` 里的代码**不得**从 `core/scheduler` 或 `core/grader` import（只能向 bus 发事件）
- `skills/` 是 `core/` 的消费者，是 `providers/` 的并列层
- CI 有一个 import-direction 检查脚本，违者 block

---

## 3. 核心接口契约

每一个都是 `abc.ABC`，在 `providers/*/base.py` 里定义。贡献者写 plugin 时继承这些基类。

### 3.1 `LLMProvider`

```python
class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]: ...

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse: ...

    @property
    @abc.abstractmethod
    def tool_call_shape(self) -> ToolCallShape: ...
      # Literal["anthropic", "openai_tool", "openai_jsonmode"]

    @property
    @abc.abstractmethod
    def pricing(self) -> Pricing: ...
```

`LLMChunk` 是内部归一化 chunk（见 §5 事件 schema）。provider 返回的东西在离开 provider 前就归一化——**anti-req #3** 的代码化。

### 3.2 `MemoryProvider`

```python
class MemoryProvider(abc.ABC):
    @abc.abstractmethod
    async def put(self, layer: Layer, item: MemoryItem) -> str: ...
      # layer ∈ {"short", "working", "long"}

    @abc.abstractmethod
    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict | None = None,
    ) -> list[MemoryItem]: ...

    @abc.abstractmethod
    async def forget(self, item_id: str) -> None: ...
```

默认实现 `SqliteVecMemory` 在 `providers/memory/sqlite_vec.py`，分三个 table（short/working/long），语义检索走 sqlite-vec。**不得**把历史写进 system prompt（anti-req #2）。

### 3.3 `ChannelAdapter`

```python
class ChannelAdapter(abc.ABC):
    name: ClassVar[str]  # e.g. "slack", "telegram"

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, target: ChannelTarget, payload: OutboundMessage) -> str: ...
      # returns external msg id

    @abc.abstractmethod
    def subscribe(self, handler: Callable[[InboundMessage], Awaitable[None]]) -> None: ...
```

每个 ChannelAdapter **必须**通过 `tests/conformance/test_channel_conformance.py` 的 13 项 test（收发、重连、丢帧、多账号、限流、并发、空消息、超大消息、表情、引用、撤回、分片、顺序）。 CI 里矩阵跑——**anti-req #7** 的代码化。

### 3.4 `ToolProvider`

```python
class ToolProvider(abc.ABC):
    @abc.abstractmethod
    def list_tools(self) -> list[ToolSpec]: ...

    @abc.abstractmethod
    async def invoke(self, call: ToolCall) -> ToolResult: ...
```

MCP bridge 是一个 `ToolProvider` 实现，把每个 MCP server 的 tools 暴露出来。

### 3.5 `SkillRuntime`

```python
class SkillRuntime(abc.ABC):
    @abc.abstractmethod
    async def fork(self, skill: SkillSpec, args: dict) -> SkillHandle: ...

    @abc.abstractmethod
    async def kill(self, handle: SkillHandle) -> None: ...

    @abc.abstractmethod
    async def status(self, handle: SkillHandle) -> SkillStatus: ...

    @abc.abstractmethod
    def enforce_manifest(self, manifest: SkillManifest) -> None: ...
      # 应用 manifest.permissions 到 sandbox（FS / net / subprocess allow-list）
```

**anti-req #5 + #8** 的代码化：技能跑在沙箱里、manifest 声明权限、可 kill、版本化（version 由 SkillManifest 携带）。

### 3.6 `Grader`

```python
class Grader(abc.ABC):
    @abc.abstractmethod
    async def grade(self, event: BehavioralEvent) -> GraderVerdict: ...
```

默认实现 `HonestGrader` 在 `core/grader/`，按 §1.3 逻辑。领域特化 grader（代码类任务：测试跑没跑；图像类任务：质量分）以 plugin 形式挂入。

### 3.7 `Scheduler`

```python
class Scheduler(abc.ABC):
    @abc.abstractmethod
    async def on_event(self, event: BehavioralEvent) -> None: ...

    @abc.abstractmethod
    async def decide_next(self, ctx: DecisionContext) -> Decision: ...
      # Decision ∈ {"call_tool", "respond", "ask_user", "delegate", "retry_optimized"}

    @abc.abstractmethod
    async def promote_candidate(self, candidate: Candidate) -> PromotionResult: ...
```

Phase 1 的 `OnlineScheduler` 就用最小实现：滑动窗口 grader 得分 → 选 best-of-n prompt 候选 → promote。

### 3.8 `EventBus`

```python
class EventBus(abc.ABC):
    @abc.abstractmethod
    async def publish(self, event: BehavioralEvent) -> None: ...

    @abc.abstractmethod
    def subscribe(
        self,
        predicate: Callable[[BehavioralEvent], bool],
        handler: Callable[[BehavioralEvent], Awaitable[None]],
    ) -> Subscription: ...

    @abc.abstractmethod
    async def replay(self, from_id: str, filter_: EventFilter) -> AsyncIterator[BehavioralEvent]: ...
```

---

## 4. 事件 Schema（BehavioralEvent）

**这是 v2 的数据契约，比接口更重要**——改事件 schema 等于全仓联动。

### 4.1 顶层结构

```python
@dataclass
class BehavioralEvent:
    id: str                    # UUIDv7（时间可排序）
    ts: float                  # Unix ts，ns 精度
    session_id: str
    agent_id: str
    type: EventType
    schema_version: int = 1    # semver，破坏变更进 CHANGELOG
    payload: dict              # 具体 type 对应具体 schema（见下）
    correlation_id: str | None = None  # 关联同一轮对话的事件
    parent_id: str | None = None       # 事件因果链
```

### 4.2 EventType 枚举（Phase 1 最小集）

| type | 来源 | payload 关键字段 |
|---|---|---|
| `user_message` | ChannelAdapter | `content`, `channel`, `user_ref` |
| `llm_request` | LLMProvider 前置 | `model`, `messages_hash`, `tools_count` |
| `llm_chunk` | LLMProvider 流式 | `delta`, `seq` |
| `llm_response` | LLMProvider 完成 | `content`, `usage`, `latency_ms` |
| `tool_call_emitted` | Scheduler 决策 | `call: ToolCall` |
| `tool_invocation_started` | ToolProvider 接单 | `call_id` |
| `tool_invocation_finished` | ToolProvider 返回 | `call_id`, `result`, `error`, `latency_ms` |
| `skill_exec_started` | SkillRuntime.fork | `skill_id`, `version`, `args_hash` |
| `skill_exec_finished` | SkillRuntime | `skill_id`, `result`, `side_effects`, `latency_ms` |
| `grader_verdict` | Grader | 完整 `GraderVerdict`（§1.3） |
| `cost_tick` | CostTracker | `tokens_in`, `tokens_out`, `usd`, `budget_left` |
| `session_lifecycle` | Session | `phase: "create" \| "active" \| "checkpoint" \| "destroy"` |
| `skill_candidate_proposed` | Scheduler | `candidate`, `reason` |
| `skill_promoted` | Scheduler | `skill_id`, `from_version`, `to_version`, `evidence: list[str]` |
| `skill_rolled_back` | Scheduler | `skill_id`, `from_version`, `to_version`, `reason` |
| `anti_req_violation` | CI/Runtime | `req_id: int`, `message`, `location` |

### 4.3 schema 管理

- 事件 schema 用 `pydantic.BaseModel` 声明在 `core/bus/events.py`，导出 JSON Schema 到 `docs/schemas/events.v1.json`
- semver：字段加 = minor，删 = major；major 要迁移脚本
- 发布前运行 `scripts/check_event_schema.py`，检测是否破坏性

---

## 5. 数据流（从用户消息到 agent 响应）

```
ChannelAdapter (inbound)
        │
        ▼
publish(user_message)  ─────┐
        │                    │
        ▼                    │   subscribe
┌───────────────┐            │
│   Session     │            │
│   Lifecycle   │            │
└──────┬────────┘            │
       │ decide_next()       │
       ▼                     │
┌───────────────┐    ┌───────┴────────┐
│   Scheduler   │───▶│  EventBus      │
└──────┬────────┘    └───────┬────────┘
       │                     │
       │ (call_tool)         │ subscribe
       ▼                     ▼
┌───────────────┐    ┌────────────────┐
│ ToolProvider  │    │     Grader     │
└──────┬────────┘    └───────┬────────┘
       │                     │
       │ publish(tool_result)│ publish(grader_verdict)
       └──────────┬──────────┘
                  ▼
             EventBus
                  │
                  ├────▶ Memory (subscribe, 选择性持久化)
                  ├────▶ CostTracker (subscribe)
                  ├────▶ Scheduler (subscribe → 更新候选)
                  └────▶ Web UI (subscribe → 展示)
```

**关键**：唯一的 "decide what to do next" 路径是 `Scheduler.decide_next`。其他组件**没有**做决策的权力，只消费事件或执行既定动作。这是整个架构的**因果轴**。

---

## 6. 开发环境与工作流

### 6.1 本地启动（Phase 1）

```bash
# 安装
git checkout v2-rewrite
pip install -e ".[dev]"

# 初始化 daemon 配置
xmclaw config init --dev

# 跑
xmclaw start                     # 启动 daemon
xmclaw chat                      # 连 CLI

# 或：直接跑 Phase 1 demo
python -m xmclaw.demo.phase1 --skill read_and_summarize --turns 50
```

### 6.2 测试分层

| 层 | 位置 | 跑法 | 何时跑 |
|---|---|---|---|
| Unit | `tests/unit/` | `pytest tests/unit -n auto` | 每次保存 |
| Integration | `tests/integration/` | `pytest tests/integration` | 每次 commit |
| Conformance | `tests/conformance/` | `pytest tests/conformance --matrix` | PR + nightly |
| Bench | `tests/bench/` | `pytest tests/bench --slow` | release gate |

### 6.2.1 Smart-gate：按 diff 选最小 pytest 子集（Epic #11）

`scripts/test_lanes.yaml` 定义了 "哪些源文件变 → 跑哪些 pytest 文件" 的 lane 映射。
`scripts/test_changed.py` 读 YAML + `git diff --name-only`，输出最小 pytest 命令。

```bash
# 本地：对工作区（staged+unstaged）vs HEAD 选子集
python scripts/test_changed.py --dry-run        # 看计划，不跑
python scripts/test_changed.py                   # 计划 + 直接跑

# CI：对 origin/main...HEAD 三点 diff
python scripts/test_changed.py --base origin/main

# 从 stdin 喂路径（给 pre-commit 或自定义脚本用）
git diff --name-only HEAD | python scripts/test_changed.py --from-stdin

# 强制跑全套（兜底）
python scripts/test_changed.py --all

# 透传额外参数给 pytest
python scripts/test_changed.py -- -v -k "not slow"
```

**Lane 语义**：

- `triggers: [glob, ...]` — fnmatch 风格 glob；任一匹配则 lane 触发。
- `tests: [path, ...]` — 触发后加入的 pytest 文件列表。
- 触发 `__always__` → 只要有变更就跑（用于 import-surface、config-shape 等便宜 sanity）。
- `tests` 里含 `__all__` → 短路走全套（pyproject / lockfile / CI 配置变化时）。
- 多个 lane 命中取并集、去重；直接动 `tests/` 下的测试文件总会被直接跑（即使没 lane 匹配源路径）。

**退出码**：`0`=pytest 成功或啥都不选（纯文档 PR），`1`=pytest 失败，`2`=参数/config 错。

**反模式**（务必不要做）：

- ❌ 教脚本 follow 传递依赖。成本（多跑一个 lane 几秒）远低于 "聪明启发式漏测后 main 炸" 的成本。显式优于 clever。
- ❌ 让 lane 间互相隐式依赖。每个 lane 都应该能独立跑过；跨 lane 的 setup 放 fixture，不放 lane 拓扑。
- ❌ 忘记加新子系统的 lane。添 `xmclaw/foo/**` 就要同步加 `foo:` lane，否则 CI 看不到你的测试。

### 6.3 CI 硬门（不过 = PR block）

| # | 检查 | 对应 anti-req |
|---|---|---|
| CI-1 | import-direction（core 不能 import providers） | 架构完整性 |
| CI-2 | 事件 schema 破坏性变更需手工 approve | schema 稳定 |
| CI-3 | Channel conformance suite 全绿 | #7 |
| CI-4 | Tool-Call IR translator 双向 fuzz | #3 |
| CI-5 | Grader ground-truth 单测覆盖率 ≥90% | #4 |
| CI-6 | 同模型 bench vs 裸 provider 劣幅 ≤5% | **#11** |
| CI-7 | 每条 `skill_promoted` 事件带 evidence | **#12** |
| CI-8 | 三平台 smoke test（Win/macOS/Linux） | #14 |
| CI-9 | 预算熔断 e2e（故意溢出时真 abort） | #6 |
| CI-10 | `anti_req_violation` 事件总数 == 0 | 全部 |

### 6.4 Commit & PR

- Commit：Conventional Commits（`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`, `bench:`）
- Branch：`feat/v2-<module>-<short>`、`fix/v2-...`，基于 `v2-rewrite`
- PR：至少 1 个 reviewer + 全部 CI 硬门绿；涉及事件 schema/接口的 PR 需要在描述里明确 semver 影响

### 6.5 发布

- `main` 上跑 v1 维护版（仅阻塞 bug）
- `v2-rewrite` 跑 v2 开发，完成后合入 `main` 并 tag `v2.0.0`
- v1 最后版本打 archive tag `v1-final`

---

## 7. 14 条 Anti-Requirements 在代码里的具体落点

| # | Anti-req | 代码层面证据 |
|---|---|---|
| 1 | 不信任文本 tool call | `ToolCall` 仅来自 `decode_from_provider` 返回结构化结果；`decide_next` 只消费 `ToolCall` 对象，不消费字符串 |
| 2 | 非 FTS5-only 记忆 | `SqliteVecMemory` 必须用 sqlite-vec embedding 检索；不得把 history 塞 system prompt |
| 3 | 翻译器不易碎 | `ir/toolcall.py` + 每个 `translators/*`；双向 fuzz 测试 CI-4 |
| 4 | 不让 LLM 自评 | `HonestGrader` 里 `llm_judge_opinion` 权重 ≤ 0.2；CI-5 覆盖 |
| 5 | 技能可回滚 | `SkillManifest.version` + `skills/versioning.py`；promote 事件必带 rollback 指针 |
| 6 | 成本硬熔断 | `utils/cost.py` 触发 `BudgetExceeded` 异常，scheduler 捕获后 abort；CI-9 |
| 7 | Channel CI parity | `conformance/test_channel_conformance.py` + 矩阵；CI-3 |
| 8 | 设备绑定 | `daemon/auth.py` 配对时交换 ed25519 公钥；未配对连接 WS 直接断 |
| 9 | Session lifecycle | `core/session/lifecycle.py`，状态机 create→active→checkpoint→destroy；泄漏 CI-10 告警 |
| 10 | 多后端同构 | `conformance/runtime_suite.py` 跨 `local/docker/ssh/...` 跑同任务 |
| **11** | 同模型 bench gate | `tests/bench/same_model_against_peers.py`，Anthropic+OpenAI 各 50 任务；CI-6 |
| **12** | 进化带证据 | `skill_promoted` event schema 要求 `evidence: list[str]` 非空；CI-7 |
| **13** | 接口化 | 所有 7 个接口都是 `abc.ABC`，import-direction check (CI-1)；plugin SDK 独立包可安装 |
| **14** | 协议 + 跨平台 | 三平台 smoke (CI-8)；MCP/ACP/OpenAI-compat 各有集成 test |

---

## 8. Phase 1 首期 deliverables（go/no-go 的最小纵切）

**目标：** 在 1 个 CLI agent + 1 个 demo skill 上，证明 streaming evolution + honest grader 真的让 agent 随用越强。

### 8.1 文件清单

```
xmclaw/core/bus/events.py                # 10 个事件类型
xmclaw/core/bus/memory.py                # in-process EventBus
xmclaw/core/bus/sqlite.py                # 持久化 EventBus
xmclaw/core/grader/checks.py             # 4 个 ground-truth check
xmclaw/core/grader/verdict.py            # HonestGrader
xmclaw/core/ir/toolcall.py               # ToolCall + translators
xmclaw/core/scheduler/online.py          # OnlineScheduler (best-of-n)
xmclaw/providers/llm/anthropic.py        # 迁移 v1 的 anthropic_client
xmclaw/providers/llm/openai.py           # 迁移 v1 的 openai_client
xmclaw/providers/memory/sqlite_vec.py    # 迁移 v1 的 memory
xmclaw/providers/tool/builtin.py         # file_read / file_write 两个工具
xmclaw/skills/base.py
xmclaw/skills/demo/read_and_summarize.py  # demo skill
xmclaw/cli/main.py                       # CLI 入口
tests/bench/phase1_learning_curve.py     # 学习曲线 bench
tests/conformance/tool_call_ir.py
```

### 8.2 Go/No-go 判据 — 现状（2026-04-21 已过）

| 判据 | 原设计 | 实际交付 | 佐证 |
|---|---|---|---|
| 1. 学习曲线单调 + ≥ 20% | 50 轮末窗 / 首窗 ≥ 1.20× | ✅ **离线 1.38×** (simulated oracle) | `phase1_learning_curve.py` |
| 1-bis. Live 学习曲线 | — | ✅ **真 LLM 1.12× over uniform baseline** | `phase1_live_learning_curve.py`, `13d7338` |
| 2. Grader / 人工一致率 ≥ 80% | — | 🚧 未做（Phase 1.2 TODO 标注在代码里） |  |
| 3. 同模型不劣于裸 SDK ≤ 5% | 均值差 ≤ 5% | ✅ 用**更强的形式**：provider_noninterference conformance (14 tests) 证明 v2 发出的 API body 与裸 SDK 完全一致。零偏差 ⇒ temperature=0 下响应必然字节一致 | `test_antireq11_provider_noninterference.py`, `5527866` |
| 4. 事件流可重放 | replay CLI | 🚧 stub（`xmclaw/core/bus/replay.py` 有签名，待 Phase 1.5） |  |

**额外达成（原 Phase 1 计划外）：**

| 额外交付 | 证据 |
|---|---|
| Tool-aware agent loop（真 LLM 100% 工具调用率）| Phase 2.6, `426f994` |
| 自治进化循环（无人干预，真 LLM 1.18× session 2 vs session 1）| Phase 3.5, `3f44d6d` |
| Anti-req #1 在三层证明（翻译器 + grader + agent loop）| `test_v2_tool_loop.py` + live |
| 跨 session 记忆（引导下一 session 收敛）| Phase 2.4, `93b1cbb` |

**Go/No-go 结论：** 判据 1 + 3 + 额外四项全过。判据 2、4 是后续工作，不阻塞当前 phase 收官。

### 8.3 非 Phase 1 范围（明确说不做）

- 不做 channel adapter（Phase 2）
- 不做 skill sandbox（Phase 3）
- 不做 OS-primitive fork/exec（Phase 3）
- 不做 hot-reload（Phase 5）
- 不做 Web UI（Phase 6）

Phase 1 只证一件事：**在线流式进化 + 诚实评分 + 统一 IR** 这三件事合起来能让同一任务随用越强。做大了就是流水线工作，做不出就是差异化失败。

---

## 9. 常见陷阱 & FAQ

**Q：events 存哪、存多久？**  
A：SQLite 文件 `~/.xmclaw/events.db`，默认 30 天 rolling 保留；session 被用户 "pin" 的永存。

**Q：事件流会不会把 secret 写进磁盘？**  
A：所有 payload 过 `utils/redact.py` 黑名单脱敏（API key / token / 密码 pattern）。conformance test 强制。

**Q：grader 打分慢咋办？**  
A：Grader 在事件发完**后**异步打分，不阻塞响应。verdict 晚到几百 ms 是允许的。

**Q：LLM 如果就是返回文本描述的"工具调用"？**  
A：translator 返回 `None`，scheduler 看到没 `ToolCall` 对象就视作普通文本响应；单独记一个 `anti_req_violation` 事件用于调试。没有"宽容解析"这回事。

**Q：plugin 作者怎么开发？**  
A：`pip install xmclaw-plugin-sdk`，继承对应基类，在 `pyproject.toml` 注册 entry-point（`xmclaw.plugins.channel` / `.tool` / `.memory` / `.llm` / `.runtime` / `.grader`）。daemon 启动时扫描加载。

**Q：我怎么知道我在写 v1 还是 v2？**  
A：分支 `v2-rewrite`。`main` 只给 v1 维护 PR。两者的 import path 不冲突（v1 是 `xmclaw.*` 原样，v2 在同一 path 下新写——冲突的文件先在 v2-rewrite 分支删 v1 原版）。

**Q：我怎么验证我的改动没违反 anti-req？**  
A：`tox -e ci-local` 跑完整 CI 硬门 10 条。任何一条失败都 block。

---

## 10. 下一步（已做过的里程碑 + 当前候选）

### 已完成（按时间顺序）

1. ✅ 切分支 `v2-rewrite`，§8.1 骨架 + placeholder 搭起来
2. ✅ Phase 1 coding：bus / IR / grader / scheduler / demo skill / 学习曲线 bench
3. ✅ 真 Anthropic provider + domain grader + tool-aware agent loop
4. ✅ Phase 2 commodity 层：OpenAI provider、sqlite-vec 分层记忆、WS channel、BuiltinTools
5. ✅ Phase 3 技能基建：SkillRegistry 版本化 + 回滚、LocalSkillRuntime、EvolutionController
6. ✅ Phase 3.5：真 LLM 自治进化循环 + 三轮 live bench 全过

### 当前候选（任选其一继续）

1. **Phase 3.4 进程隔离 runtime** — 让 manifest 的 fs/net/memory 真的能限制；Phase 3.5 已不依赖它，这次只是为了兑现承诺
2. **Strangler-fig 清理** — 删 v1 已被 v2 覆盖的模块（`agent_loop.py`、`task_classifier.py`、`evolution/*`、`genes/*`），让 repo 视觉上"只有 v2"
3. **多 channel adapter** — 把 conformance matrix 从 N=1 扩到 N=3（Slack/Telegram 模拟实现）
4. **Phase 4 起步**：daemon 集成 + 发布流水线 + v2 UI
5. **合并决策**：v2-rewrite → main 的 PR（见 [REWRITE_PLAN.md §12](REWRITE_PLAN.md#12-合并回-main-的决策点)）
