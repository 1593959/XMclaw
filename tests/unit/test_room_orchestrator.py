"""RoomOrchestrator 单测：4 种编排策略 + 历史增量 + 参与者限定 + 容错.

stub 掉所有注入依赖（agent loop / llm / persona / publish）——不碰真实 LLM/agent。
"""
from __future__ import annotations

import pytest

from xmclaw.daemon.group_room import GroupRoom
from xmclaw.daemon.room_orchestrator import RoomOrchestrator


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text
        self.ok = True


class _FakeLoop:
    """记录每次 run_turn 收到的 (session_id, message)，回固定话。"""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._histories: dict = {}
        self.calls: list[tuple[str, str]] = []

    async def run_turn(self, session_id: str, content: str):
        self.calls.append((session_id, content))
        self._histories.setdefault(session_id, []).append(content)
        return _Result(f"{self.agent_id}说:{len(self.calls)}")


def _loops(*ids: str) -> dict[str, _FakeLoop]:
    return {i: _FakeLoop(i) for i in ids}


# ════════════════════════════════════════════════════════════════════
# 策略 1：chat
# ════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_chat_round_robin_no_llm() -> None:
    loops = _loops("a", "b")
    room = GroupRoom(room_id="r", strategy="chat", participants=["a", "b"], max_rounds=4)
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    out = await orch.run("大家好")
    # 4 轮轮流，排除上一个 → a b a b
    assert out["speakers"] == ["a", "b", "a", "b"]
    assert out["strategy"] == "chat"


@pytest.mark.asyncio
async def test_chat_feeds_delta_not_full_blob() -> None:
    """核心修复：agent 第二次发言只拿到【增量】，不是整段 transcript blob，
    且历史不被清（累积在自己的 session history）。"""
    loops = _loops("a", "b")
    room = GroupRoom(room_id="r", strategy="chat", participants=["a", "b"], max_rounds=3)
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    await orch.run("初始问题")
    a = loops["a"]
    # a 说了 2 次（第1、3轮）；第二次的上下文里不该再出现"初始问题"（那是上次已见的）
    first_ctx = a.calls[0][1]
    second_ctx = a.calls[1][1]
    assert "初始问题" in first_ctx
    assert "初始问题" not in second_ctx  # 增量：只给自上次以来的新消息
    assert "新进展" in second_ctx
    # 历史累积未被清：两次调用后 session history 有 2 条
    assert len(a._histories[room.session_id]) == 2


@pytest.mark.asyncio
async def test_chat_llm_selects_speaker_and_can_stop() -> None:
    loops = _loops("alice", "bob")
    picks = iter(["bob", "alice", "DONE"])

    async def llm(system: str, user: str) -> str:
        return next(picks)

    room = GroupRoom(room_id="r", strategy="chat", participants=["alice", "bob"], max_rounds=6)
    orch = RoomOrchestrator(room, lambda x: loops.get(x), llm_complete=llm)
    out = await orch.run("hi")
    assert out["speakers"] == ["bob", "alice"]  # 第三次 DONE → 终止


@pytest.mark.asyncio
async def test_chat_at_mention_forces_first_speaker() -> None:
    loops = _loops("alice", "bob")
    room = GroupRoom(room_id="r", strategy="chat", participants=["alice", "bob"], max_rounds=1)
    orch = RoomOrchestrator(room, lambda x: loops.get(x))  # 无 LLM
    out = await orch.run("@bob 你先说")
    assert out["speakers"][0] == "bob"  # @点名生效


@pytest.mark.asyncio
async def test_chat_excludes_last_speaker_in_round_robin() -> None:
    loops = _loops("x", "y", "z")
    room = GroupRoom(room_id="r", strategy="chat", participants=["x", "y", "z"], max_rounds=3)
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    out = await orch.run("go")
    # 不连续重复同一个
    sp = out["speakers"]
    assert all(sp[i] != sp[i + 1] for i in range(len(sp) - 1))


# ════════════════════════════════════════════════════════════════════
# 策略 2：sequential
# ════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_sequential_fixed_order_and_handoff() -> None:
    loops = _loops("pm", "dev", "qa")
    room = GroupRoom(room_id="r", strategy="sequential",
                     purpose="做个登录页", participants=["pm", "dev", "qa"])
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    out = await orch.run()
    assert out["speakers"] == ["pm", "dev", "qa"]  # 固定顺序
    # dev 的上下文含 pm 的产出（接力）
    dev_ctx = loops["dev"].calls[0][1]
    assert "pm说" in dev_ctx and "上游交付" in dev_ctx
    # 第一棒 pm 无上游
    assert "流水线第一棒" in loops["pm"].calls[0][1]


@pytest.mark.asyncio
async def test_sequential_empty_goal_fails() -> None:
    room = GroupRoom(room_id="r", strategy="sequential", purpose="", participants=["a"])
    orch = RoomOrchestrator(room, lambda x: _loops("a").get(x))
    out = await orch.run("")
    assert out["ok"] is False


