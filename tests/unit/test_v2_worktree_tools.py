"""B-94: enter_worktree / exit_worktree tools — agent-driven git
worktree isolation for risky changes.

Pins:
  * specs are advertised
  * enter rejects when not in a git repo
  * enter rejects when already in a worktree
  * exit refuses to run when current primary isn't a .claude/worktrees/ path
  * happy path round-trip on a real temp git repo: enter creates dir +
    branch, primary swaps in; exit removes + primary swaps back
  * keep=True path leaves the worktree on disk
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools, _WORKTREE_ORIGIN


_HAS_GIT = shutil.which("git") is not None


def _call(args: dict, name: str = "enter_worktree") -> ToolCall:
    return ToolCall(id="c1", provenance="synthetic", name=name, args=args)


@pytest.fixture(autouse=True)
def _isolate_workspace_state(tmp_path, monkeypatch) -> None:
    """Each test gets its own WorkspaceManager state.json so we don't
    pollute the dev machine's actual ~/.xmclaw/. ``XMC_DATA_DIR`` is
    the canonical lever — paths.py's ``data_dir()`` honours it, and
    WorkspaceManager builds its state path off of that."""
    isolated = tmp_path / "xmclaw_data"
    isolated.mkdir()
    monkeypatch.setenv("XMC_DATA_DIR", str(isolated))
    _WORKTREE_ORIGIN.clear()
    yield
    _WORKTREE_ORIGIN.clear()


def test_specs_advertised() -> None:
    names = {s.name for s in BuiltinTools().list_tools()}
    assert "enter_worktree" in names
    assert "exit_worktree" in names


@pytest.mark.asyncio
async def test_enter_rejects_when_not_in_git_repo(tmp_path, monkeypatch) -> None:
    from xmclaw.core.workspace import WorkspaceManager
    wm = WorkspaceManager()
    wm.add(tmp_path)  # not a git repo
    tools = BuiltinTools()
    result = await tools.invoke(_call({"name": "x"}))
    assert result.ok is False
    assert "not a git repository" in (result.error or "")


@pytest.mark.asyncio
async def test_exit_refuses_when_not_in_worktree(tmp_path) -> None:
    from xmclaw.core.workspace import WorkspaceManager
    wm = WorkspaceManager()
    wm.add(tmp_path)
    tools = BuiltinTools()
    result = await tools.invoke(_call({}, name="exit_worktree"))
    assert result.ok is False
    assert "worktree" in (result.error or "").lower()


@pytest.mark.skipif(not _HAS_GIT, reason="git binary not on PATH")
@pytest.mark.asyncio
async def test_round_trip_create_and_remove(tmp_path) -> None:
    """Real git worktree round-trip on a temp repo. Validates that
    enter creates the dir + branch + primary swap, and exit (default)
    removes everything cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Init repo + dummy commit (needed before worktree add).
    subprocess.run(
        ["git", "init"], cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True, check=True,
    )
    (repo / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, capture_output=True, check=True,
    )

    from xmclaw.core.workspace import WorkspaceManager
    wm = WorkspaceManager()
    wm.add(repo)
    tools = BuiltinTools()

    # Enter.
    enter = await tools.invoke(_call({"name": "feat-a"}))
    assert enter.ok is True, enter.error
    wt_path = Path(enter.content["worktree_path"])
    assert wt_path.exists()
    assert (wt_path / "README.md").exists()  # worktree carries the file
    # Tool reports the right structured fields.
    assert enter.content["branch"].startswith("wt/")
    assert enter.content["original_root"] == str(repo)
    assert str(wt_path) in enter.side_effects
    # Origin recorded for the exit path.
    assert str(wt_path.resolve()) in _WORKTREE_ORIGIN

    # Exit (default: remove).
    exit_res = await tools.invoke(_call({}, name="exit_worktree"))
    assert exit_res.ok is True, exit_res.error
    assert exit_res.content["removed"] is True
    assert exit_res.content["kept"] is False
    # Worktree gone from disk.
    assert not wt_path.exists()
    # Origin map cleaned up.
    assert str(wt_path.resolve()) not in _WORKTREE_ORIGIN


@pytest.mark.skipif(not _HAS_GIT, reason="git binary not on PATH")
@pytest.mark.asyncio
async def test_keep_true_preserves_directory(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
    (repo / "x").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, capture_output=True, check=True,
    )

    from xmclaw.core.workspace import WorkspaceManager
    wm = WorkspaceManager()
    wm.add(repo)
    tools = BuiltinTools()

    enter = await tools.invoke(_call({"name": "saved"}))
    wt_path = Path(enter.content["worktree_path"])
    exit_res = await tools.invoke(_call({"keep": True}, name="exit_worktree"))
    assert exit_res.ok is True
    assert exit_res.content["kept"] is True
    assert exit_res.content["removed"] is False
    # Directory survived.
    assert wt_path.exists()
