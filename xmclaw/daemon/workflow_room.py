"""WorkflowRoomRunner — 目标驱动工作流房间（复用 SwarmOrchestrator）.

Group（workflow 模式，2026-06-06）。用户强调核心是「多 agent 工作流编排」：
给房间一个目标，编排器拆解→按能力分派给不同 agent→聚合出统一结果。

**关键复用**：这套引擎 XMclaw 基本已有——`SwarmOrchestrator.dispatch()`
（`swarm_orchestrator.py`）已做：目标→`HTNPlanner` 拆 DAG→`LoadBalancer` 按
能力分派→`TaskScheduler` 带依赖调度→`TaskAggregator` 聚合(concat/vote/
map_reduce + LLM 合成)。本 runner 只是把它**接到房间**并把关键节点（开始/
分派/完成）作为事件推到房间 session，让群聊 UI 能"看着工作流跑"。

依赖以注入传入（swarm / publish），便于单测 stub，且**不碰** app.py/factory.py。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from xmclaw.daemon.group_room import GroupRoom

_VALID_STRATEGIES = {"concat", "vote", "map_reduce"}

# async (event_type:str, payload:dict) -> None —— 推到房间 session 的事件回调
PublishFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class WorkflowRoomRunner:
    def __init__(
        self,
        room: GroupRoom,
        swarm: Any,                       # SwarmOrchestrator（鸭子类型：.dispatch）
        *,
        publish: PublishFn | None = None,
    ) -> None:
        self.room = room
        self._swarm = swarm
        self._publish = publish

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            await self._publish(event_type, {"room_id": self.room.room_id, **payload})
        except Exception:  # noqa: BLE001 — 事件推送失败不该中断工作流
            pass

    async def run(self, user_message: str = "") -> dict[str, Any]:
        """跑一次工作流：目标 = 房间 purpose（无则取用户消息）。

        返回 ``{"ok", "result", "assignments", "completed", "failed"}``。
        实时进度经 publish 推到房间 session（G2 接 WS 后群聊可见）。
        """
        # 延迟导入避免与 app/factory 的构造顺序耦合。
        from xmclaw.daemon.swarm_orchestrator import SwarmDispatchRequest

        goal = (self.room.purpose or "").strip() or (user_message or "").strip()
        if not goal:
            await self._emit("workflow_error", {"error": "目标为空（房间无 purpose 且无消息）"})
            return {"ok": False, "result": "", "assignments": {}, "completed": 0, "failed": 0,
                    "error": "empty goal"}

        strategy = self.room.aggregation if self.room.aggregation in _VALID_STRATEGIES else "map_reduce"
        await self._emit("workflow_started", {
            "goal": goal,
            "participants": list(self.room.participants),
            "strategy": strategy,
        })

        req = SwarmDispatchRequest(description=goal, strategy=strategy)
        try:
            result = await self._swarm.dispatch(req)
        except Exception as exc:  # noqa: BLE001 — 一次失败也要把错误回给房间
            await self._emit("workflow_error", {"error": f"{type(exc).__name__}: {exc}"})
            return {"ok": False, "result": "", "assignments": {}, "completed": 0, "failed": 0,
                    "error": str(exc)}

        assignments = dict(getattr(result, "assignments", {}) or {})
        await self._emit("workflow_assignments", {"assignments": assignments})
        await self._emit("workflow_done", {
            "ok": bool(getattr(result, "ok", False)),
            "result": getattr(result, "result", "") or "",
            "completed": int(getattr(result, "completed", 0) or 0),
            "failed": int(getattr(result, "failed", 0) or 0),
            "timed_out": int(getattr(result, "timed_out", 0) or 0),
            "elapsed_seconds": float(getattr(result, "elapsed_seconds", 0.0) or 0.0),
        })
        return {
            "ok": bool(getattr(result, "ok", False)),
            "result": getattr(result, "result", "") or "",
            "assignments": assignments,
            "completed": int(getattr(result, "completed", 0) or 0),
            "failed": int(getattr(result, "failed", 0) or 0),
        }
