"""/api/v2/rooms — 多 agent 群聊/工作流房间 CRUD + 运行.

Group G2 (2026-06-06)。房间 = 若干 agent 参与者 + 用途 + 形态(chat/workflow)。
- ``chat``：`GroupOrchestrator` 轮流/主持人发言。
- ``workflow``：`WorkflowRoomRunner` 复用 ``app.state.swarm_orchestrator``
  做目标驱动编排（目标→拆解→分派→聚合）。

第一刀 ``POST /{id}/run`` 同步执行并返回结果（可 curl 验证）；WS 流式（让群聊
"看着跑"）放后续。复用 ``app.state.swarm_orchestrator`` / ``app.state.agents``。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.group_room import (
    GroupRoom,
    GroupRoomRegistry,
    sanitize_room_id,
)

router = APIRouter(prefix="/api/v2/rooms", tags=["rooms"])


def _registry(request: Request) -> GroupRoomRegistry:
    """惰性构造房间注册表并挂到 app.state（首访 load_from_disk）。"""
    reg = getattr(request.app.state, "rooms", None)
    if reg is None:
        reg = GroupRoomRegistry()
        try:
            reg.load_from_disk()
        except Exception:  # noqa: BLE001
            pass
        request.app.state.rooms = reg
    return reg


def _get_loop(request: Request, agent_id: str) -> Any:
    """拿某参与者的 AgentLoop：main → app.state.agent；其余 → agents 注册表。"""
    if agent_id in ("main", "", None):
        return getattr(request.app.state, "agent", None)
    mgr = getattr(request.app.state, "agents", None)
    if mgr is None:
        return None
    ws = mgr.get(agent_id)
    return getattr(ws, "agent_loop", None) if ws is not None else None


def _apply_shared_memory(request: Request, room) -> int:
    """记忆互通：房间 shared_memory=true 时，把每个参与者的 ``_memory_service``
    指到同一个共享实例(``app.state.memory_v2_service``，主 agent 用的那个)，
    于是房间内任一 agent 写的 fact，其他 agent 都能召回。纯运行时覆盖，不动
    factory。返回成功接线的参与者数。
    """
    if not getattr(room, "shared_memory", False):
        return 0
    shared = getattr(request.app.state, "memory_v2_service", None)
    if shared is None:
        return 0
    n = 0
    for aid in room.participants:
        loop = _get_loop(request, aid)
        if loop is None:
            continue
        try:
            loop._memory_service = shared  # noqa: SLF001 — 运行时共享接线
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ── CRUD ──
@router.get("")
async def list_rooms(request: Request) -> JSONResponse:
    return JSONResponse({"rooms": [r.to_dict() for r in _registry(request).list_rooms()]})


@router.post("")
async def create_room(request: Request) -> JSONResponse:
    body = await request.json()
    raw = (body.get("room_id") or body.get("name") or "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "room_id 或 name 必填"}, status_code=400)
    # 显式 room_id 若是合法 ASCII 就用；否则（含中文名）生成随机 id。
    rid = sanitize_room_id(body.get("room_id") or "")
    if not rid:
        import uuid
        rid = "room-" + uuid.uuid4().hex[:8]
    reg = _registry(request)
    if rid in reg:
        return JSONResponse({"ok": False, "error": f"房间 {rid!r} 已存在"}, status_code=409)
    room = GroupRoom(
        room_id=rid,
        name=body.get("name", "") or rid,
        purpose=body.get("purpose", ""),
        participants=list(body.get("participants") or []),
        mode=body.get("mode", "chat"),
        policy=body.get("policy", "round_robin"),
        aggregation=body.get("aggregation", "map_reduce"),
        max_rounds=int(body.get("max_rounds", 6) or 6),
        shared_memory=bool(body.get("shared_memory", True)),
    )
    reg.create(room)
    return JSONResponse({"ok": True, "room": room.to_dict()})


@router.get("/{room_id}")
async def get_room(room_id: str, request: Request) -> JSONResponse:
    r = _registry(request).get(room_id)
    if r is None:
        return JSONResponse({"error": "房间不存在"}, status_code=404)
    return JSONResponse(r.to_dict())


@router.put("/{room_id}")
async def update_room(room_id: str, request: Request) -> JSONResponse:
    reg = _registry(request)
    r = reg.get(room_id)
    if r is None:
        return JSONResponse({"error": "房间不存在"}, status_code=404)
    body = await request.json()
    for f in ("name", "purpose", "mode", "policy", "aggregation", "shared_memory"):
        if f in body:
            setattr(r, f, body[f])
    if "participants" in body:
        r.participants = list(body["participants"] or [])
    if "max_rounds" in body:
        r.max_rounds = int(body["max_rounds"] or 6)
    reg.update(r)
    return JSONResponse({"ok": True, "room": r.to_dict()})


@router.delete("/{room_id}")
async def delete_room(room_id: str, request: Request) -> JSONResponse:
    return JSONResponse({"ok": _registry(request).remove(room_id)})


# ── 运行 ──
@router.post("/{room_id}/run")
async def run_room(room_id: str, request: Request) -> JSONResponse:
    reg = _registry(request)
    room = reg.get(room_id)
    if room is None:
        return JSONResponse({"error": "房间不存在"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    user_message = (body or {}).get("message", "") or ""

    # 记忆互通：把房间参与者接到同一 MemoryService（若开启）。
    _apply_shared_memory(request, room)

    if room.mode == "workflow":
        swarm = getattr(request.app.state, "swarm_orchestrator", None)
        if swarm is None:
            return JSONResponse(
                {"ok": False, "error": "swarm_orchestrator 未配置（cognition.swarm 未启用？）"},
                status_code=503,
            )
        from xmclaw.daemon.workflow_room import WorkflowRoomRunner
        out = await WorkflowRoomRunner(room, swarm).run(user_message)
        return JSONResponse(out)

    # chat 模式
    from xmclaw.daemon.group_orchestrator import GroupOrchestrator
    orch = GroupOrchestrator(room, get_agent_loop=lambda a: _get_loop(request, a))
    out = await orch.run_round(user_message)
    return JSONResponse(out)
