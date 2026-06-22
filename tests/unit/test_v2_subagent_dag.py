"""#2 修复：parallel_subagents 的 DAG 依赖调度。

验证「后置任务需要前置结果」不再并行盲跑：
  * _normalize_deps 只保留后向边（d<i），防环。
  * 有依赖的节点等前置完成、并把前置产出注入自己的 prompt。
  * 无依赖的节点并行启动。

用 asyncio.run 包同步测试函数，不依赖 pytest-asyncio 的 asyncio_mode。
"""
from __future__ import annotations

import asyncio

from xmclaw.providers.tool.builtin_subagent import (
    SubagentToolProvider,
    _SubResult,
    _normalize_deps,
)


def test_normalize_deps_backward_edges_only():
    # 越界(5)、负(-1)、重复(2,2) 全滤，只留 d<i 的后向边。
    assert _normalize_deps([[], [0], [0, 1], [5, -1, 2, 2]], 4) == [[], [0], [0, 1], [2]]
    # 缺失 / 非 list → 全空（= 全并行）。
    assert _normalize_deps(None, 3) == [[], [], []]
    assert _normalize_deps("nope", 2) == [[], []]
    # 自环(0→0) 与前向引用(1→2) 一律丢弃，保证无环。
    assert _normalize_deps([[0], [2], []], 3) == [[], [], []]
    # bool 不当 int 用。
    assert _normalize_deps([[], [True]], 2) == [[], []]


def test_fanout_dag_orders_and_injects_dep_outputs():
    prov = SubagentToolProvider(llm=object(), max_concurrency=8)
    started: list[int] = []
    finished: list[int] = []
    seen: dict[int, str] = {}

    async def fake_run_one(index, subtask, *, role="general", max_hops=None,
                           specialist="", depth=1):
        started.append(index)
        seen[index] = subtask
        # 让两个根任务慢一点 —— 若依赖未被尊重，#2/#3 会先于它们完成。
        await asyncio.sleep(0.05 if index in (0, 1) else 0.0)
        finished.append(index)
        return _SubResult(index=index, subtask=subtask, ok=True, content=f"OUT{index}")

    prov._run_one = fake_run_one  # type: ignore[assignment]

    subtasks = ["research A", "research B", "synthesize 0+1", "plan from 2"]
    deps = [[], [], [0, 1], [2]]  # #2 依赖 #0#1；#3 依赖 #2

    results = asyncio.run(prov._fanout(
        subtasks, roles=["general"] * 4, max_hops=5,
        specialists=[""] * 4, deps=deps,
    ))

    assert [r.index for r in results] == [0, 1, 2, 3]
    assert all(r.ok for r in results)

    # 依赖顺序：#0#1 必须在 #2 启动前完成；#2 必须在 #3 启动前完成。
    assert finished.index(0) < started.index(2)
    assert finished.index(1) < started.index(2)
    assert finished.index(2) < started.index(3)

    # 前置产出注入了后置 prompt（这是「后置需要前置结果」的核心）。
    assert "OUT0" in seen[2] and "OUT1" in seen[2]
    assert "前置任务 #0" in seen[2]
    assert "OUT2" in seen[3]
    # 无依赖根任务拿到的是原始 subtask（无注入）。
    assert seen[0] == "research A"


def test_fanout_no_deps_all_parallel():
    prov = SubagentToolProvider(llm=object(), max_concurrency=8)

    async def fake_run_one(index, subtask, **kw):
        return _SubResult(index=index, subtask=subtask, ok=True, content=f"OUT{index}")

    prov._run_one = fake_run_one  # type: ignore[assignment]

    results = asyncio.run(prov._fanout(
        ["a", "b", "c"], roles=["general"] * 3, max_hops=5,
        specialists=[""] * 3, deps=None,
    ))
    assert sorted(r.index for r in results) == [0, 1, 2]
    assert all(r.ok for r in results)
