"""Tests for JarvisOrchestrator."""
from __future__ import annotations


from xmclaw.orchestrator.orchestrator import JarvisOrchestrator


class FakeAgentLoop:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_turn(self, session_id: str, user_message: str, **kwargs):
        self.calls.append({"session_id": session_id, "user_message": user_message})
        class _R:
            content = f"echo:{user_message}"
        return _R()


class FakePlanEngine:
    async def create_plan(self, goal, **kwargs):
        from xmclaw.orchestrator.plan_engine import ExecutionPlan, Task
        return ExecutionPlan(
            plan_id="p1",
            goal=goal,
            tasks=[Task(task_id="t1", description="do thing", prompt="do thing")],
            validated=True,
        )


class FakeWorkerSwarm:
    async def execute_plan(self, plan, **kwargs):
        from xmclaw.orchestrator.worker_swarm import SwarmResult, TaskResult
        return SwarmResult(
            plan_id=plan.plan_id,
            ok=True,
            task_results=[TaskResult(task_id="t1", ok=True, output="done")],
            synthesized_output="All done",
        )


class TestJarvisOrchestrator:
    async def test_trivial_path_short_message(self):
        agent = FakeAgentLoop()
        orch = JarvisOrchestrator(agent_loop=agent)
        result = await orch.handle("sid1", "hello")
        assert result.path == "trivial"
        assert result.ok
        assert agent.calls[0]["user_message"] == "hello"

    async def test_complex_path_forced(self):
        agent = FakeAgentLoop()
        orch = JarvisOrchestrator(
            agent_loop=agent,
            plan_engine=FakePlanEngine(),
            worker_swarm=FakeWorkerSwarm(),
        )
        result = await orch.handle("sid1", "hello", force_complex=True)
        assert result.path == "complex"
        assert result.ok
        assert result.output == "All done"

    async def test_complex_fallback_when_unwired(self):
        agent = FakeAgentLoop()
        orch = JarvisOrchestrator(agent_loop=agent)
        result = await orch.handle("sid1", "refactor auth to sqlalchemy")
        assert result.path == "trivial"  # falls back because no plan_engine
        assert result.ok
