"""Todo management tool."""
import json
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.paths import BASE_DIR

TODO_FILE = BASE_DIR / "agents" / "default" / "workspace" / "todos.json"


class TodoTool(Tool):
    name = "todo"
    description = "Manage todo items: add, list, complete, delete."
    parameters = {
        "action": {
            "type": "string",
            "description": "One of: add, list, complete, delete",
        },
        "text": {
            "type": "string",
            "description": "Todo text for add action.",
        },
        "todo_id": {
            "type": "integer",
            "description": "Todo ID for complete/delete.",
        },
    }

    async def execute(self, action: str, text: str | None = None, todo_id: int | None = None) -> str:
        TODO_FILE.parent.mkdir(parents=True, exist_ok=True)
        todos = []
        if TODO_FILE.exists():
            todos = json.loads(TODO_FILE.read_text(encoding="utf-8"))

        # Normalize todos: ensure every item has an id
        for i, t in enumerate(todos):
            if "id" not in t:
                t["id"] = i + 1

        if action == "add" and text:
            new_id = max([t["id"] for t in todos], default=0) + 1
            todos.append({"id": new_id, "text": text, "done": False})
            self._save(todos)
            return f"Added todo #{new_id}: {text}"

        elif action == "list":
            if not todos:
                return "No todos."
            lines = []
            for t in todos:
                status = "[x]" if t.get("done") else "[ ]"
                lines.append(f"{status} #{t.get('id', '?')}: {t.get('text', '')}")
            return "\n".join(lines)

        elif action == "complete" and todo_id is not None:
            for t in todos:
                if t.get("id") == todo_id:
                    t["done"] = True
                    self._save(todos)
                    return f"Completed todo #{todo_id}"
            return f"Todo #{todo_id} not found"

        elif action == "delete" and todo_id is not None:
            todos = [t for t in todos if t.get("id") != todo_id]
            self._save(todos)
            return f"Deleted todo #{todo_id}"

        return "[Error: Invalid action or missing parameters]"

    def _save(self, todos: list[dict]) -> None:
        TODO_FILE.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")
