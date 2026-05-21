"""Tests for WorkerSwarm."""
from __future__ import annotations


from xmclaw.orchestrator.plan_engine import ExecutionPlan, Task
from xmclaw.orchestrator.worker_swarm import WorkerAgent, WorkerSwarm


class FakeAgentLoop:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_turn(self, session_id: str, user_message: str, **kwargs):
        self.calls.append({"session_id": session_id, "message": user_message})
        class _R:
            content = f"result:{user_message}"
        return _R()


class TestWorkerAgent:
    async def test_execute_runs_turn(self):
        loop = FakeAgentLoop()
        worker = WorkerAgent(worker_id="w1", specialty="code", loop=loop)
        task = Task(task_id="t1", description="test", prompt="do test")
        result = await worker.execute(task)
        assert result.ok
        assert result.output == "result:do test"
        assert loop.calls[0]["session_id"].startswith("worker:w1:t1")


class TestWorkerSwarm:
    async def test_execute_plan_single_task(self):
        loop = FakeAgentLoop()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=2)
        plan = ExecutionPlan(
            plan_id="p1",
            goal="test",
            tasks=[Task(task_id="t1", description="a", prompt="do a")],
            dependencies={},
        )
        result = await swarm.execute_plan(plan, synthesize=False)
        assert result.ok
        assert len(result.task_results) == 1
        assert result.task_results[0].output == "result:do a"
