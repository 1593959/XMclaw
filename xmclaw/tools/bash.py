"""Shell command execution tool with permission classification."""
import asyncio
import re
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import (
    is_path_safe, get_permission_manager, PermissionLevel, ToolCategory,
    AuditEntry, TOOL_CATEGORIES,
)
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

    # Patterns that are always blocked regardless of permission level
    BLOCKED_PATTERNS = [
        (r"\brm\s+-rf\s+/", "Destructive filesystem operation (rm -rf /)"),
        (r"\bdd\s+if=.*of=/dev/", "Direct disk write operation"),
        (r"\bmv\s+.*\s+/", "Moving files to root directory"),
        (r"\bformat\b", "Disk format operation"),
        (r"\bdel\s+/[fFq]", "Force delete system files"),
        (r"\brd\s+/s\s+/q", "Force directory removal (Windows)"),
        (r"\binit\b", "System init/process reinit"),
    ]

    # Patterns that are suspicious and trigger confirmation/warning
    SUSPICIOUS_PATTERNS = [
        (r"\brm\s+-rf\b", "Recursive delete"),
        (r"\bdel\s+/f", "Force file delete"),
        (r"\bshutdown\b", "System shutdown"),
        (r"\breboot\b", "System reboot"),
        (r"\breg\s+delete\b", "Registry modification"),
        (r"\bnet\s+user\b", "User account modification"),
        (r"\bcurl\s+.*\|\s*sh", "Piped remote script execution (curl | sh)"),
        (r"\bwget\s+.*\|\s*sh", "Piped remote script execution (wget | sh)"),
        (r"\binvoke-webrequest\s+.*\|\s*iex", "PowerShell remote code execution"),
        (r"\bsudo\s+rm\s+-rf\b", "Root recursive delete"),
        (r">\s*/dev/sd[a-z]", "Direct write to block device"),
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

        for pattern, reason in self.BLOCKED_PATTERNS:
            if re.search(pattern, cmd_lower, re.IGNORECASE):
                return ("blocked", reason)

        for pattern, reason in self.SUSPICIOUS_PATTERNS:
            if re.search(pattern, cmd_lower, re.IGNORECASE):
                return ("suspicious", reason)

        return ("safe", None)

    def _log_security_event(self, event: str, reason: str) -> None:
        """Log security event through the PermissionManager audit system."""
        try:
            pm = get_permission_manager()
            asyncio.create_task(pm._audit_log_entry(AuditEntry(
                timestamp=__import__("datetime").datetime.now().isoformat(),
                event=event,
                tool=self.name,
                user="default",
                detail=reason,
                tool_category=ToolCategory.MODERATE.value,
                risk_score=60 if event == "BLOCKED" else 30,
            )))
        except Exception:
            pass  # Don't let audit failures break tool execution

    async def execute(self, command: str, cwd: str | None = None, timeout: int = 60) -> str:
        work_dir = Path(cwd) if cwd else BASE_DIR
        if not work_dir.is_absolute():
            work_dir = BASE_DIR / work_dir
        if not is_path_safe(work_dir, BASE_DIR):
            self._log_security_event("BLOCKED", f"Path outside workspace: {work_dir}")
            return "[Blocked: Working directory is outside of allowed workspace]"

        # Rewrite command (use bundled Python if available)
        command = self._rewrite_command(command)

        # Pattern-based classification (always runs, independent of permission level)
        level, reason = self._classify_command(command)
        if level == "blocked":
            self._log_security_event("BLOCKED", f"Blocked pattern: {reason}")
            return f"[Blocked: {reason}. This command is not allowed.]"

        if level == "suspicious":
            # Log warning event
            self._log_security_event("WARNING", f"Suspicious pattern detected: {reason}")
            # Check if PermissionManager wants to block or ask
            pm = get_permission_manager()
            decision = pm.check_tool(self.name, context={"path": str(work_dir), "command": command})
            if not decision.allowed:
                if decision.requires_confirmation:
                    confirmed = await pm.request_confirmation(self.name, reason)
                    if not confirmed:
                        return f"[Blocked: {reason}. User denied confirmation.]"
                else:
                    return f"[Blocked: {reason}]"

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
