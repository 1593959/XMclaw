---
summary: "Event bus system and event types"
title: "Events"
---

# 事件总线 (Event Bus)

XMclaw 使用异步发布/订阅事件总线在各个组件之间通信，支持监控、调试和扩展。

## 架构

```
AgentLoop ──→ [agent:start] ──→ EventBus ──→ WebSocket → 前端
                      │                              ↕
                 [tool:called] ──→ 审计日志
                      │
              [evolution:cycle] ──→ 监控面板
```

## 事件类型

| 事件类型 | 说明 | 触发时机 |
|---------|------|---------|
| `agent:start` | Agent 开始处理请求 | `run_agent()` 调用时 |
| `agent:stop` | Agent 完成请求 | Agent 循环结束时 |
| `agent:error` | Agent 执行出错 | 异常抛出时 |
| `agent:message` | Agent 回复生成 | 流式输出时 |
| `user:message` | 用户消息 | 收到用户消息时 |
| `tool:called` | 工具被调用 | 工具执行前 |
| `tool:result` | 工具执行结果 | 工具执行完成后 |
| `task:assigned` | 任务分配 | 子任务创建时 |
| `task:completed` | 任务完成 | 子任务返回时 |
| `task:failed` | 任务失败 | 子任务异常时 |
| `memory:updated` | 记忆更新 | 会话保存后 |
| `evolution:cycle` | 进化循环 | 进化引擎运行 |
| `gene:generated` | Gene 生成 | 新 Gene 验证通过后 |
| `skill:generated` | Skill 生成 | 新 Skill 验证通过后 |
| `gene:activated` | Gene 激活 | Gene 被匹配时 |
| `skill:executed` | Skill 执行 | Skill 被调用时 |

## CLI 查看

```bash
# 查看最近事件
xmclaw events

# 只看工具调用
xmclaw events --type tool:called

# 只看进化事件
xmclaw events --type evolution:cycle

# 看更多
xmclaw events --limit 100
```

## HTTP API

```bash
# 获取最近事件
curl http://127.0.0.1:8765/api/events

# 按类型过滤
curl "http://127.0.0.1:8765/api/events?event_type=tool:called"

# 获取统计信息
curl http://127.0.0.1:8765/api/events/stats
```

## Python 使用

```python
from xmclaw.core.event_bus import get_event_bus, Event, EventType

bus = get_event_bus()

# 订阅特定事件
async def on_tool(event):
    print(f"Tool called: {event.payload}")

sid = await bus.subscribe("tool:called", on_tool)

# 订阅所有事件（通配符）
sid2 = await bus.subscribe_wildcard(lambda e: print(f"Any: {e.event_type}"))

# 发布事件
await bus.publish(Event(
    event_type=EventType.AGENT_START,
    source="my_agent",
    payload={"task": "hello"},
))

# 取消订阅
bus.unsubscribe(sid)

# 获取历史
events = bus.get_history(event_type="tool:called", limit=50)

# 统计
stats = bus.get_stats()
# {'total_events': 142, 'subscriber_count': 5, 'events_by_type': {...}}
```

## 限流

事件总线有内置限流保护：每种事件类型每 60 秒最多 200 条，超出则静默丢弃，防止事件风暴。

## WebSocket 转发

守护进程的 WebSocket 处理程序会自动将所有事件转发给连接的客户端，前端收到：

```json
{
  "type": "event",
  "event": {
    "event_type": "tool:called",
    "source": "default",
    "payload": {"tool": "bash", "args": {...}},
    "timestamp": "2026-04-18T12:00:00"
  }
}
```
