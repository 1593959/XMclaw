from __future__ import annotations

import pytest

from xmclaw.cognition.task_scheduler import Task, TaskScheduler


@pytest.mark.asyncio
async def test_task_scheduler_snapshots_tasks_as_graph_state(tmp_path) -> None:
    scheduler = TaskScheduler(db_path=tmp_path / "events.db")
    await scheduler.submit(
        Task(
            id="research",
            prompt="Research implementation options",
            priority=8,
            max_retries=4,
            timeout_seconds=60,
            agent_id="planner",
        ),
    )
    await scheduler.submit(
        Task(
            id="implement",
            prompt="Implement the chosen option",
            priority=6,
            dependencies=["research"],
            max_retries=2,
            timeout_seconds=120,
            agent_id="builder",
        ),
    )
    await scheduler._update_status(
        "research",
        "completed",
        result="option A",
    )
    await scheduler._update_status(
        "implement",
        "escalated",
        error="tool timeout",
        retries=2,
    )

    state = await scheduler.snapshot_graph_state(
        thread_id="goal-1",
        run_id="run-1",
        goal="ship feature",
    )
    snap = state.snapshot()

    assert snap["thread_id"] == "goal-1"
    assert snap["run_id"] == "run-1"
    assert snap["goal"] == "ship feature"
    assert snap["final"] == "completed"
    assert snap["confidence"] == 0.5
    assert [t["id"] for t in snap["subtasks"]] == ["research", "implement"]
    assert snap["subtasks"][1]["dependencies"] == ["research"]
    assert snap["subtasks"][1]["status"] == "escalated"
    assert snap["node_policies"][0]["max_retries"] == 4
    assert snap["node_policies"][1]["timeout_s"] == 120.0
    assert snap["errors"][0]["task_id"] == "implement"
    assert snap["metadata"]["total_tasks"] == 2
    assert snap["metadata"]["failed_tasks"] == 1
    assert snap["metadata"]["inspection"]["ok"] is False
    assert snap["metadata"]["inspection"]["failed_ids"] == ["implement"]


@pytest.mark.asyncio
async def test_task_scheduler_empty_snapshot_is_pending(tmp_path) -> None:
    scheduler = TaskScheduler(db_path=tmp_path / "events.db")

    state = await scheduler.snapshot_graph_state()
    snap = state.snapshot()

    assert snap["final"] == "pending"
    assert snap["confidence"] == 0.0
    assert snap["subtasks"] == []
    assert snap["node_policies"] == []
    assert snap["metadata"]["inspection"]["ok"] is True
