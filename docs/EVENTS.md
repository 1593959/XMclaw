---
summary: "Event bus system and event types (v2)"
title: "Events"
---

# 事件总线（v2 BehavioralEvent）

v2 daemon 把每个可观察动作（工具调用、LLM 响应、skill 执行、grader 判决、todo 变更、注入检测、...）都包成一个 `BehavioralEvent`，通过 `EventBus` 广播。订阅者（grader / scheduler / 内存 / 成本追踪 / WebSocket 前端）**只消费事件，不互相直接调用**——这是 v2 松耦合的根基。

> 本文件是 daemon 的事件契约。Schema 变更都要联动这里 + `xmclaw/core/bus/events.py`；破坏性变更按 [docs/V2_DEVELOPMENT.md §4.3](V2_DEVELOPMENT.md#43-schema-管理) 走 major 升级。

## 数据模型

```python
# xmclaw/core/bus/events.py
@dataclass(frozen=True, slots=True)
class BehavioralEvent:
    id: str                    # uuid4 hex（UUIDv7 等 stdlib 就绪再切）
    ts: float                  # time.time() — unix 秒（float）
    session_id: str
    agent_id: str
    type: EventType            # 下表枚举
    payload: dict[str, Any]    # 每种 type 有自己的 payload 约定
    correlation_id: str | None = None
    parent_id: str | None = None
    schema_version: int = 1
```

`frozen=True` 是刻意的：订阅者不能原地改事件，避免一个下游把另一个下游看到的数据改了。构造走 `make_event(session_id=, agent_id=, type=, payload=...)`——`id` 和 `ts` 由 factory 填。

## EventType 全集

| type                          | 发射者                      | payload 关键字段                                                     |
| ----------------------------- | --------------------------- | -------------------------------------------------------------------- |
| `user_message`                | Channel / AgentLoop 入口    | `content`, `channel`, `user_ref`                                     |
| `llm_request`                 | LLMProvider 前置            | `model`, `messages_hash`, `tools_count`                              |
| `llm_chunk`                   | LLMProvider 流式            | `delta`, `seq`                                                       |
| `llm_response`                | LLMProvider 完成            | `content`, `usage`, `latency_ms`                                     |
| `tool_call_emitted`           | Scheduler 决策              | `call: ToolCall`                                                     |
| `tool_invocation_started`     | ToolProvider 接单           | `call_id`                                                            |
| `tool_invocation_finished`    | ToolProvider 返回           | `call_id`, `result`, `error`, `latency_ms`                           |
| `skill_exec_started`          | SkillRuntime.fork           | `skill_id`, `version`, `args_hash`                                   |
| `skill_exec_finished`         | SkillRuntime                | `skill_id`, `result`, `side_effects`, `latency_ms`                   |
| `grader_verdict`              | HonestGrader                | 完整 `GraderVerdict`（见 V2_DEVELOPMENT §1.3）                       |
| `cost_tick`                   | CostTracker                 | `tokens_in`, `tokens_out`, `usd`, `budget_left`                      |
| `session_lifecycle`           | Session                     | `phase: "create" \| "active" \| "checkpoint" \| "destroy"`           |
| `skill_candidate_proposed`    | Scheduler                   | `candidate`, `reason`                                                |
| `skill_promoted`              | Scheduler                   | `skill_id`, `from_version`, `to_version`, `evidence: list[str]`      |
| `skill_rolled_back`           | Scheduler                   | `skill_id`, `from_version`, `to_version`, `reason`                   |
| `anti_req_violation`          | CI / Runtime                | `req_id: int`, `message`, `location`                                 |
| `todo_updated`                | `todo_write` 工具（AgentLoop） | `items: [...]`, `sid`（供 Web UI 实时渲染 Todo 面板，免轮询）      |
| `prompt_injection_detected`   | `xmclaw/security/policy.py` | `source`, `policy`, `findings`, `invisible_chars`, `scanned_length`, `acted`, `tool_call_id` |

完整常量定义见 [`xmclaw/core/bus/events.py`](../xmclaw/core/bus/events.py)。所有值都是 snake_case 字符串——旧版 v1 的 `agent:start` / `tool:called` 格式已下线。

## Bus 实现

`xmclaw/core/bus/` 里有两份实现：

- **`InProcessEventBus`** — 纯内存。`subscribe(predicate, handler)` 返回 `Subscription`，`publish(event)` 把 event fan-out 给每个谓词返回 True 的订阅者。handler 在 asyncio task 里跑——一个 handler 抛异常不会把 publish 带崩（会通过 `get_logger(__name__).warning("bus.subscriber_failed", ...)` 结构化记一笔）。`xmclaw ping` 跑的就是它。
- **`SqliteEventBus(InProcessEventBus)`** — 继承前者，把每个 event 落到 `~/.xmclaw/v2/events.db`（WAL + FTS5），支持 `query(session_id=, since=, until=, types=, limit=, offset=)` 和 `search(q, session_id=, limit=)`。`xmclaw serve` 默认走这个；CI / 集成测试场景没指定的话走 `InProcessEventBus`。

两者的发布接口一致，所以 `AgentLoop` 只认 `InProcessEventBus` 这一个协议。

## Python 订阅样板

```python
from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)
from xmclaw.core.bus.memory import accept_all

bus = InProcessEventBus()

async def on_tool_finished(event: BehavioralEvent) -> None:
    print(f"tool done: {event.payload.get('call_id')} "
          f"in {event.payload.get('latency_ms')} ms")

# 只收 tool_invocation_finished
sub = bus.subscribe(
    predicate=lambda e: e.type == EventType.TOOL_INVOCATION_FINISHED,
    handler=on_tool_finished,
)

# 或者收全部（调试时最省心）
bus.subscribe(accept_all, on_tool_finished)

await bus.publish(make_event(
    session_id="sess-1",
    agent_id="default",
    type=EventType.USER_MESSAGE,
    payload={"content": "hello"},
))

sub.cancel()   # 取消订阅
```

## HTTP API

`xmclaw serve` 的 FastAPI app 暴露一条只读查询端点：

```
GET /api/v2/events
```

查询参数（全可选）：

| 参数         | 类型    | 说明                                                                 |
| ------------ | ------- | -------------------------------------------------------------------- |
| `session_id` | string  | 只返回某一 session 的事件                                            |
| `since`      | float   | unix 秒（含），返回 ts ≥ since 的事件                                |
| `until`      | float   | unix 秒（不含），返回 ts < until 的事件                              |
| `types`      | string  | 逗号分隔 EventType 值（如 `tool_invocation_finished,grader_verdict`）|
| `q`          | string  | FTS5 关键字搜索（仅在 SqliteEventBus 时生效，`q` 设置时优先于时间范围）|
| `limit`      | int     | 返回上限，默认 200，clamp 到 `[1, 2000]`                             |
| `offset`     | int     | 分页偏移，默认 0                                                     |

响应：

```json
{
  "events": [ /* event-as-jsonable 序列化结果 */ ],
  "count": 42,
  "bus": "SqliteEventBus"
}
```

`bus` 字段供前端判断是否能用 FTS5（`q` 参数），避免在 `InProcessEventBus` fallback 下误报「查不到」。

## WebSocket 实时流

前端 `/ui/` 通过 WebSocket 订阅实时事件。daemon 的 WS handler 把 bus 订阅映射成 WS 帧；客户端收到：

```json
{
  "type": "event",
  "event": {
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
}
```

断线重连时客户端可以用上一条看到的 `id` 走 `GET /api/v2/events?since=<ts>` 或 `bus.replay()`（见 [`xmclaw/core/bus/replay.py`](../xmclaw/core/bus/replay.py)）回填错过的事件。

## 相关文件

- [`xmclaw/core/bus/events.py`](../xmclaw/core/bus/events.py) — `EventType` + `BehavioralEvent` + `make_event()`
- [`xmclaw/core/bus/memory.py`](../xmclaw/core/bus/memory.py) — `InProcessEventBus` + `accept_all`
- [`xmclaw/core/bus/sqlite.py`](../xmclaw/core/bus/sqlite.py) — `SqliteEventBus`（WAL + FTS5 + `query/search`）
- [`xmclaw/core/bus/replay.py`](../xmclaw/core/bus/replay.py) — `replay(from_id, filter_)` async iterator
- [`xmclaw/daemon/app.py`](../xmclaw/daemon/app.py) — `GET /api/v2/events` 实现
- [docs/V2_DEVELOPMENT.md §4](V2_DEVELOPMENT.md#4-事件-schemabehavioralevent) — 设计原则与 schema 版本管理
