"""Tests for WorkerSwarm."""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.bus.events import EventType
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


class FakeBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, event) -> None:
        self.published.append({
            "session_id": event.session_id,
            "agent_id": event.agent_id,
            "type": event.type.value if hasattr(event.type, "value") else event.type,
            "payload": event.payload,
        })


class FakeAgentLoopWithBus(FakeAgentLoop):
    def __init__(self) -> None:
        super().__init__()
        self._bus = FakeBus()


class ConcurrentFakeAgentLoop(FakeAgentLoop):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def run_turn(self, session_id: str, user_message: str, **kwargs):
        self.calls.append({"session_id": session_id, "message": user_message})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            class _R:
                content = f"result:{user_message}"
            return _R()
        finally:
            self.active -= 1


class TestWorkerAgent:
    async def test_execute_runs_turn(self):
        loop = FakeAgentLoop()
        worker = WorkerAgent(worker_id="w1", specialty="code", loop=loop)
        task = Task(task_id="t1", description="test", prompt="do test")
        result = await worker.execute(task)
        assert result.ok
        # Phase 6.4: enriched prompt now carries parent context prefix.
        assert result.output.startswith("result:")
        assert "【你的子任务】" in loop.calls[0]["message"]
        assert loop.calls[0]["session_id"].startswith("worker:w1:t1")

    async def test_execute_with_parent_notifies_lifecycle(self):
        """Phase 6.4: WorkerAgent sends WORKER_STARTED/COMPLETED to parent."""
        loop = FakeAgentLoopWithBus()
        worker = WorkerAgent(worker_id="w0", specialty="code", loop=loop)
        task = Task(task_id="tA", description="desc", prompt="do A")
        result = await worker.execute(
            task, parent_session_id="parent-sid-123",
            parent_goal="big goal", completed_tasks=["t0: done"],
        )
        assert result.ok
        published = loop._bus.published
        assert len(published) == 2

        started = published[0]
        assert started["session_id"] == "parent-sid-123"
        assert started["type"] == EventType.WORKER_STARTED.value
        assert started["payload"]["worker_id"] == "w0"
        assert started["payload"]["task_id"] == "tA"
        assert "do A" in started["payload"]["prompt_preview"]

        completed = published[1]
        assert completed["session_id"] == "parent-sid-123"
        assert completed["type"] == EventType.WORKER_COMPLETED.value
        assert completed["payload"]["worker_id"] == "w0"
        assert completed["payload"]["task_id"] == "tA"
        assert "elapsed_seconds" in completed["payload"]

    async def test_execute_failure_notifies_parent(self):
        """Phase 6.4: WorkerAgent sends WORKER_FAILED on exception."""
        class FailingLoop(FakeAgentLoopWithBus):
            async def run_turn(self, session_id: str, user_message: str, **kwargs):
                self.calls.append({"session_id": session_id, "message": user_message})
                raise RuntimeError("boom")

        loop = FailingLoop()
        worker = WorkerAgent(worker_id="w0", specialty="code", loop=loop)
        task = Task(task_id="tB", description="desc", prompt="do B")
        result = await worker.execute(
            task, parent_session_id="parent-sid-456",
        )
        assert not result.ok
        assert "boom" in result.error

        published = loop._bus.published
        assert len(published) == 2
        assert published[0]["type"] == EventType.WORKER_STARTED.value
        assert published[1]["type"] == EventType.WORKER_FAILED.value
        assert published[1]["payload"]["error"] == "boom"


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
        # Phase 6.4: enriched prompt carries parent_goal + completed_tasks.
        assert result.task_results[0].output.startswith("result:")
        assert "【父任务】" in loop.calls[0]["message"]

    async def test_execute_plan_forwards_parent_session_id(self):
        """Phase 6.4: execute_plan passes parent_session_id to each worker."""
        loop = FakeAgentLoopWithBus()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=2)
        plan = ExecutionPlan(
            plan_id="p1",
            goal="test",
            tasks=[
                Task(task_id="t1", description="a", prompt="do a"),
                Task(task_id="t2", description="b", prompt="do b"),
            ],
            dependencies={},
        )
        result = await swarm.execute_plan(
            plan, synthesize=False, parent_session_id="parent-789",
        )
        assert result.ok
        assert len(result.task_results) == 2
        # Both workers should have notified the parent.
        started_events = [
            e for e in loop._bus.published
            if e["type"] == EventType.WORKER_STARTED.value
        ]
        assert len(started_events) == 2
        for ev in started_events:
            assert ev["session_id"] == "parent-789"

    async def test_execute_plan_dependency_chain(self):
        """Tasks with dependencies run sequentially, not in parallel."""
        loop = FakeAgentLoop()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=4)
        plan = ExecutionPlan(
            plan_id="p1",
            goal="chain",
            tasks=[
                Task(task_id="t1", description="first", prompt="do 1"),
                Task(task_id="t2", description="second", prompt="do 2"),
                Task(task_id="t3", description="third", prompt="do 3"),
            ],
            dependencies={"t2": ["t1"], "t3": ["t2"]},
        )
        result = await swarm.execute_plan(plan, synthesize=False)
        assert result.ok
        assert len(result.task_results) == 3
        # t2's enriched prompt should include t1's output
        assert any("result:" in c["message"] for c in loop.calls[1:])

    async def test_execute_plan_uses_graph_executor_concurrency_limit(self):
        loop = ConcurrentFakeAgentLoop()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=2)
        plan = ExecutionPlan(
            plan_id="p-concurrency",
            goal="parallel",
            tasks=[
                Task(task_id=f"t{i}", description=f"task {i}", prompt=f"do {i}")
                for i in range(4)
            ],
            dependencies={},
        )

        result = await swarm.execute_plan(plan, synthesize=False)

        assert result.ok is True
        assert len(result.task_results) == 4
        assert loop.max_active == 2

    async def test_execute_plan_returns_graph_state_trace(self):
        loop = FakeAgentLoop()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=2, task_timeout_s=42.0)
        plan = ExecutionPlan(
            plan_id="p-graph",
            goal="graph trace",
            tasks=[
                Task(task_id="t1", description="a", prompt="do a"),
                Task(task_id="t2", description="b", prompt="do b"),
            ],
            dependencies={"t2": ["t1"]},
        )

        result = await swarm.execute_plan(plan, synthesize=False)

        assert result.ok is True
        assert result.graph_state is not None
        snap = result.graph_state.snapshot()
        assert snap["final"] == "completed"
        assert snap["metadata"]["source"] == "worker_swarm"
        assert snap["subtasks"][1]["dependencies"] == ["t1"]
        assert snap["node_policies"][0]["timeout_s"] == 42.0
        assert [s["status"] for s in snap["subtasks"]] == ["completed", "completed"]
        assert len(snap["artifacts"]) == 2

    async def test_execute_plan_deadlock_is_failed_graph_state(self):
        loop = FakeAgentLoop()
        swarm = WorkerSwarm(agent_loop=loop, max_workers=2)
        plan = ExecutionPlan(
            plan_id="p-cycle",
            goal="cycle",
            tasks=[
                Task(task_id="t1", description="a", prompt="do a"),
                Task(task_id="t2", description="b", prompt="do b"),
            ],
            dependencies={"t1": ["t2"], "t2": ["t1"]},
        )

        result = await swarm.execute_plan(plan, synthesize=False)

        assert result.ok is False
        assert result.task_results == []
        assert result.graph_state is not None
        snap = result.graph_state.snapshot()
        assert snap["final"] == "failed"
        assert snap["errors"][0]["kind"] == "swarm_deadlock"