# ════════════════════════════════════════════════════════════════════
# 策略 3：supervisor
# ════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_supervisor_delegates_then_done_then_synthesizes() -> None:
    loops = _loops("researcher", "writer")
    script = iter([
        '{"next": "researcher", "instruction": "查竞品"}',
        '{"next": "writer", "instruction": "写报告"}',
        '{"next": "DONE"}',
        "最终汇总报告",  # synthesize
    ])

    async def llm(system: str, user: str) -> str:
        return next(script)

    room = GroupRoom(room_id="r", strategy="supervisor",
                     purpose="竞品分析", participants=["researcher", "writer"], max_rounds=6)
    orch = RoomOrchestrator(room, lambda x: loops.get(x), llm_complete=llm)
    out = await orch.run()
    assert out["speakers"] == ["researcher", "writer"]
    assert out["result"] == "最终汇总报告"
    # worker 拿到主管指令
    assert "查竞品" in loops["researcher"].calls[0][1]


@pytest.mark.asyncio
async def test_supervisor_without_llm_falls_back_to_sequential() -> None:
    loops = _loops("a", "b")
    room = GroupRoom(room_id="r", strategy="supervisor",
                     purpose="目标", participants=["a", "b"])
    orch = RoomOrchestrator(room, lambda x: loops.get(x))  # 无 llm
    out = await orch.run()
    assert out["speakers"] == ["a", "b"]  # 退化成顺序


# ════════════════════════════════════════════════════════════════════
# 策略 4：autonomous（Magentic-One）
# ════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_autonomous_ledger_loop_and_complete() -> None:
    loops = _loops("worker1", "worker2")
    script = iter([
        "任务台账：步骤1 步骤2",  # build_task_ledger
        '{"complete": false, "stalled": false, "progress": true, "next": "worker1", "instruction": "做步骤1"}',
        '{"complete": false, "stalled": false, "progress": true, "next": "worker2", "instruction": "做步骤2"}',
        '{"complete": true}',  # 完成
        "最终结果",  # synthesize
    ])

    async def llm(system: str, user: str) -> str:
        return next(script)

    room = GroupRoom(room_id="r", strategy="autonomous",
                     purpose="完成复杂任务", participants=["worker1", "worker2"], max_rounds=8)
    orch = RoomOrchestrator(room, lambda x: loops.get(x), llm_complete=llm)
    out = await orch.run()
    assert out["speakers"] == ["worker1", "worker2"]
    assert out["result"] == "最终结果"
    assert "task_ledger" in out


@pytest.mark.asyncio
async def test_autonomous_stall_triggers_replan() -> None:
    loops = _loops("w")
    events: list[str] = []
    script = iter([
        "初始台账",
        '{"complete": false, "stalled": true, "next": "w"}',   # stall 1
        '{"complete": false, "stalled": true, "next": "w"}',   # stall 2 → 触发 replan
        "修订台账",                                              # replan 产出
        '{"complete": true}',
        "结果",
    ])

    async def llm(system: str, user: str) -> str:
        return next(script)

    async def pub(t, p):
        events.append(t)

    room = GroupRoom(room_id="r", strategy="autonomous",
                     purpose="难任务", participants=["w"], max_rounds=8)
    orch = RoomOrchestrator(room, lambda x: loops.get(x), llm_complete=llm,
                            publish=pub, max_stall=2)
    out = await orch.run()
    assert "workflow_replan" in events
    assert out["result"] == "结果"


# ════════════════════════════════════════════════════════════════════
# 横切：参与者限定 / 人格 / 容错
# ════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_only_room_participants_run() -> None:
    """全局有 a/b/c，但房间只拉了 a → 只有 a 被调用（修审计 #5）。"""
    loops = _loops("a", "b", "c")
    room = GroupRoom(room_id="r", strategy="sequential", purpose="x", participants=["a"])
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    await orch.run()
    assert loops["a"].calls and not loops["b"].calls and not loops["c"].calls


@pytest.mark.asyncio
async def test_persona_brief_used_in_prompts() -> None:
    loops = _loops("a", "b")
    seen: dict[str, str] = {}

    async def llm(system: str, user: str) -> str:
        seen["prompt"] = user
        return "DONE"

    personas = {"a": {"role": "研究员", "goal": "查资料"},
                "b": {"role": "写手", "goal": "成稿"}}
    room = GroupRoom(room_id="r", strategy="chat", participants=["a", "b"], max_rounds=2)
    orch = RoomOrchestrator(room, lambda x: loops.get(x), llm_complete=llm,
                            get_persona=lambda i: personas.get(i, {}))
    await orch.run("hi")
    assert "研究员" in seen["prompt"]  # 选讲者 prompt 含人格


@pytest.mark.asyncio
async def test_agent_error_does_not_kill_round() -> None:
    class _Boom(_FakeLoop):
        async def run_turn(self, session_id: str, content: str):
            raise RuntimeError("炸了")

    loops = {"a": _Boom("a"), "b": _FakeLoop("b")}
    room = GroupRoom(room_id="r", strategy="sequential", purpose="x",
                     participants=["a", "b"])
    orch = RoomOrchestrator(room, lambda x: loops.get(x))
    out = await orch.run()
    # a 炸了，b 仍跑
    assert out["ok"] is True
    assert any("出错" in r["text"] for r in out["transcript"])
    assert loops["b"].calls


@pytest.mark.asyncio
async def test_resolve_strategy_back_compat() -> None:
    assert GroupRoom(room_id="x", mode="chat").resolve_strategy() == "chat"
    assert GroupRoom(room_id="x", mode="workflow").resolve_strategy() == "autonomous"
    assert GroupRoom(room_id="x", mode="chat", policy="supervisor").resolve_strategy() == "supervisor"
    assert GroupRoom(room_id="x", strategy="sequential").resolve_strategy() == "sequential"
