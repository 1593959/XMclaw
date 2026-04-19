"""File read tool."""
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import is_path_safe
from xmclaw.utils.paths import BASE_DIR


class FileReadTool(Tool):
    name = "file_read"
    description = "Read a text file. Returns file content."
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Absolute or workspace-relative path to the file.",
        },
        "start_line": {
            "type": "integer",
            "description": "Optional. 1-based start line.",
        },
        "end_line": {
            "type": "integer",
            "description": "Optional. 1-based end line.",
        },
    }

    async def execute(self, file_path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        target = Path(file_path)
        if not target.is_absolute():
            target = BASE_DIR / target

        if not is_path_safe(target, BASE_DIR):
            return "[Error: Path is outside of allowed workspace]"

        if not target.exists():
            return f"[Error: File not found: {file_path}]"

        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return "[Error: File is not a valid text file]"

        if start_line is not None or end_line is not None:
            start = (start_line or 1) - 1
            end = end_line or len(lines)
            lines = lines[start:end]

        return "\n".join(lines)
