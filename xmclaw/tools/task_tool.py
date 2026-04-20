"""Task management tool - background task tracking."""
import json
import uuid
from datetime import datetime
from pathlib import Path

from xmclaw.tools.base import Tool
from xmclaw.utils.paths import get_agent_dir


def _task_file(agent_id: str) -> Path:
    """Canonical task-file location for an agent.

    Lives under ``workspace/`` alongside todos.json / plan.md / notes.md
    so the agent's task-level scratch is all in one place (the workspace
    contract defined in ``docs/WORKSPACE.md``).  Hard-coding ``default``
    here used to strand tasks from every non-default agent into the
    default agent's workspace — a silent multi-agent data bug.
    """
    return get_agent_dir(agent_id) / "workspace" / "tasks.json"


class TaskTool(Tool):
    name = "task"
    description = "Create, update, list, and manage background tasks."
    parameters = {
        "action": {
            "type": "string",
            "description": "One of: create, get, list, update, complete, delete",
        },
        "title": {
            "type": "string",
            "description": "Task title for create action.",
        },
        "description": {
            "type": "string",
            "description": "Optional task description.",
        },
        "task_id": {
            "type": "string",
            "description": "Task ID for get/update/complete/delete.",
        },
        "status": {
            "type": "string",
            "description": "New status for update action (pending, in_progress, completed, failed).",
        },
    }

    async def execute(
        self,
        action: str,
        title: str | None = None,
        description: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        agent_id: str = "default",
    ) -> str:
        task_file = _task_file(agent_id)
        task_file.parent.mkdir(parents=True, exist_ok=True)
        tasks = []
        if task_file.exists():
            tasks = json.loads(task_file.read_text(encoding="utf-8"))

        if action == "create" and title:
            new_id = str(uuid.uuid4())[:8]
            task = {
                "id": new_id,
                "title": title,
                "description": description or "",
                "status": "pending",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            tasks.append(task)
            self._save(task_file, tasks)
            return f"Created task {new_id}: {title}"

        elif action == "list":
            if not tasks:
                return "No tasks."
            lines = []
            for t in tasks:
                lines.append(f"[{t['status']}] {t['id']}: {t['title']}")
            return "\n".join(lines)

        elif action == "get" and task_id:
            for t in tasks:
                if t["id"] == task_id:
                    return json.dumps(t, indent=2, ensure_ascii=False)
            return f"Task {task_id} not found"

        elif action == "update" and task_id and status:
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = status
                    t["updated_at"] = datetime.now().isoformat()
                    self._save(task_file, tasks)
                    return f"Updated task {task_id} to {status}"
            return f"Task {task_id} not found"

        elif action == "complete" and task_id:
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = "completed"
                    t["updated_at"] = datetime.now().isoformat()
                    self._save(task_file, tasks)
                    return f"Completed task {task_id}"
            return f"Task {task_id} not found"

        elif action == "delete" and task_id:
            tasks = [t for t in tasks if t["id"] != task_id]
            self._save(task_file, tasks)
            return f"Deleted task {task_id}"

        return "[Error: Invalid action or missing parameters]"

    def _save(self, task_file: Path, tasks: list[dict]) -> None:
        task_file.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
