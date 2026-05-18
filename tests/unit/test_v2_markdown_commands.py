"""Markdown commands — Wave-32+ (2026-05-18).

Covers:

  * frontmatter parsing (with + without)
  * discovery merge (project > claude > user)
  * render: shell substitution, $ARGUMENTS, untrusted-workspace gate
  * REST endpoint via TestClient
"""
from __future__ import annotations

import sys

import pytest

from xmclaw.cognition.markdown_commands import (
    CommandDef,
    discover_commands,
    find_command,
    parse_frontmatter,
    render_command,
)


# ── parse_frontmatter ───────────────────────────────────────────────────


def test_parse_frontmatter_basic() -> None:
    text = """---
description: Create a commit
allowed-tools: Bash(git add:*), Bash(git commit:*)
argument-hint: optional msg
---

## Body

hi"""
    fm, body = parse_frontmatter(text)
    assert fm["description"] == "Create a commit"
    assert fm["allowed-tools"] == "Bash(git add:*), Bash(git commit:*)"
    assert fm["argument-hint"] == "optional msg"
    assert body.startswith("## Body")


def test_parse_frontmatter_absent_returns_full_body() -> None:
    text = "# Just a heading\n\nno frontmatter here"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_ignores_comments_and_blank_lines() -> None:
    text = """---
# comment line ignored

description: actual value
---

body"""
    fm, _ = parse_frontmatter(text)
    assert fm == {"description": "actual value"}


# ── discovery ────────────────────────────────────────────────────────────


def test_discover_returns_empty_when_no_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # data_dir() lookup should also find nothing — point XMC_DATA_DIR
    # at an empty temp.
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    cmds = discover_commands()
    assert cmds == []


def test_discover_project_overrides_user(tmp_path, monkeypatch) -> None:
    """Project-local commands win over user-global ones with the
    same name. Same-named files in priority order: .xmclaw/ wins
    over .claude/ wins over user data_dir()."""
    monkeypatch.chdir(tmp_path)
    # User-global
    user_dir = tmp_path / "xdata" / "commands"
    user_dir.mkdir(parents=True)
    (user_dir / "commit.md").write_text(
        "---\ndescription: user-version\n---\nuser body",
        encoding="utf-8",
    )
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    # Project-local with same name
    proj_dir = tmp_path / ".xmclaw" / "commands"
    proj_dir.mkdir(parents=True)
    (proj_dir / "commit.md").write_text(
        "---\ndescription: project-version\n---\nproject body",
        encoding="utf-8",
    )
    by_name = {c.name: c for c in discover_commands()}
    assert by_name["commit"].description == "project-version"
    assert by_name["commit"].source == "project-md"
    assert "project body" in by_name["commit"].prompt_body


def test_discover_claude_dir_compat(tmp_path, monkeypatch) -> None:
    """Files in ``.claude/commands/`` are discovered too — gives
    zero-friction reuse of claude-code-src plugins."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    claude_dir = tmp_path / ".claude" / "commands"
    claude_dir.mkdir(parents=True)
    (claude_dir / "review.md").write_text(
        "---\ndescription: claude review\n---\nbody",
        encoding="utf-8",
    )
    cmds = {c.name: c for c in discover_commands()}
    assert "review" in cmds
    assert cmds["review"].source == "claude-md"


def test_find_command_lookup(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    proj = tmp_path / ".xmclaw" / "commands"
    proj.mkdir(parents=True)
    (proj / "ping.md").write_text("body", encoding="utf-8")
    assert find_command("ping") is not None
    assert find_command("nope") is None


# ── render: shell substitution ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_substitutes_shell_output(tmp_path) -> None:
    """`!`cmd`` is replaced with the command's stdout."""
    # Use `python -c` for cross-platform reliability instead of
    # `echo` which has different quoting on cmd vs bash.
    cmd = CommandDef(
        name="t", description="",
        prompt_body=f'Status: !`{sys.executable} -c "print(\'OK_TOKEN\')"`',
    )
    res = await render_command(cmd, cwd=tmp_path)
    assert res.ok
    assert "OK_TOKEN" in res.rendered
    assert "!`" not in res.rendered  # placeholder substituted


