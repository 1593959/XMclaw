# Multi-Agent（Epic #17：HTTP-to-self 架构）

XMclaw 在**同一个 daemon 进程**里可以并行跑多个 agent。每个 agent
是一个 `Workspace`，彼此共享一条事件总线（`InProcessEventBus`）和一
个 `MultiAgentManager` 注册表，但拥有各自的 `AgentLoop`（或观察者）
和独立配置。Agent 之间通过**工具调用** `list_agents` / `chat_with_agent`
/ `submit_to_agent` / `check_agent_task` 互相触达 —— 不走网络，直接
调本进程同伴的 `run_turn`，但在 session id 和 prompt 前缀层面保留清
晰的 "谁在调谁" 轨迹。

> 这份文档覆盖 Epic #17 Phase 1-7 的落地形态。后续 phase 或新 epic
> 改了工具面 / 生命周期 / 数据布局，请同步更新。

## 架构速览

```
           ┌─────────────────── daemon (FastAPI, 1 process) ────────────────────┐
           │                                                                    │
  WS ──────┼──▶ /agent/v2/{sid}?agent_id=X ──▶ MultiAgentManager.get(X)         │
           │                                     │                              │
           │                                     ▼                              │
           │                 ┌── Workspace("main", kind=llm)   ──▶ AgentLoop ─┐│
           │                 │                                               ▼││
  REST ────┼─▶ /api/v2/agents │ Workspace("helper", kind=llm)  ──▶ AgentLoop  ─│┼──▶ InProcessEventBus ──▶ 所有订阅者
           │                 │                                               ▲││     (grader / memory / scheduler / UI / observer)
           │                 └── Workspace("evo-1", kind=evolution) ──▶ EvolutionAgent ─┘│
           │                                                                    │
           └────────────────────────────────────────────────────────────────────┘
```

- **一个进程 / 一条总线**：所有 workspace 都 publish/subscribe 到同
  一个 `InProcessEventBus` 实例。主 agent 的 `grader_verdict` 和 evolution
  observer 的 `SKILL_CANDIDATE_PROPOSED` 走同一通路，其它订阅者
  （memory、scheduler、UI live-replay）不需要知道事件来自哪个 agent
  —— 事件上 `agent_id` 字段已经带好。
- **路由由 agent_id 决定**：WS 连接用 `?agent_id=X`，HTTP 用
  `X-Agent-Id` header；`app.state.agents: MultiAgentManager` 按 id
  查 workspace，拿不到就 close 4404。
- **Agent 间通信不出进程**：`chat_with_agent("worker", ...)`
  直接 `await manager.get("worker").agent_loop.run_turn(sid, content)`
  —— 没 HTTP、没序列化；合成的 session id `main:to:worker:{ts}:{uuid8}`
  让日志观察者能一眼识别这条 turn 是跨 agent 的。

## Workspace 的两种 kind

Phase 7 引入 `kind` 判别字段，默认 `"llm"` 向后兼容。

| kind | 用途 | 字段 | 生命周期 |
|---|---|---|---|
| `"llm"` | 服务 WS turn 的常规 chat agent | `agent_loop: AgentLoop` | 惰性——turn 到来时才工作 |
| `"evolution"` | headless observer，不应 prompt | `observer: EvolutionAgent` | `start()` 订阅总线；`stop()` 取消订阅 |

`is_ready()` 按 kind 分派：LLM 看 `agent_loop`，evolution 看 `observer`。
`list_agents` 工具把 `kind` surface 给 caller LLM —— 观察者 workspace
被调 `chat_with_agent` 会以 "not ready" 失败，这是设计使然而非 bug。

### 新建 workspace

HTTP：

```bash
# LLM workspace
curl -H "X-Pairing-Token: $XMC_TOKEN" \
  -X POST http://127.0.0.1:8765/api/v2/agents \
  -d '{"agent_id": "helper", "llm": {"anthropic": {"api_key": "sk-ant-..."}}}'

# Evolution observer
curl -H "X-Pairing-Token: $XMC_TOKEN" \
  -X POST http://127.0.0.1:8765/api/v2/agents \
  -d '{"agent_id": "evo-main", "kind": "evolution"}'
```

每个 workspace 的 resolved config 立刻写到
`~/.xmclaw/v2/agents/<agent_id>.json`（tmp + rename 原子写），daemon
重启时 `load_from_disk` 会逐个 rehydrate。

配置合法键：
- `agent_id`（string，管理器强制与请求路径/键同名）
- `kind`（`"llm"` | `"evolution"`，可省；省略 = `"llm"`）
- `llm.*` / `tools.*` / `security.*`（仅 `kind="llm"` 使用，见
  [CONFIG.md](CONFIG.md)）

### 删除

```bash
curl -H "X-Pairing-Token: $XMC_TOKEN" \
  -X DELETE http://127.0.0.1:8765/api/v2/agents/helper
```

Manager 先 `await workspace.stop()` 再删磁盘配置 —— evolution observer
的总线订阅会同步取消，避免 daemon 关机前留下孤儿 subscription。

### 名字是 `main` 的 primary

