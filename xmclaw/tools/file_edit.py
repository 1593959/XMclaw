"""File edit tool (find and replace)."""
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import is_path_safe
from xmclaw.utils.paths import BASE_DIR


class FileEditTool(Tool):
    name = "file_edit"
    description = "Find and replace text in a file."
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Path to the file.",
        },
        "old_text": {
            "type": "string",
            "description": "Exact text to find.",
        },
        "new_text": {
            "type": "string",
            "description": "Text to replace with.",
        },
    }

    async def execute(self, file_path: str, old_text: str, new_text: str) -> str:
        target = Path(file_path)
        if not target.is_absolute():
            target = BASE_DIR / target

        if not is_path_safe(target, BASE_DIR):
            return "[Error: Path is outside of allowed workspace]"

        if not target.exists():
            return f"[Error: File not found: {file_path}]"

        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return "[Error: old_text not found in file]"

        content = content.replace(old_text, new_text)
        target.write_text(content, encoding="utf-8")
        return f"File edited: {target}"
