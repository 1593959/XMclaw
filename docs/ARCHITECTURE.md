---
summary: "XMclaw v2 system architecture, components, and wire protocol"
read_when:
- Onboarding to the v2 daemon codebase
- Extending LLM / tool / memory / channel providers
- Debugging agent-loop or event-bus behavior
title: "Architecture"
---

# Architecture (v2)

> 权威设计文档 — 单一事实源。细节落地（schema 版本管理、anti-req
> 实现、phase 规划）参见 [docs/V2_DEVELOPMENT.md](V2_DEVELOPMENT.md)；
> 状态快照见 [docs/V2_STATUS.md](V2_STATUS.md)。**v1 架构**（5-阶段 pipeline、
> GeneForge、SkillForge、自定义 XML tool-call 格式）已下线——如在
> 老代码里看到 `xmclaw.tools.base` / `xmclaw.llm.router` / `shared/skills/`
> 这类引用，它属于 v1 残留，不是 v2 契约。

## One-paragraph summary

A single long-lived **FastAPI + WebSocket daemon** hosts one `AgentLoop`
per session. Every observable step (LLM call, tool invocation, skill
execution, grader verdict, todo update, prompt-injection hit, …) is
broadcast as a `BehavioralEvent` over an `EventBus`. Subscribers
(`EvolutionAgent` observer, `MemoryManager`, `CostTracker`, WebSocket
UI) only consume events — they never call each other. This one-way
data flow is the **evolution-as-runtime** substrate: after every tool
call, `AgentLoop` invokes the `HonestGrader` to score on hard signals
(ran / returned / type_matched / side_effect_observable, sum 0.80;
LLM self-rating capped at 0.20) and publishes a paired `GRADER_VERDICT`.
The `EvolutionAgent` observer aggregates verdicts per `(skill_id,
version)` and emits `SKILL_CANDIDATE_PROPOSED` once promotion gates
clear; the orchestrator forwards the proposal through evidence-gated
`SkillRegistry.promote(evidence=…)` (default `auto_apply=False` —
human approves via `xmclaw evolve approve`). Skills never reach the
agent's prompt or tool list except through this path.