`app.state.agent`（factory 在启动时从 `daemon/config.json` 构建的 primary
AgentLoop）**不**在 `MultiAgentManager` 里注册。WS 路由把 `agent_id=main`
或缺省都落到 primary；REST `/api/v2/agents` 不让你创建或删除 `"main"`
（reserved id）。`list_agents` 工具把 primary 合成一行在列表顶部。

## Agent 间工具（`xmclaw/providers/tool/agent_inter.py`）

只分发给 primary 的 tool surface，worker agent 默认拿不到这 4 个工具
（避免 delegation loop 和 session id 污染）。

| 工具 | 同步/异步 | 返回 | 备注 |
|---|---|---|---|
| `list_agents` | sync | `{"agents": [{agent_id, ready, primary, kind}, ...]}` | `kind` 告诉 caller 哪些 agent 能 chat |
| `chat_with_agent` | sync (await) | 最后一条 assistant message | 阻塞直到 callee `run_turn` 完成 |
| `submit_to_agent` | async | `{task_id, agent_id}` | 返回立刻，后台跑 turn |
| `check_agent_task` | sync | `{status, reply?, error?}` | status ∈ {pending, running, done, error} |

**session id 规范**（Phase 6）：
```
{caller}:to:{callee}:{ts_ms}:{uuid_hex_8}
```
字面 `to` 作分隔符，`ts_ms` 是毫秒 epoch，`uuid_hex_8` 是 `uuid4().hex[:8]`。
一键 `split(":")` 就拿到 `[caller, "to", callee, ts, uuid]`，日志查看器和事
件 replay 工具零负担。

**prompt 自动打标**：出站 content 头部贴 `[Agent {caller} requesting]\n\n`，
让 callee 的 LLM 清楚自己是在被另一个 agent 调而非用户；已贴的不
重复贴（避免嵌套 delegation 层层叠 banner）。

**caller 来源**：`get_current_agent_id()` 读 Phase 4 的 `ContextVar`。
如果你从 WS 路径进来，middleware 已经把 `X-Agent-Id` 头 / `agent_id`
查询参数 seed 进去；如果从 CLI / 测试 / scheduler 这种无请求上下文
的路径进来，fallback 成 primary id。

### 示例：main 让 helper 代跑一个 turn

```python
# 在 main 的一次 turn 里，LLM 调
chat_with_agent(agent_id="helper", content="用 ls 列一下当前目录")

# 实际发生的：
# 1. AgentInterTools.invoke 把 chat_with_agent 路由到 helper workspace
# 2. session_id = "main:to:helper:1704067200000:a3f9b2c1"
# 3. stamped = "[Agent main requesting]\n\n用 ls 列一下当前目录"
# 4. await helper.agent_loop.run_turn(session_id, stamped)
# 5. 从 helper 的 history 末尾抽 assistant message，作为 tool 返回
```

## Evolution Agent（Phase 7）

`xmclaw/daemon/evolution_agent.py::EvolutionAgent` —— 一个 headless
workspace，不服 WS turn，只订阅总线、聚合验证分、按需调
`EvolutionController.consider_promotion`、把决策结果 publish 回总线
+ 追加审计日志。

### 生命周期

1. `build_workspace("evo-main", {"kind": "evolution"}, bus)` 实例化
   `EvolutionAgent`。
2. `MultiAgentManager.create()` 注册进 dict 之前 `await ws.start()`
   —— observer 在总线上 subscribe `grader_verdict`。
3. 每来一个 verdict 事件，observer 按 `(skill_id, version)` 聚合
   `{plays, total_reward}`。没有 `skill_id` 字段时用 `candidate_idx:{N}`
   兜底，让 bench 事件也不漏算。
4. 调用方（通常是 SkillScheduler 或 CLI 评估脚本）
   `await observer.evaluate(head_version=H, head_mean=M)` 得到一份
   `EvolutionReport`：
   - `EvolutionDecision.PROMOTE` → observer publish
     `SKILL_CANDIDATE_PROPOSED` 事件（带 evidence 列表和 winner 信
     息），**但绝不直接写 `SkillRegistry`**（anti-req #12：evidence
     必须经过 `registry.promote` 正门）。
   - `NO_CHANGE` / `ROLLBACK` → 只写审计日志，不发事件。
5. 每次 `evaluate` 都追加一行 JSON 到
   `~/.xmclaw/v2/evolution/<agent_id>/decisions.jsonl`。
6. `MultiAgentManager.remove()` 或 daemon lifespan 的 `finally` →
   `await ws.stop()` 取消订阅。

### 审计日志格式

```json
{
  "ts": 1704067200.123,
  "agent_id": "evo-main",
  "decision": "promote",
  "head_version": 3,
  "winner_candidate_id": "summary",
  "winner_version": 4,
  "evaluations": [
    {"candidate_id": "summary", "version": 3, "plays": 12, "mean_score": 0.71},
    {"candidate_id": "summary", "version": 4, "plays": 15, "mean_score": 0.79}
  ],
  "evidence": ["candidate=summary", "plays=15", "mean=0.790", "baseline=0.710",
               "gap_over_head=0.080", "gap_over_second=0.080"],
  "reason": "arm 'summary' cleared all gates — promoting v4"
}
```

