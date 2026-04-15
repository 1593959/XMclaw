"""Git integration tool for XMclaw."""
import subprocess
from pathlib import Path

from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class GitTool(Tool):
    name = "git"
    description = "Execute git commands: status, diff, commit, branch, log."
    parameters = {
        "subcommand": {
            "type": "string",
            "enum": ["status", "diff", "commit", "branch", "log"],
            "description": "Git subcommand to execute.",
        },
        "path": {
            "type": "string",
            "description": "Working directory for the git command. Defaults to current agent workspace.",
        },
        "message": {
            "type": "string",
            "description": "Commit message (required for commit subcommand).",
        },
        "args": {
            "type": "string",
            "description": "Additional arguments to pass to git.",
        },
    }

    async def execute(self, subcommand: str, path: str = ".", message: str = "", args: str = "") -> str:
        cwd = Path(path).resolve()
        if not (cwd / ".git").exists():
            # Try to find parent git repo
            for parent in cwd.parents:
                if (parent / ".git").exists():
                    cwd = parent
                    break

        base_cmd = ["git", "-C", str(cwd), subcommand]
        if args:
            base_cmd.extend(args.split())

        if subcommand == "commit":
            if not message:
                return "[Error] commit subcommand requires a message."
            base_cmd.extend(["-m", message])

        try:
            result = subprocess.run(
                base_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                return f"[Git Error] {result.stderr.strip()}"
            return output or "(no output)"
        except Exception as e:
            logger.error("git_tool_error", error=str(e))
            return f"[Error] {e}"