> **Epic #24 (2026-05-01) note**: an earlier "xm-auto-evo" Node.js
> subsystem ran in parallel and wrote SKILL.md files directly into
> the agent's system prompt without going through the grader.
> Phase 1 deleted it (`xmclaw/evolution_core/`,
> `xmclaw/daemon/{auto_evo_bridge,learned_skills,…}.py`,
> `routers/auto_evo.py`) so the only path is the one described above.
> See [DEV_ROADMAP.md Epic #24](DEV_ROADMAP.md#epic-24--自主进化重做学徒成长系统-核心差异化重写).

## Topology

```
┌────────────────┐  WS /agent/v2/{sid} + HTTP /api/v2/*  ┌──────────────┐
│  Clients       │ ◀────────────────────────────────────▶│  daemon      │
│  • web UI      │                                        │  (FastAPI)   │
│  • CLI chat    │                                        │              │
│  • channels    │                                        └──────┬───────┘
└────────────────┘                                               │ owns
                                                                 ▼
                                                       ┌─────────────────┐
                                                       │   AgentLoop     │
                                                       │  (per session)  │
                                                       └──┬──────────┬───┘
                                                          │          │
                                           LLMProvider    │          │   ToolProvider
                                           ┌──────────────▼─┐    ┌───▼──────────────┐
                                           │ anthropic.py   │    │ builtin + mcp    │
                                           │ openai.py      │    │ browser + lsp    │
                                           │ router (fair   │    │ composite        │
                                           │  retry, cost)  │    │                  │
                                           └────────────────┘    └──────────────────┘
                                                          │          │
                                                          ▼          ▼
                                                      ┌───────────────────┐
                                                      │    EventBus       │◀── subscribers:
                                                      │  (in-mem + SQLite │    grader, scheduler,
                                                      │   WAL + FTS5)     │    memory, cost, WS
                                                      └───────────────────┘
```

Import direction is enforced by
[`scripts/check_import_direction.py`](../scripts/check_import_direction.py)：
`xmclaw.core.*` 不得 import `xmclaw.providers.*` 或 `xmclaw.skills`；
`xmclaw.utils.*` 不得 import 任何其他 `xmclaw.*` 子包。详细规则在
[`xmclaw/core/AGENTS.md`](../xmclaw/core/AGENTS.md) 和
[`xmclaw/utils/AGENTS.md`](../xmclaw/utils/AGENTS.md)。

## 主要组件

| 包                          | 职责                                                                 | 关键文件                                      |
| --------------------------- | -------------------------------------------------------------------- | --------------------------------------------- |
| `xmclaw/core/bus/`          | `BehavioralEvent` + `EventType` + `InProcessEventBus` + `SqliteEventBus` | `events.py`, `memory.py`, `sqlite.py`, `replay.py` |
| `xmclaw/core/ir/`           | LLM-provider 无关的 `ToolCall` / `ToolResult` 中间表达               | `ir/toolcall.py`                              |
| `xmclaw/core/grader/`       | Honest Grader —— 不让 LLM 给自己打分                                 | `checks.py`, `verdict.py`                     |
| `xmclaw/core/scheduler/`    | Skill 促/降级在线调度 + candidate registry                           | `online.py`, `policy.py`                      |
| `xmclaw/core/evolution/`    | Evolution controller —— 把 grader 判决和 scheduler 决策绑到一起      | `controller.py`                               |
| `xmclaw/daemon/agent_loop.py`| 每 session 一个，把 LLM + tool + bus 黏到一起的 `run_turn()`        | —                                             |
| `xmclaw/daemon/app.py`      | FastAPI app 工厂：`/health`, `/api/v2/{pair,config,status,events}`, WS | —                                             |
| `xmclaw/daemon/factory.py`  | 从 `config.json` 构造 agent（LLM provider + 合并 tool provider）     | —                                             |
| `xmclaw/providers/llm/`     | `LLMProvider` ABC + anthropic / openai 实现 + 翻译层                 | `base.py`, `anthropic.py`, `openai.py`        |
| `xmclaw/providers/tool/`    | `ToolProvider` ABC + builtin / mcp / browser / lsp / composite       | 对应同名 .py                                  |
| `xmclaw/providers/memory/`  | `MemoryProvider` + sqlite-vec 默认实现                               | `base.py`, `sqlite_vec.py`                    |
| `xmclaw/providers/channel/` | `ChannelAdapter`（Slack / Telegram / …）接入骨架                     | `base.py` + per-channel                       |
| `xmclaw/providers/runtime/` | 沙箱运行时（local / process）                                         | `local.py`, `process.py`                      |
| `xmclaw/security/`          | Prompt-injection scanner + policy gate + 来源常量                    | `scanner.py`, `policy.py`                     |
| `xmclaw/skills/`            | `SkillBase` + registry + 示例 skill                                  | `registry.py`                                 |
| `xmclaw/cli/`               | `xmclaw` CLI（`start` / `stop` / `chat` / `doctor` / …）             | `main.py`, `doctor_registry.py`               |
| `xmclaw/utils/`             | 路径 / 日志 / 红染 / 成本核算；DAG 最底层                             | `paths.py`, `log.py`, `redact.py`, `cost.py`  |

## AgentLoop 生命周期

```
run_turn(session_id, user_message)
  │
  ├─ emit USER_MESSAGE
  │
  ├─ loop up to max_hops:
  │    │
  │    ├─ emit LLM_REQUEST
  │    ├─ llm.complete(messages, tools=tools)
  │    ├─ emit LLM_RESPONSE
  │    │
  │    ├─ if response.tool_calls:
  │    │     for call in tool_calls:
  │    │       emit TOOL_CALL_EMITTED
  │    │       emit TOOL_INVOCATION_STARTED
  │    │       result = await tool_provider.invoke(call)
  │    │       emit TOOL_INVOCATION_FINISHED (with side_effects)
  │    │     feed results into messages, continue
  │    │
  │    └─ else:
  │          return assistant text, break
  │
  └─ if hop limit reached:
       emit ANTI_REQ_VIOLATION("hop limit")
```

完整实现：[`xmclaw/daemon/agent_loop.py`](../xmclaw/daemon/agent_loop.py)。
Anti-req #1 在这一层的落地：loop 只消费 provider 翻译器产出的结构化
`ToolCall`，不会掉回 "看起来像 tool-call 但其实是文本" 的 fallback。

## 事件契约（wire protocol）

**HTTP**：只读查询端点 `GET /api/v2/events` 过滤 `session_id` / `since` /
`until` / `types` / `q`（FTS5 关键字）。

**WebSocket**：`/agent/v2/{session_id}`。客户端发 `{role: "user",
content: "..."}`，服务端推 JSON 帧，每一帧都是一个 `BehavioralEvent`
的 jsonable 投影：

```json
{
  "id": "a1b2...",
  "ts": 1745284800.123,
  "session_id": "sess-1",
  "agent_id": "default",
  "type": "tool_invocation_finished",
  "payload": {"call_id": "call_7", "result": "...", "latency_ms": 412},
  "correlation_id": null,
  "parent_id": null,
  "schema_version": 1
}
```

完整 `EventType` 枚举、payload 字段、订阅样板、FTS5 搜索、断线重连
重放协议 → [docs/EVENTS.md](EVENTS.md)。v2 **不再**使用 v1 的
`chunk` / `state` / `tool_call` / `done` / `reflection` 这套事件命名。

## Data 路径

运行时数据全部落在 `~/.xmclaw/v2/`（`XMC_DATA_DIR` 可整体搬家）：

```
~/.xmclaw/v2/
├── events.db            ← SqliteEventBus 持久化（WAL + FTS5）
├── memory.db            ← sqlite-vec 长期记忆
├── pairing_token.txt    ← anti-req #8：WS/HTTP 鉴权 token（0600）
├── daemon.pid / .meta   ← 单例 + 元数据
└── daemon.log           ← start 的 stdout/stderr tee
```

布局 + env 覆盖详情 → [docs/WORKSPACE.md](WORKSPACE.md)。paths 模块
是单一入口 → [`xmclaw/utils/paths.py`](../xmclaw/utils/paths.py)。

## 插件 / 扩展点

- **LLM provider**：实现 `LLMProvider` ABC，注册到 router；示例见
  `xmclaw/providers/llm/anthropic.py` / `openai.py`。
- **Tool provider**：实现 `ToolProvider` ABC；新工具登录到
  `scripts/test_lanes.yaml` 的 `tools` lane 并更新 [docs/TOOLS.md](TOOLS.md)。
- **Channel adapter**：实现 `ChannelAdapter`；遵循
  [`xmclaw/providers/channel/AGENTS.md`](../xmclaw/providers/channel/AGENTS.md) 里的 conformance 约束。
- **Doctor check**：`entry_points` 的 `xmclaw.doctor` 组，`xmclaw doctor
  --discover-plugins` 会自动拉起。完整协议见 [docs/DOCTOR.md](DOCTOR.md)。
- **Skill**：`SkillBase` 子类，版本化注册到 `SkillRegistry`；`Evolution
  Controller` 按 grader 证据促/降级。Phase 规划见 V2_DEVELOPMENT §3。

## 不变量

- **One daemon per host** —— `~/.xmclaw/v2/daemon.pid` 是 singleton 锁；僵 pid 由 `doctor --fix` 清理。
- **Events are the only coupling** —— grader / scheduler / memory / cost / UI 之间**不直接互调**，全部通过 bus。破坏这条 = 砸了 evolution-as-runtime 的地基。
- **Frozen events** —— `BehavioralEvent` 是 `frozen=True, slots=True`；订阅者不得原地改事件。
- **Import direction is one-way** —— `core` ← `daemon`（loop 层）← `cli`；`providers` 不反向 import `core` 的 high-level；`utils` 在 DAG 最底层。CI 守门。
- **Schema versioning** —— 破坏性事件 schema 变更走 `schema_version` major 升级，规则在 V2_DEVELOPMENT §4.3。
- **Anti-req enforcement** —— 14 条 anti-requirement 在代码里都有对应的 runtime/test 落点，违反会 emit `ANTI_REQ_VIOLATION` 事件。

## 相关文档

- [V2 开发文档](V2_DEVELOPMENT.md) —— 代码级接口、phase 规划、anti-req 实现
- [V2 Status](V2_STATUS.md) —— 当前 phase 进度 + bench 数字
- [Events](EVENTS.md) —— `BehavioralEvent` / `EventType` 完整契约
- [Tools](TOOLS.md) —— 工具清单、provider 结构
- [Workspace](WORKSPACE.md) —— `~/.xmclaw/` 数据目录 + `XMC_DATA_DIR`
- [Config](CONFIG.md) —— `daemon/config.json` 结构 + `XMC__` env override
- [Doctor](DOCTOR.md) —— 诊断 + 插件协议
- [Dev Roadmap](DEV_ROADMAP.md) —— Epic 分解 + 执行协议
