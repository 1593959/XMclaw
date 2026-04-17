"""Docker-based sandbox for secure code execution."""
from __future__ import annotations
import asyncio
import subprocess
import tempfile
from pathlib import Path
from xmclaw.utils.log import logger

DEFAULT_IMAGE = "python:3.12-slim"


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


class DockerRunner:
    """Execute code inside a Docker container with strict resource limits."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        timeout: int = 30,
        memory_limit: str = "256m",
        cpu_quota: int = 50000,
        network: str = "none",
        max_output: int = 65536,
    ):
        self.image = image
        self.timeout = timeout
        self.memory_limit = memory_limit
        self.cpu_quota = cpu_quota
        self.network = network
        self.max_output = max_output
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = _docker_available()
        return self._available

    async def run_python(self, code: str, stdin: str = "") -> dict:
        if not self.available:
            return {"stdout": "", "stderr": "Docker not available", "exit_code": -1, "timed_out": False}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "solution.py").write_text(code, encoding="utf-8")
            cmd = [
                "docker", "run", "--rm",
                "--network", self.network,
                f"--memory={self.memory_limit}",
                f"--memory-swap={self.memory_limit}",
                f"--cpu-quota={self.cpu_quota}",
                "--pids-limit=64",
                "--read-only",
                "--tmpfs", "/tmp:size=64m,noexec",
                "-v", f"{tmp}:/workspace:ro",
                "--workdir", "/workspace",
                self.image,
                "python", "/workspace/solution.py",
            ]
            return await self._run_cmd(cmd, stdin)

    async def run_shell(self, command: str, stdin: str = "") -> dict:
        if not self.available:
            return {"stdout": "", "stderr": "Docker not available", "exit_code": -1, "timed_out": False}
        cmd = [
            "docker", "run", "--rm",
            "--network", self.network,
            f"--memory={self.memory_limit}",
            f"--memory-swap={self.memory_limit}",
            f"--cpu-quota={self.cpu_quota}",
            "--pids-limit=64",
            self.image,
            "sh", "-c", command,
        ]
        return await self._run_cmd(cmd, stdin)

    async def run_code(self, language: str, code: str, stdin: str = "") -> dict:
        if language in ("python", "python3", "py"):
            return await self.run_python(code, stdin)
        if language in ("bash", "sh", "shell"):
            return await self.run_shell(code, stdin)
        return {
            "stdout": "",
            "stderr": f"Unsupported language: {language}",
            "exit_code": -1,
            "timed_out": False,
        }

    async def _run_cmd(self, cmd: list[str], stdin: str) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_bytes = stdin.encode() if stdin else None
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=self.timeout + 5,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return {"stdout": "", "stderr": f"Docker timeout after {self.timeout}s", "exit_code": -1, "timed_out": True}
            return {
                "stdout": stdout.decode("utf-8", errors="replace")[: self.max_output],
                "stderr": stderr.decode("utf-8", errors="replace")[: self.max_output],
                "exit_code": proc.returncode or 0,
                "timed_out": False,
            }
        except Exception as e:
            logger.error("docker_runner_error", error=str(e))
            return {"stdout": "", "stderr": str(e), "exit_code": -1, "timed_out": False}
