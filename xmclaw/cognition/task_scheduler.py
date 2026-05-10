"""TaskScheduler — 任务 DAG 调度器。

取代 CronTickTask 的纯定时模式，支持：
- 任务依赖拓扑排序
- 状态机: PENDING → RUNNING → COMPLETED / FAILED → RETRYING → ESCALATED
- 优先级抢占
- 自愈重试（指数退避）
- 进度可视化
"""
from __future__ import annotations

import asyncio
import heapq
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

TaskStatus = Literal["pending", "blocked", "running", "completed", "failed", "retrying", "escalated"]

# Jarvisification Phase 5: reuse the events.db file instead of a
# separate tasks.db.  Task state changes are events — they belong
# in the same substrate.  The tasks table co-exists with the event
# bus tables (events, sessions) in the same SQLite WAL.
from xmclaw.utils.paths import default_events_db_path

_DEFAULT_DB_PATH = default_events_db_path()


@dataclass(frozen=True, slots=True)
class Task:
    """一个任务。"""

    id: str
    prompt: str
    priority: int = 5  # 1-10, 10 最高
    dependencies: list[str] = field(default_factory=list)
    status: TaskStatus = "pending"
    retries: int = 0
    max_retries: int = 3
    timeout_seconds: int = 300
    agent_id: str = "main"
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "status": self.status,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            prompt=data["prompt"],
            priority=data.get("priority", 5),
            dependencies=list(data.get("dependencies", [])),
            status=data.get("status", "pending"),
            retries=data.get("retries", 0),
            max_retries=data.get("max_retries", 3),
            timeout_seconds=data.get("timeout_seconds", 300),
            agent_id=data.get("agent_id", "main"),
            created_at=data.get("created_at", 0.0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            result=data.get("result"),
        )


