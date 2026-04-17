"""Process-based sandbox — subprocess with timeout (no Docker required)."""
from __future__ import annotations
import asyncio
import sys
import tempfile
from pathlib import Path
from xmclaw.utils.log import logger


class ProcessRunner:
    """Execute code in an isolated subprocess with a hard timeout."""

    def __init__(self, timeout: int = 30, max_output: int = 65536):
        self.timeout = timeout
        self.max_output = max_output

    async def run_python(self, code: str, stdin: str = "") -> dict:
        """Execute Python code. Returns {stdout, stderr, exit_code, timed_out}."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp = Path(f.name)
        try:
            return await self._exec([sys.executable, str(tmp)], stdin)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    async def run_shell(self, command: str, stdin: str = "") -> dict:
        """Execute a shell command. Returns {stdout, stderr, exit_code, timed_out}."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return await self._wait(proc, stdin)
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "exit_code": -1, "timed_out": False}

    async def _exec(self, cmd: list[str], stdin: str = "") -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return await self._wait(proc, stdin)
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "exit_code": -1, "timed_out": False}

    async def _wait(self, proc: asyncio.subprocess.Process, stdin: str) -> dict:
        stdin_bytes = stdin.encode() if stdin else None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {
                "stdout": "",
                "stderr": f"[Timeout after {self.timeout}s]",
                "exit_code": -1,
                "timed_out": True,
            }
        return {
            "stdout": stdout.decode("utf-8", errors="replace")[: self.max_output],
            "stderr": stderr.decode("utf-8", errors="replace")[: self.max_output],
            "exit_code": proc.returncode or 0,
            "timed_out": False,
        }
