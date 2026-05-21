"""WorkerSwarm — parallel task execution via logical Worker Agents.

Each WorkerAgent is a thin wrapper around a shared AgentLoop that
uses an independent ``session_id`` + a capability-trimmed tool
allowlist.  Multiple workers coexist safely on the same asyncio
loop because context is keyed by session.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.orchestrator.plan_engine import ExecutionPlan, Task
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass(slots=True)
class TaskResult:
    task_id: str
    ok: bool
    output: str = ""
    grader_score: float | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class SwarmResult:
    plan_id: str
    ok: bool
    task_results: list[TaskResult] = field(default_factory=list)
    synthesized_output: str = ""
    elapsed_seconds: float = 0.0


class WorkerAgent:
    """Logical worker that executes one task via AgentLoop.run_turn()."""

    def __init__(
        self,
        *,
        worker_id: str,
        specialty: str,
        loop: Any,  # AgentLoop instance
        tools_allowlist: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.specialty = specialty
        self._loop = loop
        self._tools_allowlist = tools_allowlist

    async def execute(self, task: Task) -> TaskResult:
        start = time.monotonic()
        session_id = f"worker:{self.worker_id}:{task.task_id}"
        try:
            result = await self._loop.run_turn(
                session_id=session_id,
                user_message=task.prompt,
                tools_allowlist=self._tools_allowlist,
            )
            text = getattr(result, "content", "") or getattr(result, "output", "") or ""
            # Best-effort grader score extraction.
            grader = getattr(self._loop, "_grader", None)
            score: float | None = None
            if grader is not None and hasattr(grader, "last_score"):
                score = grader.last_score
            return TaskResult(
                task_id=task.task_id,
                ok=True,
                output=str(text),
                grader_score=score,
                elapsed_seconds=time.monotonic() - start,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "worker_agent.execute_failed worker=%s task=%s err=%s",
                self.worker_id, task.task_id, exc,
            )
            return TaskResult(
                task_id=task.task_id,
                ok=False,
                error=str(exc),
                elapsed_seconds=time.monotonic() - start,
            )


class WorkerSwarm:
    """Manages a pool of WorkerAgents and executes ExecutionPlans."""

    def __init__(
        self,
        *,
        agent_loop: Any,
        max_workers: int = 4,
        default_tools_allowlist: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._agent_loop = agent_loop
        self._max_workers = max_workers
        self._default_tools = default_tools_allowlist
        self._workers: dict[str, WorkerAgent] = {}

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        synthesize: bool = True,
    ) -> SwarmResult:
        """Execute all tasks in the plan, respecting dependencies.

        Independent tasks are run in parallel up to ``max_workers``.
        Dependent tasks wait for prerequisites to complete.
        """
        start = time.monotonic()
        results: dict[str, TaskResult] = {}
        pending = {t.task_id: t for t in plan.tasks}
        deps = {t.task_id: set(plan.dependencies.get(t.task_id, [])) for t in plan.tasks}

        while pending:
            # Find tasks whose dependencies are all satisfied.
            ready_ids = [
                tid for tid, task in pending.items()
                if not deps[tid] or all(d in results and results[d].ok for d in deps[tid])
            ]
            if not ready_ids:
                # Cycle or broken dependency — break to avoid infinite loop.
                _log.error("worker_swarm.deadlock plan_id=%s", plan.plan_id)
                break

            # Batch size limited by max_workers.
            batch = ready_ids[: self._max_workers]
            batch_tasks = [pending[tid] for tid in batch]

            # Create / reuse workers for this batch.
            coros: list[asyncio.Task[TaskResult]] = []
            for task in batch_tasks:
                worker = self._get_or_create_worker(task)
                coros.append(asyncio.create_task(worker.execute(task)))

            batch_results = await asyncio.gather(*coros, return_exceptions=True)
            for task, res in zip(batch_tasks, batch_results):
                if isinstance(res, BaseException):  # noqa: BLE001
                    results[task.task_id] = TaskResult(
                        task_id=task.task_id,
                        ok=False,
                        error=str(res),
                    )
                else:
                    results[task.task_id] = res
                del pending[task.task_id]

        task_results = [results[t.task_id] for t in plan.tasks if t.task_id in results]
        all_ok = all(r.ok for r in task_results)
        synthesized = ""
        if synthesize and task_results:
            synthesized = await self._synthesize(plan, task_results)

        return SwarmResult(
            plan_id=plan.plan_id,
            ok=all_ok,
            task_results=task_results,
            synthesized_output=synthesized,
            elapsed_seconds=time.monotonic() - start,
        )

    def _get_or_create_worker(self, task: Task) -> WorkerAgent:
        """Pick or mint a WorkerAgent for a task."""
        # Simple heuristic: one worker per specialty, round-robin on collision.
        specialty = self._infer_specialty(task)
        key = f"{specialty}_{len(self._workers) % self._max_workers}"
        if key not in self._workers:
            self._workers[key] = WorkerAgent(
                worker_id=key,
                specialty=specialty,
                loop=self._agent_loop,
                tools_allowlist=self._capability_tools(task.required_capabilities),
            )
        return self._workers[key]

    @staticmethod
    def _infer_specialty(task: Task) -> str:
        caps = " ".join(task.required_capabilities).lower()
        if "code" in caps or "edit" in caps or "refactor" in caps:
            return "code"
        if "web" in caps or "search" in caps or "browser" in caps:
            return "research"
        if "bash" in caps or "docker" in caps or "ssh" in caps:
            return "ops"
        if "channel" in caps or "message" in caps or "email" in caps:
            return "comm"
        return "general"

    def _capability_tools(
        self, capabilities: list[str],
    ) -> set[str] | frozenset[str] | None:
        """Map capability slugs to actual tool names."""
        # TODO(Jarvis J2 #15): build a real capability→tool mapping from registry.
        # For now, return the default allowlist (all tools) so workers
        # don't starve.  Future iteration trims by capability.
        return self._default_tools

    async def _synthesize(
        self, plan: ExecutionPlan, results: list[TaskResult],
    ) -> str:
        """Best-effort LLM synthesis of worker outputs into a unified answer."""
        llm = getattr(self._agent_loop, "_llm", None)
        ok_outputs = [r for r in results if r.ok]
        if llm is None:
            return "\n\n---\n\n".join(
                f"[{r.task_id}]\n{r.output}" for r in ok_outputs
            )
        try:
            from xmclaw.core.ir import Message

            # Build pieces with explicit truncation markers so the LLM
            # knows context was cut (prevents hallucination).
            pieces: list[str] = []
            for r in ok_outputs:
                truncated = len(r.output) > 800
                snippet = r.output[:800] + (" …[truncated]" if truncated else "")
                pieces.append(f"- {r.task_id}: {snippet}")

            failed = [r for r in results if not r.ok]
            failure_note = ""
            if failed:
                failure_note = (
                    "\nNote: the following tasks failed — "
                    f"{', '.join(r.task_id for r in failed)}.\n"
                )

            system_prompt = (
                "You are a synthesis assistant. Combine the provided task "
                "outputs into a single coherent summary that answers the "
                "user's original goal. Do not list raw step outputs. "
                "If a task failed, note it briefly. Keep the summary under "
                "300 words. Respond in the same language as the user's goal."
            )
            user_prompt = (
                f"Goal: {plan.goal}\n"
                f"{failure_note}\n"
                f"Task outputs:\n"
                + "\n".join(pieces)
                + "\n\nSynthesize a concise final answer:"
            )
            resp = await llm.complete(
                [
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                tools=None,
            )
            return str(getattr(resp, "content", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            _log.warning("worker_swarm.synthesize_failed: %s", exc)
            return "\n\n---\n\n".join(
                f"[{r.task_id}]\n{r.output}" for r in ok_outputs
            )
