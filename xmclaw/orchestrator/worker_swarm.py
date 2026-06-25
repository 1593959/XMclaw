"""WorkerSwarm — parallel task execution via logical Worker Agents.

Each WorkerAgent is a thin wrapper around a shared AgentLoop that
uses an independent ``session_id`` + a capability-trimmed tool
allowlist.  Multiple workers coexist safely on the same asyncio
loop because context is keyed by session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.cognition.graph_executor import GraphExecutor
from xmclaw.cognition.graph_runtime import (
    GraphState,
    apply_updates,
    inspect_graph_state,
)
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
    graph_state: GraphState | None = None


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

    async def execute(
        self, task: Task,
        *, parent_session_id: str | None = None,
        parent_goal: str = "",
        completed_tasks: list[str] | None = None,
    ) -> TaskResult:
        start = time.monotonic()
        session_id = f"worker:{self.worker_id}:{task.task_id}"

        # Enrich subtask prompt with parent context so the worker
        # understands WHY it's doing this and WHAT has already been done.
        _completed = completed_tasks or []
        _completed_summary = "\n".join(f"  - {c}" for c in _completed) or "  (无)"
        enriched_prompt = (
            f"【父任务】{parent_goal}\n\n"
            f"【已完成的前置步骤】\n{_completed_summary}\n\n"
            f"【你的子任务】\n{task.prompt}\n\n"
            f"请专注完成子任务，但始终记住父任务的最终目标。"
            f"返回简洁、可直接整合的结果，不要冗长叙述。"
        )

        # Notify parent session that this worker has started.
        if parent_session_id:
            await self._notify_parent(
                parent_session_id,
                kind="worker_started",
                payload={
                    "worker_id": self.worker_id,
                    "task_id": task.task_id,
                    "prompt_preview": task.prompt[:200],
                },
            )

        result_obj: TaskResult | None = None
        try:
            result = await self._loop.run_turn(
                session_id=session_id,
                user_message=enriched_prompt,
                tools_allowlist=self._tools_allowlist,
            )
            text = getattr(result, "content", "") or getattr(result, "output", "") or ""
            # Best-effort grader score extraction.
            grader = getattr(self._loop, "_grader", None)
            score: float | None = None
            if grader is not None and hasattr(grader, "last_score"):
                score = grader.last_score
            result_obj = TaskResult(
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
            result_obj = TaskResult(
                task_id=task.task_id,
                ok=False,
                error=str(exc),
                elapsed_seconds=time.monotonic() - start,
            )
            if parent_session_id:
                await self._notify_parent(
                    parent_session_id,
                    kind="worker_failed",
                    payload={
                        "worker_id": self.worker_id,
                        "task_id": task.task_id,
                        "error": str(exc)[:500],
                    },
                )

        # Success — notify parent.
        if result_obj is not None and result_obj.ok and parent_session_id:
            await self._notify_parent(
                parent_session_id,
                kind="worker_completed",
                payload={
                    "worker_id": self.worker_id,
                    "task_id": task.task_id,
                    "output_preview": result_obj.output[:500],
                    "elapsed_seconds": round(result_obj.elapsed_seconds, 2),
                },
            )
        return result_obj

    async def _notify_parent(
        self, parent_session_id: str, *, kind: str, payload: dict[str, Any],
    ) -> None:
        """Fire a worker lifecycle event onto the parent's session bus.

        Swallowed on any error — worker progress is best-effort
        observability, not control flow.
        """
        bus = getattr(self._loop, "_bus", None)
        if bus is None:
            return
        try:
            from xmclaw.core.bus.events import EventType, make_event
            event_type = getattr(EventType, kind.upper(), EventType.INNER_MONOLOGUE)
            await bus.publish(make_event(
                session_id=parent_session_id,
                agent_id=f"worker:{self.worker_id}",
                type=event_type,
                payload=payload,
            ))
        except Exception:  # noqa: BLE001
            pass


class WorkerSwarm:
    """Manages a pool of WorkerAgents and executes ExecutionPlans."""

    def __init__(
        self,
        *,
        agent_loop: Any,
        max_workers: int = 4,
        default_tools_allowlist: set[str] | frozenset[str] | None = None,
        task_timeout_s: float = 300.0,
    ) -> None:
        self._agent_loop = agent_loop
        self._max_workers = max(1, int(max_workers))
        self._default_tools = default_tools_allowlist
        self._task_timeout_s = max(1.0, float(task_timeout_s))
        self._workers: dict[str, WorkerAgent] = {}

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        synthesize: bool = True,
        parent_session_id: str | None = None,
    ) -> SwarmResult:
        """Execute all tasks in the plan, respecting dependencies.

        Independent tasks are run in parallel up to ``max_workers``.
        Dependent tasks wait for prerequisites to complete.
        """
        start = time.monotonic()
        results: dict[str, TaskResult] = {}
        task_by_id = {t.task_id: t for t in plan.tasks}
        graph_state = _plan_graph_state(plan, task_timeout_s=self._task_timeout_s)

        async def run_worker_node(node: dict[str, Any], _state: GraphState):
            task_id = str(node.get("id") or "")
            task = task_by_id[task_id]
            worker = self._get_or_create_worker(task)
            result = await worker.execute(
                task,
                parent_session_id=parent_session_id,
                parent_goal=plan.goal,
                completed_tasks=[
                    f"{t.task_id}: {results[t.task_id].output[:80]}"
                    for t in plan.tasks
                    if t.task_id in results and results[t.task_id].ok
                ],
            )
            results[task.task_id] = result
            if not result.ok:
                raise RuntimeError(result.error or "worker returned ok=False")
            return _task_result_graph_updates(task, result)

        execution = await GraphExecutor(max_concurrency=self._max_workers).run(
            graph_state,
            {"swarm_task": run_worker_node},
        )
        graph_state = execution.state
        if not execution.ok and not results:
            _log.error("worker_swarm.deadlock plan_id=%s", plan.plan_id)
            graph_state = apply_updates(graph_state, _swarm_deadlock_updates(plan))

        task_results = [results[t.task_id] for t in plan.tasks if t.task_id in results]
        all_ok = len(task_results) == len(plan.tasks) and all(r.ok for r in task_results)
        synthesized = ""
        if synthesize and task_results:
            synthesized = await self._synthesize(plan, task_results)
        inspection = inspect_graph_state(graph_state)
        graph_state = apply_updates(
            graph_state,
            {
                "final": "completed" if all_ok else "failed",
                "confidence": (
                    len([r for r in task_results if r.ok]) / len(plan.tasks)
                    if plan.tasks
                    else 0.0
                ),
                "metadata": {
                    "inspection": inspection.to_dict(),
                    "synthesized": bool(synthesized),
                    "elapsed_seconds": round(time.monotonic() - start, 3),
                },
            },
        )

        # 2026-05-24 user-report fix: pre-fix the synthesized output
        # only travelled back via the return value, which app.py:2183
        # was discarding. User saw worker_completed status rows in the
        # chat but never the final answer the workers merged into —
        # so two parallel paths (this WorkerSwarm + the main session's
        # parallel_subagents tool call) both fired, the UI showed a
        # race-looking jumble of "worker 执行中" stacked under another
        # path's "修完了" reply.
        # Publish the synthesized output as a fake LLM_RESPONSE on the
        # parent session so the chat UI renders it as a normal
        # assistant message bubble. Best-effort — never raise from
        # the swarm path over a publish failure.
        if parent_session_id and synthesized:
            try:
                from xmclaw.core.bus.events import EventType, make_event
                bus = getattr(self._agent_loop, "_bus", None)
                if bus is not None:
                    await bus.publish(make_event(
                        session_id=parent_session_id,
                        agent_id="swarm",
                        type=EventType.LLM_RESPONSE,
                        payload={
                            "content": synthesized,
                            "ok": all_ok,
                            "source": "worker_swarm",
                            "tool_calls_count": 0,
                            "elapsed_s": round(
                                time.monotonic() - start, 2,
                            ),
                        },
                    ))
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "worker_swarm.publish_synthesized_failed err=%s", exc,
                )

        return SwarmResult(
            plan_id=plan.plan_id,
            ok=all_ok,
            task_results=task_results,
            synthesized_output=synthesized,
            elapsed_seconds=time.monotonic() - start,
            graph_state=graph_state,
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


def _plan_graph_state(plan: ExecutionPlan, *, task_timeout_s: float = 300.0) -> GraphState:
    """Build the canonical graph-state trace for a swarm plan."""
    state = GraphState(
        thread_id=plan.plan_id,
        run_id=plan.plan_id,
        goal=plan.goal,
    )
    return apply_updates(
        state,
        {
            "subtasks": [
                {
                    "id": task.task_id,
                    "index": index,
                    "kind": "swarm_task",
                    "status": "pending",
                    "prompt": task.prompt or task.description,
                    "description": task.description,
                    "dependencies": list(plan.dependencies.get(task.task_id, [])),
                    "agent_id": task.agent_id,
                    "required_capabilities": list(task.required_capabilities),
                }
                for index, task in enumerate(plan.tasks)
            ],
            "node_policies": [
                {
                    "id": task.task_id,
                    "index": index,
                    "kind": "swarm_task",
                    "agent_id": task.agent_id,
                    "max_retries": 0,
                    "timeout_s": task_timeout_s,
                }
                for index, task in enumerate(plan.tasks)
            ],
            "metadata": {
                "source": "worker_swarm",
                "validated": bool(plan.validated),
                "validation_errors": list(plan.validation_errors),
            },
        },
    )


def _swarm_deadlock_updates(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "errors": [
            {
                "kind": "swarm_deadlock",
                "task_id": task.task_id,
                "message": "task dependencies are unsatisfied or cyclic",
                "dependencies": list(plan.dependencies.get(task.task_id, [])),
            }
            for task in plan.tasks
        ],
    }


def _task_result_graph_updates(task: Task, result: TaskResult) -> dict[str, Any]:
    base = {
        "id": result.task_id,
        "task_id": result.task_id,
        "kind": "swarm_task",
        "agent_id": task.agent_id,
        "latency_ms": result.elapsed_seconds * 1000.0,
        "grader_score": result.grader_score,
    }
    updates: dict[str, Any] = {
        "subtasks": {
            **base,
            "status": "completed" if result.ok else "failed",
            "output_preview": result.output[:500],
        },
    }
    if result.ok:
        updates["artifacts"] = {
            **base,
            "kind": "worker_result",
            "content": result.output[:4000],
        }
        updates["messages"] = {
            **base,
            "role": "assistant",
            "content": result.output[:4000],
        }
    else:
        updates["errors"] = {
            **base,
            "kind": "worker_failed",
            "message": result.error or "worker failed",
        }
    return updates
