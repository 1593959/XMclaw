"""Docker-backed shell sandbox for the builtin bash tool."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class DockerShellUnavailable(RuntimeError):
    """Raised when Docker execution cannot be attempted."""


@dataclass(frozen=True, slots=True)
class DockerShellSandbox:
    image: str = "python:3.12-alpine"
    docker_bin: str = "docker"
    memory: str = "512m"
    cpus: str = "1.0"
    pids_limit: int = 256
    network: str = "none"
    runner: Runner | None = None

    def run(
        self,
        command: str,
        *,
        cwd: str | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        if not command.strip():
            raise ValueError("empty command")
        workdir = Path(cwd or ".").resolve()
        if not workdir.exists() or not workdir.is_dir():
            raise DockerShellUnavailable(
                f"docker sandbox cwd does not exist or is not a directory: {workdir}"
            )

        args = [
            self.docker_bin,
            "run",
            "--rm",
            "--network",
            self.network,
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{workdir}:/workspace",
            "-w",
            "/workspace",
            self.image,
            "/bin/sh",
            "-lc",
            command,
        ]
        runner = self.runner or subprocess.run
        try:
            proc = runner(
                args,
                shell=False,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise DockerShellUnavailable(
                f"docker executable not found: {self.docker_bin}"
            ) from exc
        merged = (proc.stdout or b"") + (proc.stderr or b"")
        return int(proc.returncode), merged


__all__ = ["DockerShellSandbox", "DockerShellUnavailable"]
