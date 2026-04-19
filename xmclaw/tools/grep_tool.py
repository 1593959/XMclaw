"""File content search tool (grep)."""
import re
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import is_path_safe
from xmclaw.utils.paths import BASE_DIR


class GrepTool(Tool):
    name = "grep"
    description = "Search file contents for a regex or literal string pattern."
    parameters = {
        "pattern": {
            "type": "string",
            "description": "Pattern to search for.",
        },
        "directory": {
            "type": "string",
            "description": "Directory to search in. Defaults to workspace root.",
        },
        "is_regex": {
            "type": "boolean",
            "description": "Whether pattern is a regex. Default False.",
        },
        "include_pattern": {
            "type": "string",
            "description": "Optional glob to filter files (e.g., '*.py').",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of matches. Default 50.",
        },
    }

    async def execute(
        self,
        pattern: str,
        directory: str | None = None,
        is_regex: bool = False,
        include_pattern: str | None = None,
        limit: int = 50,
    ) -> str:
        search_dir = Path(directory) if directory else BASE_DIR
        if not search_dir.is_absolute():
            search_dir = BASE_DIR / search_dir

        if not is_path_safe(search_dir, BASE_DIR):
            return "[Error: Directory is outside of allowed workspace]"

        if not search_dir.exists():
            return f"[Error: Directory not found: {directory}]"

        try:
            compiled = re.compile(pattern) if is_regex else None
        except re.error as e:
            return f"[Error: Invalid regex: {e}]"

        matches = []
        for p in search_dir.rglob("*"):
            if not p.is_file():
                continue
            if include_pattern and not p.match(include_pattern):
                continue
            # Skip binary files
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            for i, line in enumerate(text.splitlines(), 1):
                found = False
                if is_regex and compiled:
                    found = bool(compiled.search(line))
                else:
                    found = pattern in line

                if found:
                    rel = str(p.relative_to(BASE_DIR))
                    matches.append(f"{rel}:{i}: {line.strip()}")
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break

        if not matches:
            return f"No matches for '{pattern}'."

        return "\n".join(matches)