class TaskScheduler:
    """任务 DAG 调度器。"""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        max_concurrent: int = 3,
        executor: Callable[[Task], Awaitable[str]] | None = None,
        bus: Any | None = None,
    ) -> None:
        self.db_path = str(db_path or _DEFAULT_DB_PATH)
        self.max_concurrent = max_concurrent
        self.executor = executor
        self._bus = bus
        self._conn = self._open_conn()
        self._ensure_schema()
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running = False
        self._stop_event = asyncio.Event()
        try:
            self._lock: asyncio.Lock | None = asyncio.Lock()
        except RuntimeError:
            self._lock = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _open_conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 5,
                dependencies TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                retries INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                timeout_seconds INTEGER NOT NULL DEFAULT 300,
                agent_id TEXT NOT NULL DEFAULT 'main',
                created_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                error TEXT,
                result TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_priority ON tasks(priority DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_agent ON tasks(agent_id)")
        self._conn.commit()

    # ── 公共 API ──

    async def submit(self, task: Task) -> str:
        """提交任务。检查依赖，如未满足则置为 BLOCKED。"""
        async with self._get_lock():
            # 检查依赖环
            if self._has_cycle(task):
                raise ValueError(f"Task {task.id} dependency cycle detected")

            # 检查依赖是否满足
            deps_met = await self._dependencies_met(task.dependencies)
            task_id = task.id or uuid.uuid4().hex
            task = Task(
                **{**task.to_dict(), "id": task_id, "status": "pending" if deps_met else "blocked", "created_at": time.time()}
            )
            self._save_task(task)

        if deps_met and self._running:
            self._wake_scheduler()

        return task_id

    async def cancel(self, task_id: str) -> bool:
        """取消任务。"""
        task = await self.get_task(task_id)
        if task is None or task.status in ("completed", "failed", "escalated"):
            return False

        # 取消正在运行的 asyncio.Task
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]

        await self._update_status(task_id, "failed", error="cancelled by user")
        return True

    async def get_status(self, task_id: str) -> TaskStatus | None:
        task = await self.get_task(task_id)
        return task.status if task else None

    async def get_task(self, task_id: str) -> Task | None:
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_dict(dict(row)) if row else None

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """列出任务。"""
        where = ["1=1"]
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)

        cur = self._conn.cursor()
        rows = cur.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY priority DESC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [Task.from_dict(dict(r)) for r in rows]

    async def get_progress(self, task_id: str) -> dict[str, Any]:
        """获取任务进度。"""
        task = await self.get_task(task_id)
        if task is None:
            return {"status": "not_found"}

        elapsed = 0.0
        if task.started_at:
            elapsed = time.time() - task.started_at

        dep_status: dict[str, str] = {}
        for dep_id in task.dependencies:
            dep = await self.get_task(dep_id)
            dep_status[dep_id] = dep.status if dep else "unknown"

        return {
            "status": task.status,
            "elapsed_seconds": round(elapsed, 1),
            "retries": task.retries,
            "max_retries": task.max_retries,
            "dependency_status": dep_status,
            "error": task.error,
            "result_preview": (task.result or "")[:200] if task.result else None,
        }

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动调度循环。"""
        self._running = True
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(
            self._schedule_loop(), name="task-scheduler"
        )

    async def stop(self) -> None:
        """优雅停止。"""
        self._running = False
        self._stop_event.set()
        if hasattr(self, "_scheduler_task"):
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._scheduler_task.cancel()
        # 取消所有运行中的任务
        for t in list(self._running_tasks.values()):
            t.cancel()
        self._running_tasks.clear()

    # ── 内部调度 ──

    def _wake_scheduler(self) -> None:
        """唤醒调度循环（如果有正在等待的）。"""
        # 通过重新创建任务来唤醒
        pass  # 实际实现中可通过 asyncio.Event 实现

    async def _schedule_loop(self) -> None:
        """主调度循环。"""
        while self._running:
            # 获取可运行的 pending 任务
            pending = await self.list_tasks(status="pending", limit=self.max_concurrent * 2)

            # 按优先级排序
            pending.sort(key=lambda t: -t.priority)

            for task in pending:
                if len(self._running_tasks) >= self.max_concurrent:
                    break
                if task.id in self._running_tasks:
                    continue

                await self._update_status(task.id, "running", started_at=time.time())
                coro = self._execute_with_timeout(task)
                self._running_tasks[task.id] = asyncio.create_task(coro, name=f"task-{task.id}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                # 清理已完成的任务
                done = [tid for tid, t in self._running_tasks.items() if t.done()]
                for tid in done:
                    del self._running_tasks[tid]
                continue
            break

    async def _execute_with_timeout(self, task: Task) -> None:
        """执行任务，带超时控制。"""
        try:
            if self.executor is None:
                await self._update_status(
                    task.id, "failed", error="no executor configured"
                )
                return

            result = await asyncio.wait_for(
                self.executor(task), timeout=task.timeout_seconds
            )
            await self._update_status(
                task.id, "completed", result=result, completed_at=time.time()
            )
            # 通知依赖该任务的其他任务
            await self._notify_dependents(task.id)

        except asyncio.TimeoutError:
            await self._on_execution_failed(task, "timeout")
        except Exception as exc:
            await self._on_execution_failed(task, f"{type(exc).__name__}: {exc}")

    async def _on_execution_failed(self, task: Task, error: str) -> None:
        """处理执行失败。"""
        if task.retries < task.max_retries:
            # 指数退避
            delay = 2 ** task.retries
            await self._update_status(
                task.id, "retrying", error=error, retries=task.retries + 1
            )
            await asyncio.sleep(delay)
            await self._update_status(task.id, "pending")
        else:
            await self._update_status(
                task.id, "escalated", error=error, completed_at=time.time()
            )
            await self._notify_dependents(task.id)  # 依赖方将永远 blocked

    async def _notify_dependents(self, completed_task_id: str) -> None:
        """通知依赖该任务的其他任务检查是否可以解锁。"""
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT * FROM tasks WHERE status = 'blocked' AND dependencies LIKE ?",
            (f'%"{completed_task_id}"%',),
        ).fetchall()

        for row in rows:
            task = Task.from_dict(dict(row))
            if await self._dependencies_met(task.dependencies):
                await self._update_status(task.id, "pending")

    # ── helpers ──

    def _has_cycle(self, task: Task, visited: set[str] | None = None) -> bool:
        """检查任务依赖是否有环。"""
        visited = visited or set()
        if task.id in visited:
            return True
        visited.add(task.id)
        for dep_id in task.dependencies:
            dep = self._get_task_sync(dep_id)
            if dep is None:
                continue
            if self._has_cycle(dep, visited.copy()):
                return True
        return False

    def _get_task_sync(self, task_id: str) -> Task | None:
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_dict(dict(row)) if row else None

    async def _dependencies_met(self, dependencies: list[str]) -> bool:
        """检查所有依赖是否已完成。"""
        for dep_id in dependencies:
            dep = await self.get_task(dep_id)
            if dep is None or dep.status != "completed":
                return False
        return True

    def _save_task(self, task: Task) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, prompt, priority, dependencies, status, retries, max_retries,
             timeout_seconds, agent_id, created_at, started_at, completed_at, error, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id, task.prompt, task.priority, json.dumps(task.dependencies),
                task.status, task.retries, task.max_retries, task.timeout_seconds,
                task.agent_id, task.created_at, task.started_at, task.completed_at,
                task.error, task.result,
            ),
        )
        self._conn.commit()

    async def _update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
        result: str | None = None,
        retries: int | None = None,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> None:
        async with self._get_lock():
            cur = self._conn.cursor()
            updates = ["status = ?"]
            params: list[Any] = [status]
            if error is not None:
                updates.append("error = ?")
                params.append(error)
            if result is not None:
                updates.append("result = ?")
                params.append(result)
            if retries is not None:
                updates.append("retries = ?")
                params.append(retries)
            if started_at is not None:
                updates.append("started_at = ?")
                params.append(started_at)
            if completed_at is not None:
                updates.append("completed_at = ?")
                params.append(completed_at)
            params.append(task_id)
            cur.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            self._conn.commit()
