"""Local environment executor."""
import asyncio
from pathlib import Path
from xmclaw.tools.environments.base import Environment


class LocalEnvironment(Environment):
    async def execute(self, command: str, cwd: str | None = None, timeout: int = 60) -> dict:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Timeout after {timeout}s",
            }
