"""Group G1 单测：GroupRoom 注册表 + GroupOrchestrator 选讲者/编排循环。"""
from __future__ import annotations

import pytest

from xmclaw.daemon.group_orchestrator import GroupOrchestrator
from xmclaw.daemon.group_room import (
    GroupRoom,
    GroupRoomRegistry,
    room_id_from_session,
    session_id_for,
)


# ── 假 AgentLoop：只实现 orchestrator 用到的 run_turn + _histories ──
class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.ok = True


class _FakeLoop:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._histories: dict[str, list] = {}
        self.calls: list[tuple[str, str]] = []

    async def run_turn(self, session_id: str, content: str):
        self.calls.append((session_id, content))
        # 模拟一次回合也会写 history（orchestrator 应在调用前清掉它）
        self._histories.setdefault(session_id, []).append(content)
        return _FakeResult(f"{self.agent_id} 说了点什么")


# ── 房间注册表 ──
def test_room_registry_roundtrip(tmp_path) -> None:
    reg = GroupRoomRegistry(registry_dir=tmp_path)
    room = GroupRoom(room_id="r1", name="测试房", purpose="头脑风暴",
                     participants=["alice", "bob"], policy="round_robin")
    reg.create(room)
    assert (tmp_path / "r1.json").exists()
    # 新注册表从盘加载
    reg2 = GroupRoomRegistry(registry_dir=tmp_path)
    loaded = reg2.load_from_disk()
    assert "r1" in loaded
    r = reg2.get("r1")
    assert r is not None and r.participants == ["alice", "bob"]
    assert r.purpose == "头脑风暴"
    # session id 约定
    assert r.session_id == "group:r1"
    assert room_id_from_session(session_id_for("r1")) == "r1"
    # 删除
    assert reg2.remove("r1") is True
    assert not (tmp_path / "r1.json").exists()


# ── round_robin ──
@pytest.mark.asyncio
async def test_round_robin_cycles_speakers() -> None:
    loops = {"alice": _FakeLoop("alice"), "bob": _FakeLoop("bob")}
    room = GroupRoom(room_id="r", participants=["alice", "bob"],
                     policy="round_robin", max_rounds=4)
    orch = GroupOrchestrator(room, get_agent_loop=lambda a: loops.get(a))
    out = await orch.run_round("大家讨论一下")
    # 4 轮 → alice, bob, alice, bob
    assert out["speakers"] == ["alice", "bob", "alice", "bob"]
    # 每次调用前 history 被清 → 假 loop 每次只看到 1 条注入上下文
    assert all(len(loops[a]._histories.get("group:r", [])) == 1 for a in loops)
    # transcript 含用户 + 4 条 agent 发言
    assert orch.transcript[0]["speaker"] == "user"
    assert len([t for t in orch.transcript if t["speaker"] != "user"]) == 4


# ── supervisor：LLM 选一次 alice 再交回 USER ──
@pytest.mark.asyncio
async def test_supervisor_selects_then_hands_back() -> None:
    loops = {"alice": _FakeLoop("alice"), "bob": _FakeLoop("bob")}
    seq = iter(["alice", "USER"])

    async def _select(prompt: str) -> str:
        return next(seq)

    room = GroupRoom(room_id="r", purpose="写一段文案",
                     participants=["alice", "bob"], policy="supervisor",
                     max_rounds=5)
    orch = GroupOrchestrator(room, get_agent_loop=lambda a: loops.get(a),
                             llm_select=_select)
    out = await orch.run_round("帮我想个标题")
    assert out["speakers"] == ["alice"]  # 选了 alice，第二轮 USER → 停
    # supervisor prompt 带上了房间用途
    assert loops["alice"].calls  # alice 确实被调用


@pytest.mark.asyncio
async def test_supervisor_without_llm_falls_back_to_round_robin() -> None:
    loops = {"a": _FakeLoop("a"), "b": _FakeLoop("b")}
    room = GroupRoom(room_id="r", participants=["a", "b"],
                     policy="supervisor", max_rounds=2)  # 没传 llm_select
    orch = GroupOrchestrator(room, get_agent_loop=lambda x: loops.get(x))
    out = await orch.run_round("hi")
    assert out["speakers"] == ["a", "b"]


# ── WorkflowRoomRunner（workflow 模式，复用 SwarmOrchestrator 的薄层）──
from xmclaw.daemon.workflow_room import WorkflowRoomRunner


class _FakeSwarmResult:
    def __init__(self) -> None:
        self.ok = True
        self.result = "汇总：竞品分析完成"
        self.assignments = {"task1": "researcher", "task2": "analyst"}
        self.completed = 2
        self.failed = 0
        self.timed_out = 0
        self.elapsed_seconds = 1.2


class _FakeSwarm:
    def __init__(self) -> None:
        self.dispatched = []

    async def dispatch(self, req):
        self.dispatched.append(req)
        return _FakeSwarmResult()


@pytest.mark.asyncio
async def test_workflow_room_runs_and_emits() -> None:
    events: list[tuple[str, dict]] = []

    async def _pub(t, p):
        events.append((t, p))

    room = GroupRoom(room_id="wf", mode="workflow", purpose="做个竞品分析",
                     participants=["researcher", "analyst"], aggregation="map_reduce")
    swarm = _FakeSwarm()
    runner = WorkflowRoomRunner(room, swarm, publish=_pub)
    out = await runner.run()
    assert out["ok"] is True
    assert out["result"] == "汇总：竞品分析完成"
    assert out["assignments"] == {"task1": "researcher", "task2": "analyst"}
    # dispatch 收到目标 = 房间 purpose
    assert swarm.dispatched[0].description == "做个竞品分析"
    # 推了 started / assignments / done 三类事件
    types = [t for t, _ in events]
    assert "workflow_started" in types
    assert "workflow_assignments" in types
    assert "workflow_done" in types


@pytest.mark.asyncio
async def test_workflow_empty_goal_errors() -> None:
    room = GroupRoom(room_id="wf2", mode="workflow", purpose="", participants=["a"])
    runner = WorkflowRoomRunner(room, _FakeSwarm())
    out = await runner.run("")  # 无 purpose 无消息
    assert out["ok"] is False and out.get("error") == "empty goal"


def test_room_mode_default_and_workflow_field() -> None:
    assert GroupRoom(room_id="x").mode == "chat"
    r = GroupRoom(room_id="y", mode="workflow", aggregation="vote")
    assert r.mode == "workflow" and r.aggregation == "vote"
    # 往返保留 mode/aggregation
    assert GroupRoom.from_dict(r.to_dict()).mode == "workflow"
