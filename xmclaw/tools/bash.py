"""Shell command execution tool."""
import asyncio
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import is_path_safe
from xmclaw.utils.paths import BASE_DIR


class BashTool(Tool):
    name = "bash"
    description = "Execute a shell command. Returns stdout and stderr."
    parameters = {
        "command": {
            "type": "string",
            "description": "Shell command to execute.",
        },
        "cwd": {
            "type": "string",
            "description": "Optional working directory.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds. Default 60.",
        },
    }

    def _resolve_python(self) -> str:
        """Find bundled or system Python for executing python commands."""
        bundled = BASE_DIR / "python" / "python.exe"
        if bundled.exists():
            return str(bundled)
        return "python"

    def _rewrite_command(self, command: str) -> str:
        """Rewrite python commands to use bundled python if available."""
        cmd = command.strip()
        if cmd.startswith("python ") or cmd.startswith("python3 "):
            py = self._resolve_python()
            return py + cmd[cmd.find(" "):]
        if cmd.startswith("py "):
            py = self._resolve_python()
            return py + cmd[2:]
        return cmd

    async def execute(self, command: str, cwd: str | None = None, timeout: int = 60) -> str:
        work_dir = Path(cwd) if cwd else BASE_DIR
        if not work_dir.is_absolute():
            work_dir = BASE_DIR / work_dir
        if not is_path_safe(work_dir, BASE_DIR):
            return "[Error: Working directory is outside of allowed workspace]"

        command = self._rewrite_command(command)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                return f"[Exit {proc.returncode}] {output}\n{err}"
            return output or err or "[No output]"
        except asyncio.TimeoutError:
            proc.kill()
            return f"[Error: Command timed out after {timeout}s]"
        except Exception as e:
            return f"[Error: {e}]"
