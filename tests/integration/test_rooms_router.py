"""Group G2 集成测试：/api/v2/rooms CRUD + /run（chat & workflow）。

TestClient 打真实 URL（跨前后端边界纪律），app.state 注入假 agent / swarm。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xmclaw.daemon.group_room import GroupRoomRegistry
from xmclaw.daemon.routers import rooms as rooms_router


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.ok = True


class _FakeLoop:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._histories: dict = {}

    async def run_turn(self, session_id: str, content: str):
        self._histories.setdefault(session_id, []).append(content)
        return _FakeResult(f"{self.agent_id}: 收到")


class _FakeSwarmResult:
    ok = True
    result = "工作流完成：汇总结果"
    assignments = {"t1": "main"}
    completed = 1
    failed = 0
    timed_out = 0
    elapsed_seconds = 0.5


class _FakeSwarm:
    async def dispatch(self, req):
        return _FakeSwarmResult()


def _build_app(tmp_path) -> FastAPI:
    app = FastAPI()
    app.include_router(rooms_router.router)
    # 隔离的房间注册表（tmp 目录）
    app.state.rooms = GroupRoomRegistry(registry_dir=tmp_path)
    app.state.agent = _FakeLoop("main")        # chat /run 用
    app.state.agents = None
    app.state.swarm_orchestrator = _FakeSwarm()  # workflow /run 用
    app.state.memory_v2_service = object()       # 共享记忆 sentinel
    return app


def test_room_crud_roundtrip(tmp_path) -> None:
    with TestClient(_build_app(tmp_path)) as c:
        # 建
        r = c.post("/api/v2/rooms", json={
            "name": "脑暴室", "purpose": "想标题", "participants": ["main"],
            "mode": "chat", "policy": "round_robin", "max_rounds": 1,
        })
        assert r.status_code == 200, r.text
        rid = r.json()["room"]["room_id"]
        # 列
        assert any(x["room_id"] == rid for x in c.get("/api/v2/rooms").json()["rooms"])
        # 取
        got = c.get(f"/api/v2/rooms/{rid}").json()
        assert got["purpose"] == "想标题" and got["participants"] == ["main"]
        # 改
        r2 = c.put(f"/api/v2/rooms/{rid}", json={"purpose": "改了", "max_rounds": 2})
        assert r2.json()["room"]["purpose"] == "改了"
        # 删
        assert c.delete(f"/api/v2/rooms/{rid}").json()["ok"] is True
        assert c.get(f"/api/v2/rooms/{rid}").status_code == 404


def test_chat_room_run(tmp_path) -> None:
    with TestClient(_build_app(tmp_path)) as c:
        c.post("/api/v2/rooms", json={
            "room_id": "chat1", "participants": ["main"], "mode": "chat", "max_rounds": 1,
        })
        out = c.post("/api/v2/rooms/chat1/run", json={"message": "大家好"}).json()
        assert out["speakers"] == ["main"]
        assert any(t["speaker"] == "main" for t in out["transcript"])


def test_workflow_room_run(tmp_path) -> None:
    with TestClient(_build_app(tmp_path)) as c:
        c.post("/api/v2/rooms", json={
            "room_id": "wf1", "purpose": "做竞品分析", "participants": ["main"],
            "mode": "workflow",
        })
        out = c.post("/api/v2/rooms/wf1/run", json={}).json()
        assert out["ok"] is True
        assert out["result"] == "工作流完成：汇总结果"


def test_run_unknown_room_404(tmp_path) -> None:
    with TestClient(_build_app(tmp_path)) as c:
        assert c.post("/api/v2/rooms/nope/run", json={}).status_code == 404


def test_shared_memory_wires_participants(tmp_path) -> None:
    """房间 shared_memory=true 跑一次后，参与者的 _memory_service 被接到
    app.state.memory_v2_service（同一实例 → 记忆互通）。"""
    app = _build_app(tmp_path)
    shared = app.state.memory_v2_service
    with TestClient(app) as c:
        c.post("/api/v2/rooms", json={
            "room_id": "smem", "participants": ["main"], "mode": "chat",
            "max_rounds": 1, "shared_memory": True,
        })
        # 跑之前 main loop 的 memory 还不是共享实例
        assert getattr(app.state.agent, "_memory_service", None) is not shared
        c.post("/api/v2/rooms/smem/run", json={"message": "hi"})
        # 跑之后被接到共享实例
        assert app.state.agent._memory_service is shared


def test_shared_memory_off_does_not_wire(tmp_path) -> None:
    app = _build_app(tmp_path)
    shared = app.state.memory_v2_service
    with TestClient(app) as c:
        c.post("/api/v2/rooms", json={
            "room_id": "nomem", "participants": ["main"], "mode": "chat",
            "max_rounds": 1, "shared_memory": False,
        })
        c.post("/api/v2/rooms/nomem/run", json={"message": "hi"})
        assert getattr(app.state.agent, "_memory_service", None) is not shared
