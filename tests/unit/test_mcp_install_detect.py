"""MCP server 检测 + install_from_source 路由（2026-06-07）。

用户反馈：装 MCP server 仓库被当成"不是 skill"拒掉。现在安装器应识别 MCP
server、返回可热加载的 mcp_config，而不是报错。
"""
from __future__ import annotations

import json

import pytest

from xmclaw.skills.marketplace import (
    InstallValidationError,
    detect_mcp_server,
    install_from_source,
)


def _write(p, name, text):
    (p / name).write_text(text, encoding="utf-8")


# ── detect_mcp_server ──
def test_detect_node_mcp_by_dependency(tmp_path):
    _write(tmp_path, "package.json", json.dumps({
        "name": "bilibili-mcp-server",
        "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"},
    }))
    plan = detect_mcp_server(tmp_path)
    assert plan and plan["runtime"] == "node"
    assert plan["command"] == "npx" and str(tmp_path) in plan["args"]


def test_detect_node_mcp_by_name(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"name": "some-mcp-thing"}))
    # 目录名也带 mcp
    plan = detect_mcp_server(tmp_path)
    assert plan and plan["runtime"] == "node"


def test_detect_python_mcp_with_script(tmp_path):
    _write(tmp_path, "pyproject.toml",
           '[project]\nname="x-mcp"\ndependencies=["mcp>=1.0"]\n'
           '[project.scripts]\nx-mcp-server = "x.server:main"\n')
    plan = detect_mcp_server(tmp_path)
    assert plan and plan["runtime"] == "python"
    assert plan["command"] == "uvx" and "x-mcp-server" in plan["args"]


def test_detect_returns_none_for_plain_repo(tmp_path):
    _write(tmp_path, "package.json", json.dumps({
        "name": "just-a-web-app",
        "dependencies": {"react": "^18"},
    }))
    assert detect_mcp_server(tmp_path) is None


def test_detect_none_for_empty_dir(tmp_path):
    assert detect_mcp_server(tmp_path) is None


# ── install_from_source 路由 ──
def _fake_git(target_factory):
    """造一个假 git runner：把指定内容写进 clone 目标目录。"""
    def _runner(cmd, **kw):
        # cmd = ["git","clone","--depth=1","--quiet", url, target]
        target_factory(cmd[-1])

        class _R:
            returncode = 0
            stderr = ""
        return _R()
    return _runner


def test_install_mcp_server_returns_plan_not_error(tmp_path):
    def _populate(target):
        import os
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "bilibili-mcp-server",
                       "dependencies": {"@modelcontextprotocol/sdk": "^1"}}, f)

    res = install_from_source(
        "github:huccihuang/bilibili-mcp-server",
        git_runner=_fake_git(_populate),
        install_root=tmp_path / "skills",
    )
    assert res.kind == "mcp"
    assert res.mcp_config and res.mcp_config["command"] == "npx"
    assert res.mcp_config["disabled"] is False


def test_install_plain_repo_gives_actionable_error(tmp_path):
    def _populate(target):
        import os
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "README.md"), "w", encoding="utf-8") as f:
            f.write("# just a website\n")

    with pytest.raises(InstallValidationError) as ei:
        install_from_source(
            "github:foo/plain-web",
            git_runner=_fake_git(_populate),
            install_root=tmp_path / "skills",
        )
    msg = str(ei.value)
    assert "MCP" in msg and "skill" in msg  # 报错可操作
