"""Safe code execution tool — routes through SandboxManager."""
from __future__ import annotations
from xmclaw.tools.base import Tool
from xmclaw.sandbox.manager import get_sandbox


class CodeExecTool(Tool):
    """Execute code safely in a sandboxed environment.

    Uses Docker when available (network-isolated, read-only, memory-limited).
    Falls back to a subprocess runner with a hard timeout when Docker is absent.
    """

    name = "code_exec"
    description = (
        "Execute code safely in a sandboxed environment. "
        "Supports Python, Bash, and other languages (non-Python requires Docker). "
        "Returns stdout, stderr, exit code, and any safety warnings."
    )
    parameters = {
        "code": {
            "type": "string",
            "description": "Source code to execute.",
        },
        "language": {
            "type": "string",
            "description": "Programming language: 'python' (default), 'bash', 'javascript', etc.",
        },
        "stdin": {
            "type": "string",
            "description": "Optional stdin input to pass to the program.",
        },
        "timeout": {
            "type": "integer",
            "description": "Execution timeout in seconds (max 120). Default: 30.",
        },
    }

    async def execute(
        self,
        code: str,
        language: str = "python",
        stdin: str = "",
        timeout: int = 30,
    ) -> str:
        if not code.strip():
            return "[CodeExec Error: Empty code]"

        timeout = max(1, min(120, timeout))
        sandbox = get_sandbox()
        sandbox._process.timeout = timeout
        if sandbox._docker:
            sandbox._docker.timeout = timeout

        result = await sandbox.run_code(language, code, stdin)

        lines = []
        if result.get("warnings"):
            lines.append("⚠️  Safety warnings: " + "; ".join(result["warnings"]))
        lines.append(f"[Backend: {result.get('backend', '?')} | Exit: {result.get('exit_code', '?')}]")
        if result.get("timed_out"):
            lines.append(f"[Timed out after {timeout}s]")
        if result.get("stdout"):
            lines.append("--- stdout ---")
            lines.append(result["stdout"].rstrip())
        if result.get("stderr"):
            lines.append("--- stderr ---")
            lines.append(result["stderr"].rstrip())
        if not result.get("stdout") and not result.get("stderr"):
            lines.append("[No output]")

        return "\n".join(lines)
