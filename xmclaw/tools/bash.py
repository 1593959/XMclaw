"""Shell command execution tool with permission classification."""
import asyncio
import re
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

    # Dangerous patterns that should be blocked or require explicit confirmation
    DANGEROUS_PATTERNS = [
        (r"\brm\s+-rf\s+/\b", "Destructive filesystem operation"),
        (r"\bdd\s+if=.*of=/dev/", "Direct disk write"),
        (r"\bmv\s+.*\s+/\b", "Moving files to root"),
        (r"\bformat\b", "Disk format operation"),
        (r"\bdel\s+/[fFq]", "Force delete operation"),
        (r"\brd\s+/s\s+/q", "Force directory removal"),
    ]

    # Patterns that are suspicious but may be legitimate in development
    SUSPICIOUS_PATTERNS = [
        (r"\brm\s+-rf\b", "Recursive delete"),
        (r"\bdel\s+/f", "Force file delete"),
        (r"\bshutdown\b", "System shutdown"),
        (r"\breboot\b", "System reboot"),
        (r"\breg\s+delete\b", "Registry modification"),
        (r"\bnet\s+user\b", "User account modification"),
        (r"\bcurl\s+.*\|\s*sh", "Piped remote script execution"),
        (r"\bwget\s+.*\|\s*sh", "Piped remote script execution"),
        (r"\binvoke-webrequest\s+.*\|\s*iex", "Remote code execution"),
    ]

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

    def _classify_command(self, command: str) -> tuple[str, str | None]:
        """Classify command safety. Returns (level, reason)."""
        cmd_lower = command.lower()

        for pattern, reason in self.DANGEROUS_PATTERNS:
            if re.search(pattern, cmd_lower, re.IGNORECASE):
                return ("blocked", reason)

        for pattern, reason in self.SUSPICIOUS_PATTERNS:
            if re.search(pattern, cmd_lower, re.IGNORECASE):
                return ("suspicious", reason)

        return ("safe", None)

    async def execute(self, command: str, cwd: str | None = None, timeout: int = 60) -> str:
        work_dir = Path(cwd) if cwd else BASE_DIR
        if not work_dir.is_absolute():
            work_dir = BASE_DIR / work_dir
        if not is_path_safe(work_dir, BASE_DIR):
            return "[Error: Working directory is outside of allowed workspace]"

        command = self._rewrite_command(command)

        # Classify and potentially block
        level, reason = self._classify_command(command)
        if level == "blocked":
            return f"[Blocked: {reason}. This command is not allowed.]"
        if level == "suspicious":
            # In a full implementation, this would pause and ask the user.
            # For now, we allow but annotate the output.
            pass

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

            result = output
            if proc.returncode != 0:
                result = f"[Exit {proc.returncode}] {output}\n{err}"
            elif err:
                result = f"{output}\n{err}" if output else err

            if level == "suspicious" and reason:
                result = f"[Warning: {reason}]\n{result}"

            return result or "[No output]"
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return f"[Error: Command timed out after {timeout}s]"
        except Exception as e:
            return f"[Error: {e}]"
