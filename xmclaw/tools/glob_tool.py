"""File glob search tool."""
import fnmatch
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import is_path_safe
from xmclaw.utils.paths import BASE_DIR


class GlobTool(Tool):
    name = "glob"
    description = "Search for files matching a glob pattern in a directory."
    parameters = {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match (e.g., '*.py', '**/*.json').",
        },
        "directory": {
            "type": "string",
            "description": "Directory to search in. Defaults to workspace root.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results. Default 50.",
        },
    }

    async def execute(
        self,
        pattern: str,
        directory: str | None = None,
        limit: int = 50,
    ) -> str:
        search_dir = Path(directory) if directory else BASE_DIR
        if not search_dir.is_absolute():
            search_dir = BASE_DIR / search_dir

        if not is_path_safe(search_dir, BASE_DIR):
            return "[Error: Directory is outside of allowed workspace]"

        if not search_dir.exists():
            return f"[Error: Directory not found: {directory}]"

        results = []
        # Support **/ recursive patterns
        if "**" in pattern:
            parts = pattern.split("**")
            if len(parts) == 2 and parts[0] in ("", "/"):
                suffix = parts[1].lstrip("/")
                for p in search_dir.rglob(suffix):
                    if p.is_file():
                        results.append(str(p.relative_to(BASE_DIR)))
                        if len(results) >= limit:
                            break
            else:
                for p in search_dir.rglob(pattern):
                    if p.is_file():
                        results.append(str(p.relative_to(BASE_DIR)))
                        if len(results) >= limit:
                            break
        else:
            for p in search_dir.glob(pattern):
                if p.is_file():
                    results.append(str(p.relative_to(BASE_DIR)))
                    if len(results) >= limit:
                        break

        if not results:
            return f"No files matching '{pattern}' found."

        return "\n".join(results)