@pytest.mark.asyncio
async def test_render_substitutes_arguments(tmp_path) -> None:
    cmd = CommandDef(
        name="t", description="",
        prompt_body="User asked: $ARGUMENTS",
    )
    res = await render_command(cmd, "hello world", cwd=tmp_path)
    assert res.rendered == "User asked: hello world"


@pytest.mark.asyncio
async def test_render_empty_arguments_substitutes_empty(tmp_path) -> None:
    cmd = CommandDef(
        name="t", description="",
        prompt_body="Args: [$ARGUMENTS]",
    )
    res = await render_command(cmd, cwd=tmp_path)
    assert res.rendered == "Args: []"


@pytest.mark.asyncio
async def test_render_failed_shell_substitution_annotated(tmp_path) -> None:
    """A failing shell escape doesn't kill the render — substitute
    a ``<failed: ...>`` placeholder + report in ``failures``."""
    cmd = CommandDef(
        name="t", description="",
        prompt_body="Cmd: !`exit 1`",
    )
    res = await render_command(cmd, cwd=tmp_path)
    # The cmd ran (returncode != 0) so it's marked failed.
    assert res.ok is False
    assert "<failed:" in res.rendered or "exit 1" in res.rendered
    assert any("exit 1" in f for f in res.failures)


@pytest.mark.asyncio
async def test_render_untrusted_workspace_skips_shell(tmp_path) -> None:
    """Untrusted workspaces refuse to run shell escapes —
    substitute a placeholder instead. Prevents a hostile .md drop
    from running arbitrary commands."""
    cmd = CommandDef(
        name="t", description="",
        prompt_body='Will not run: !`rm -rf /tmp/hostile`',
    )
    res = await render_command(cmd, cwd=tmp_path, workspace_trust="untrusted")
    assert res.ok  # render itself succeeded
    # The hostile command name is preserved in the placeholder
    # (so the user sees WHAT was skipped), but it was NOT executed.
    assert "untrusted workspace" in res.rendered
    assert "not run" in res.rendered
    assert "rm -rf" in res.rendered  # the placeholder shows it


@pytest.mark.asyncio
async def test_render_multiple_escapes_concurrent(tmp_path) -> None:
    """All shell escapes run concurrently via asyncio.gather. Pin
    that both placeholders get filled in their original positions."""
    cmd = CommandDef(
        name="t", description="",
        prompt_body=(
            f'A=!`{sys.executable} -c "print(\'A_OUT\')"` '
            f'B=!`{sys.executable} -c "print(\'B_OUT\')"`'
        ),
    )
    res = await render_command(cmd, cwd=tmp_path)
    assert res.ok
    assert "A=A_OUT" in res.rendered
    assert "B=B_OUT" in res.rendered


# ── REST endpoint ───────────────────────────────────────────────────────


def test_commands_router_lists_discovered(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    proj = tmp_path / ".xmclaw" / "commands"
    proj.mkdir(parents=True)
    (proj / "hello.md").write_text(
        "---\ndescription: say hi\n---\nbody",
        encoding="utf-8",
    )
    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/commands")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["commands"]]
    assert "hello" in names
    # Prompt body NOT in list response (saves bytes).
    assert all("prompt_body" not in c for c in resp.json()["commands"])


def test_commands_router_get_returns_full_record(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    proj = tmp_path / ".xmclaw" / "commands"
    proj.mkdir(parents=True)
    (proj / "h2.md").write_text(
        "---\ndescription: d\n---\nfull body here",
        encoding="utf-8",
    )
    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/commands/h2")
    assert resp.status_code == 200
    assert "full body here" in resp.json()["prompt_body"]


def test_commands_router_get_404_when_missing(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/commands/nonesuch")
    assert resp.status_code == 404


def test_commands_router_render_executes(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    proj = tmp_path / ".xmclaw" / "commands"
    proj.mkdir(parents=True)
    (proj / "echo.md").write_text(
        "---\ndescription: d\n---\nArgs: $ARGUMENTS",
        encoding="utf-8",
    )
    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.post(
        "/api/v2/commands/echo/render",
        json={"arguments": "world"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["rendered"] == "Args: world"