append-only JSONL，外部 tail 工具直接可读。`OSError` 在写审计时被吞
并记 warning —— 内存聚合才是权威，审计是 best-effort。

### 阈值调优

`PromotionThresholds` 默认值来自 bench 初期标定（见
`xmclaw/core/evolution/controller.py`）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `min_plays` | 10 | 最佳 arm 必须积累至少这么多播放次数 |
| `min_mean` | 0.65 | 绝对质量底线 |
| `min_gap_over_head` | 0.05 | 相对 HEAD 的提升门槛 |
| `min_gap_over_second` | 0.03 | 与第二名的 separation 门槛 |

每个 observer 可以持自己的阈值（Phase 8 会把 config 拉到 workspace
配置），允许 A/B 实验。

## 运行态数据布局

```
~/.xmclaw/v2/
├── agents/                      ← MultiAgentManager 注册表
│   ├── main.json                ← （可选）primary 如果也从磁盘来
│   ├── helper.json
│   └── evo-main.json            ← {"agent_id": "evo-main", "kind": "evolution"}
├── evolution/                   ← Phase 7 审计子树
│   └── evo-main/
│       └── decisions.jsonl
├── events.db
├── memory.db
├── daemon.pid / .meta / .log
└── pairing_token.txt
```

`agents/` 和 `evolution/` 都是 v2 runtime 子树，`rm -rf ~/.xmclaw/v2`
一把清干净；用户 preset 库（`~/.xmclaw/workspaces/`）和审计日志
（`~/.xmclaw/logs/`）在外面不受影响。

## 常见用法 recipe

### 1. 跑一个独立的 QA agent

```bash
# 新建 helper
curl -H "X-Pairing-Token: $XMC_TOKEN" \
  -X POST http://127.0.0.1:8765/api/v2/agents \
  -d '{"agent_id": "qa", "llm": {"anthropic": {"api_key": "sk-ant-...", "default_model": "claude-haiku-4-5"}}}'

# 在 main 里让 LLM 调 chat_with_agent("qa", "review this PR diff: ...")
```

### 2. 启用 evolution observer

```bash
# 创建一次就好——配置写盘，下次 daemon start 自动 rehydrate + 订阅
curl -H "X-Pairing-Token: $XMC_TOKEN" \
  -X POST http://127.0.0.1:8765/api/v2/agents \
  -d '{"agent_id": "evo-main", "kind": "evolution"}'

# 运行一段时间后查审计
cat ~/.xmclaw/v2/evolution/evo-main/decisions.jsonl | tail -5
```

### 3. 查看某 agent 的事件流

所有 `BehavioralEvent` 都带 `agent_id`，事件 API 支持按 agent 过滤：

```bash
curl "http://127.0.0.1:8765/api/v2/events?agent_id=evo-main&types=skill_candidate_proposed"
```

## 设计约束 / Anti-patterns

- **不要在 provider 层 import `xmclaw.daemon.*`**：`providers/tool/*`
  的 agent 间工具走 `typing.Protocol` duck-typing 约束 `MultiAgentManager` /
  `Workspace` / `AgentLoop` 的形状；任何直接 import 会被
  `scripts/check_import_direction.py` 拦下。
- **Observer 不得写 `SkillRegistry`**：`SKILL_CANDIDATE_PROPOSED` 是
  建议，`SKILL_PROMOTED` 必须从主循环经 `registry.promote(evidence=...)`
  发。
- **Worker agent 默认没有 agent 间工具**：初代设计让 primary 当
  delegator、worker 当 delegate；需要 worker 能再次 delegate 时显式
  开启，考虑递归深度上限。
- **Session id 不能写 `:to:` 以外的 literal**：日志 tooling 按
  `split(":")[1] == "to"` 识别跨 agent session，自定义 agent id 里
  不要包含 `:`。
- **同一个 agent_id 并发 create 靠 `pending_starts` dedup**：两路
  调用返回**同一个** Workspace 对象，不会各建一份 LLM client 或
  memory handle。

## 相关代码位置

| 关注点 | 位置 |
|---|---|
| Workspace dataclass + build_workspace | `xmclaw/daemon/workspace.py` |
| 注册表 / 生命周期 / 持久化 | `xmclaw/daemon/multi_agent_manager.py` |
| WS agent_id 路由 / lifespan | `xmclaw/daemon/app.py` |
| `/api/v2/agents` CRUD | `xmclaw/daemon/routers/agents.py` |
| AgentContext ContextVar + ASGI middleware | `xmclaw/core/agent_context.py` + `xmclaw/daemon/agent_context.py` |
| Agent 间工具（4 个） | `xmclaw/providers/tool/agent_inter.py` |
| Evolution observer | `xmclaw/daemon/evolution_agent.py` |
| 决策引擎（pure） | `xmclaw/core/evolution/controller.py` |
| 事件类型清单 | `xmclaw/core/bus/events.py`，参考 [EVENTS.md](EVENTS.md) |
| 路径入口 | `xmclaw/utils/paths.py`（`agents_registry_dir`, `evolution_dir`） |
