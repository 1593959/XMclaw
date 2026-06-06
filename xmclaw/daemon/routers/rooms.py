"""/api/v2/rooms — 多 agent 编排房间 CRUD + 运行.

Group 重做 (2026-06-06)。房间 = 若干 agent 参与者 + 用途 + **4 选 1 编排策略**，
全部经统一内核 :class:`RoomOrchestrator` 跑（按正确范式，见
``docs/audit/MULTI_AGENT_LOGIC_AUDIT_2026.md``）：
- ``chat``       — 群聊：共享历史 + LLM 选讲者（AutoGen GroupChat）
- ``sequential`` — 固定流水线：A→B→C 接力（CrewAI sequential）
- ``supervisor`` — 主管派活：主管 LLM 动态分派（CrewAI hierarchical）
- ``autonomous`` — 目标驱动：任务/进度台账 + 重规划（Magentic-One）

全部**限定在房间参与者内**、用结构化人格选择、明确终止。``POST /{id}/run`` 同步
执行返回结果（可 curl 验证）；每个 agent 的 run_turn 事件经房间 session 实时推
到前端（WS 订阅 ``group:<id>``）。
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


def _persona_of(request: Request, agent_id: str) -> dict[str, Any]:
    """读某参与者的结构化人格(role/goal/backstory/style)，供选讲者/派活判断。
    main → app.state 主 agent 配置；其余 → MultiAgentManager 的 workspace.config。
    人格字段尚未结构化时(G5 前)，尽力从 name/description/system_prompt 兜底。"""
    cfg: dict[str, Any] = {}
    if agent_id in ("main", "", None):
        cfg = getattr(request.app.state, "agent_config", None) or {}
    else:
        mgr = getattr(request.app.state, "agents", None)
        ws = mgr.get(agent_id) if mgr is not None else None
        cfg = (getattr(ws, "config", None) or {}) if ws is not None else {}
    out: dict[str, Any] = {}
    for k in ("role", "goal", "backstory", "style"):
        v = cfg.get(k)
        if v:
            out[k] = v
    if not out.get("role"):
        out["role"] = cfg.get("name") or cfg.get("display_name") or agent_id
    if not out.get("goal"):
        desc = cfg.get("description") or cfg.get("persona") or ""
        if desc:
            out["goal"] = str(desc)[:160]
    return out


def _llm_complete_factory(request: Request, room):
    """造一个 ``async (system, user) -> str`` 的通用 LLM 调用，取任一参与者的
    ``_llm`` provider（群聊主持人/主管/编排器用）。无可用 LLM → 返回 None
    （RoomOrchestrator 会优雅降级为轮流/顺序）。"""
    llm = None
    for aid in [*room.participants, "main"]:
        loop = _get_loop(request, aid)
        cand = getattr(loop, "_llm", None) if loop is not None else None
        if cand is not None:
            llm = cand
            break
    if llm is None:
        return None

    async def _complete(system: str, user: str) -> str:
        from xmclaw.core.ir import Message
        resp = await llm.complete([
            Message(role="system", content=system),
            Message(role="user", content=user),
        ])
        return getattr(resp, "content", "") or ""

    return _complete


def _publish_factory(request: Request, room):
    """造一个 ``async (event_type, payload) -> None`` 把编排元事件(选讲者/计划/
    重规划)推到房间 session，让前端 WS 实时看到工作流推进。"""
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return None

    async def _publish(event_type: str, payload: dict[str, Any]) -> None:
        from xmclaw.core.bus.events import EventType, make_event
        await bus.publish(make_event(
            session_id=room.session_id,
            agent_id=str(payload.get("speaker") or room.room_id),
            type=EventType.INNER_MONOLOGUE,
            payload={"room_event": event_type, **payload},
        ))

    return _publish


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
        strategy=body.get("strategy", "") or "",
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
    for f in ("name", "purpose", "mode", "strategy", "policy", "aggregation", "shared_memory"):
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

    from xmclaw.daemon.room_orchestrator import RoomOrchestrator
    orch = RoomOrchestrator(
        room,
        get_agent_loop=lambda a: _get_loop(request, a),
        llm_complete=_llm_complete_factory(request, room),
        get_persona=lambda a: _persona_of(request, a),
        publish=_publish_factory(request, room),
    )
    out = await orch.run(user_message)
    return JSONResponse(out)
