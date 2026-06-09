"""Regression: MCP command security audit must not reject plain binaries.

2026-06-08 bug: ``_SHELL_METACHAR_RE`` had a trailing ``|`` (empty-string
alternation) so it matched EVERY string → every MCP server's command
(uvx / npx / node …) was flagged "command_injection_risk" and failed to start
("1 个 MCP server 启动失败"). Fix removes the stray ``|``.
"""
from __future__ import annotations

import pytest

from xmclaw.providers.tool.mcp_hub import McpServerConfig, _audit_server_config


@pytest.mark.parametrize("command", ["uvx", "npx", "node", "python", "uv", "deno"])
def test_plain_binaries_pass_audit(command):
    cfg = McpServerConfig(name="x", command=command, args=["--from", "/some/path"])
    safe, msgs = _audit_server_config(cfg)
    assert safe is True, f"{command!r} wrongly flagged: {msgs}"
    assert not any("command_injection" in m for m in msgs)


@pytest.mark.parametrize("command", ["a;b", "x$(y)", "a|b", "foo`bar`", 'a"b', "a&c"])
def test_injection_commands_still_blocked(command):
    cfg = McpServerConfig(name="x", command=command)
    safe, msgs = _audit_server_config(cfg)
    assert safe is False
    assert any("command_injection" in m for m in msgs)


def test_windows_path_arg_does_not_block():
    # 用户真实场景:uvx --from C:\Users\...\bilibili-mcp-server
    cfg = McpServerConfig(
        name="bilibili-mcp-server",
        command="uvx",
        args=["--from", r"C:\Users\15978\.xmclaw\skills_user\bilibili-mcp-server"],
    )
    safe, _ = _audit_server_config(cfg)
    assert safe is True
