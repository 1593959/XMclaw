"""GroupRoom + GroupRoomRegistry — 多 agent 群聊房间的模型与持久化.

Group 重做 (2026-06-06)。一个「房间」= 若干 agent 参与者 + 用户共享的一条
对话，加上编排策略(4 选 1)与自定义用途。运行时由 :class:`RoomOrchestrator`
驱动（见 ``room_orchestrator.py``）；本模块只负责**数据模型 + 落盘注册表**，
不持有任何运行时 handle（参与者的 Workspace 仍由 MultiAgentManager 拥有）。

落盘约定：每个房间一个 ``<data>/v2/rooms/<room_id>.json``（peer of
agents registry，见 :func:`xmclaw.utils.paths.rooms_dir`）。注册表线程不安全
以外的并发由调用方（路由层）用一把 asyncio.Lock 串行化 create/remove，
与 MultiAgentManager 同构——这里保持薄。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from xmclaw.utils.paths import rooms_dir

SpeakerPolicy = Literal["round_robin", "supervisor"]
RoomMode = Literal["chat", "workflow"]
# Group 重做(2026-06-06)：统一的 4 种编排策略（用户"都要"）。
#   chat       — 群聊：共享历史 + LLM 选讲者（AutoGen GroupChat）
#   sequential — 固定流水线：A→B→C 顺序接力（CrewAI sequential / MetaGPT）
#   supervisor — 主管派活：主管 LLM 按角色动态分派（CrewAI hierarchical）
#   autonomous — 目标驱动：任务/进度台账 + 内循环 + 重规划（Magentic-One）
Strategy = Literal["chat", "sequential", "supervisor", "autonomous"]

# 房间 session id 约定：``group:<room_id>``。前端订阅这个 session 即可收到
# 房间内所有讲者的事件（事件顶层自带 agent_id）。
GROUP_SESSION_PREFIX = "group:"

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_room_id(raw: str) -> str:
    """房间 id 只允许 [A-Za-z0-9_-]，与 agent_id 同规则。"""
    return _ID_RE.sub("", (raw or "").strip())


def session_id_for(room_id: str) -> str:
    return f"{GROUP_SESSION_PREFIX}{room_id}"


def room_id_from_session(session_id: str) -> str | None:
    if session_id and session_id.startswith(GROUP_SESSION_PREFIX):
        return session_id[len(GROUP_SESSION_PREFIX):]
    return None


@dataclass
class GroupRoom:
    """一个群聊房间的纯数据描述。"""

    room_id: str
    name: str = ""
    # 自定义用途/目标：chat 模式喂给 supervisor 选讲者；workflow 模式作为
    # 编排目标(success goal)喂给 SwarmOrchestrator 拆解。
    purpose: str = ""
    participants: list[str] = field(default_factory=list)  # agent_id 列表
    # 房间形态：
    #   "chat"     — 群聊：agent 之间轮流/主持人发言（GroupOrchestrator）
    #   "workflow" — 目标驱动工作流：目标→拆解→按能力分派→聚合
    #                （WorkflowRoomRunner 复用 SwarmOrchestrator）
    mode: RoomMode = "chat"
    # 统一编排策略（4 选 1，用户"都要"）。空串则由 _resolve_strategy 从
    # mode/policy 兜底推导（向后兼容旧房间 json）。
    strategy: str = ""
    policy: SpeakerPolicy = "round_robin"   # chat 模式的选讲者策略
    aggregation: str = "map_reduce"          # workflow 模式的聚合策略
    max_rounds: int = 6          # chat：一条用户消息后 agent 间最多连说几轮
    shared_memory: bool = True   # 房间内 agent 是否共享同一 MemoryService
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GroupRoom":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def session_id(self) -> str:
        return session_id_for(self.room_id)

    def resolve_strategy(self) -> str:
        """返回生效的编排策略。显式 ``strategy`` 优先；否则从旧 mode/policy 推导：
        workflow → autonomous（目标驱动）；chat + supervisor policy → supervisor；
        其余 → chat。这样旧房间 json 无需迁移也能跑。"""
        s = (self.strategy or "").strip().lower()
        if s in ("chat", "sequential", "supervisor", "autonomous"):
            return s
        if self.mode == "workflow":
            return "autonomous"
        if self.policy == "supervisor":
            return "supervisor"
        return "chat"


class GroupRoomRegistry:
    """``<data>/v2/rooms/*.json`` 的薄注册表（建/删/列/存）。

    并发由调用方串行化（路由层一把锁），与 MultiAgentManager 约定一致。
    """

    def __init__(self, registry_dir: Path | None = None) -> None:
        self._dir = registry_dir if registry_dir is not None else rooms_dir()
        self._rooms: dict[str, GroupRoom] = {}

    # ── 读 ──
    def get(self, room_id: str) -> GroupRoom | None:
        return self._rooms.get(room_id)

    def __contains__(self, room_id: object) -> bool:
        return isinstance(room_id, str) and room_id in self._rooms

    def list_rooms(self) -> list[GroupRoom]:
        return sorted(self._rooms.values(), key=lambda r: r.updated_at, reverse=True)

    # ── 写 ──
    def create(self, room: GroupRoom) -> GroupRoom:
        room.room_id = sanitize_room_id(room.room_id)
        room.updated_at = time.time()
        self._rooms[room.room_id] = room
        self._persist(room)
        return room

    def update(self, room: GroupRoom) -> GroupRoom:
        room.updated_at = time.time()
        self._rooms[room.room_id] = room
        self._persist(room)
        return room

    def remove(self, room_id: str) -> bool:
        if room_id not in self._rooms:
            return False
        del self._rooms[room_id]
        try:
            self._path(room_id).unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def load_from_disk(self) -> list[str]:
        """rehydrate 所有 ``*.json`` 房间，返回加载到的 room_id 列表。"""
        loaded: list[str] = []
        if not self._dir.exists():
            return loaded
        for p in sorted(self._dir.glob("*.json")):
            try:
                room = GroupRoom.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if not room.room_id:
                room.room_id = p.stem
            self._rooms[room.room_id] = room
            loaded.append(room.room_id)
        return loaded

    # ── 内部 ──
    def _path(self, room_id: str) -> Path:
        return self._dir / f"{sanitize_room_id(room_id)}.json"

    def _persist(self, room: GroupRoom) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时再替换，避免半截文件被 load。
        tmp = self._path(room.room_id).with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(room.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path(room.room_id))
