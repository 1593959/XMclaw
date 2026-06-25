from __future__ import annotations

import subprocess

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.providers.tool.error_templates import (
    path_not_found_error_template,
    permission_denied_error_template,
    shell_check_error_template,
    tool_sandbox_error_template,
    tool_timeout_error_template,
)


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


def test_named_tool_error_templates_render_stable_kinds() -> None:
    assert shell_check_error_template("command exited non-zero (2)").startswith(
        "[shell_check_error]"
    )
    assert "[tool_timeout]" in tool_timeout_error_template("bash", 1.5)
    assert "[tool_sandbox_error]" in tool_sandbox_error_template("docker missing")
    assert "[path_not_found]" in path_not_found_error_template("/missing")
    assert "[permission_denied]" in permission_denied_error_template("/locked")


@pytest.mark.asyncio
async def test_bash_non_zero_uses_shell_error_template() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(
        _call("bash", {"command": "python -c \"import sys; print('bad'); sys.exit(7)\""}),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("[shell_check_error]")
    assert "command exited non-zero" in result.error
    assert "[cmd:" in result.error
    assert "bad" in result.error


@pytest.mark.asyncio
async def test_bash_docker_policy_without_runner_uses_sandbox_template() -> None:
    tools = BuiltinTools(shell_execution_policy="docker")
    result = await tools.invoke(_call("bash", {"command": "echo should-not-run"}))

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("[tool_sandbox_error]")
    assert "sandbox" in result.error


@pytest.mark.asyncio
async def test_bash_docker_unavailable_uses_sandbox_template() -> None:
    def runner(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    tools = BuiltinTools(
        shell_execution_policy="docker",
        shell_sandbox_runner=runner,
    )
    result = await tools.invoke(_call("bash", {"command": "echo hi"}))

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("[tool_sandbox_error]")
    assert "docker" in result.error


@pytest.mark.asyncio
async def test_bash_docker_timeout_uses_timeout_template() -> None:
    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=0.01)

    tools = BuiltinTools(
        shell_execution_policy="docker",
        shell_sandbox_runner=runner,
    )
    result = await tools.invoke(
        _call("bash", {"command": "sleep 5", "timeout_seconds": 0.01}),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("[tool_timeout]")
