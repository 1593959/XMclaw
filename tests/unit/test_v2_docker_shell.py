from __future__ import annotations

import subprocess

import pytest

from xmclaw.providers.tool.docker_shell import (
    DockerShellSandbox,
    DockerShellUnavailable,
)


def test_docker_shell_sandbox_builds_hardened_command(tmp_path) -> None:
    calls: list[dict] = []

    def runner(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args, 0, b"ok\n", b"")

    sandbox = DockerShellSandbox(image="alpine:3.20", runner=runner)

    code, output = sandbox.run("echo ok", cwd=str(tmp_path), timeout=3.0)

    assert code == 0
    assert output == b"ok\n"
    args = calls[0]["args"]
    assert args[:3] == ["docker", "run", "--rm"]
    assert ["--network", "none"] == args[args.index("--network"):args.index("--network") + 2]
    assert ["--cap-drop", "ALL"] == args[args.index("--cap-drop"):args.index("--cap-drop") + 2]
    assert "--security-opt" in args and "no-new-privileges" in args
    assert f"{tmp_path.resolve()}:/workspace" in args
    assert args[-4:] == ["alpine:3.20", "/bin/sh", "-lc", "echo ok"]
    assert calls[0]["kwargs"]["shell"] is False
    assert calls[0]["kwargs"]["capture_output"] is True
    assert calls[0]["kwargs"]["timeout"] == 3.0


def test_docker_shell_sandbox_reports_missing_docker(tmp_path) -> None:
    def runner(*args, **kwargs):
        raise FileNotFoundError("docker")

    sandbox = DockerShellSandbox(runner=runner)

    with pytest.raises(DockerShellUnavailable) as exc:
        sandbox.run("echo ok", cwd=str(tmp_path), timeout=3.0)

    assert "docker executable not found" in str(exc.value)


def test_docker_shell_sandbox_rejects_missing_cwd(tmp_path) -> None:
    sandbox = DockerShellSandbox(runner=lambda *args, **kwargs: None)  # type: ignore[arg-type]

    with pytest.raises(DockerShellUnavailable) as exc:
        sandbox.run("echo ok", cwd=str(tmp_path / "missing"), timeout=3.0)

    assert "cwd does not exist" in str(exc.value)
