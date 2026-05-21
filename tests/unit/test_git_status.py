"""Unit tests for xmclaw.core.workspace.git_status."""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.workspace.git_status import GitStatus, get_git_status


def test_get_git_status_returns_none_for_non_git_dir(tmp_path: Path):
    assert get_git_status(tmp_path) is None


def test_get_git_status_in_git_repo(tmp_path: Path):
    # Initialise a dummy repo.
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    (tmp_path / "a.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "a.txt"], cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )

    gs = get_git_status(tmp_path)
    assert gs is not None
    assert gs.branch == "main" or gs.branch == "master"
    assert len(gs.commit) == 7
    assert gs.is_dirty is False
    assert gs.modified_count == 0
    assert gs.untracked_count == 0
    assert len(gs.recent_commits) == 1
    assert "init" in gs.recent_commits[0]


def test_get_git_status_detects_dirty(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    (tmp_path / "b.txt").write_text("dirty")
    subprocess.run(
        ["git", "add", "b.txt"], cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    (tmp_path / "b.txt").write_text("modified")

    gs = get_git_status(tmp_path)
    assert gs is not None
    assert gs.is_dirty is True
    assert gs.modified_count == 1


def test_git_status_render():
    gs = GitStatus(
        branch="feat/x",
        commit="a1b2c3d",
        is_dirty=True,
        modified_count=2,
        untracked_count=1,
        ahead_behind="+1-0",
        recent_commits=["a1b2c3d feat: add foo", "b2c3d4e fix: bar"],
    )
    out = gs.render()
    assert "feat/x" in out
    assert "a1b2c3d" in out
    assert "+1-0" in out
    assert "2 个文件" in out
    assert "1 个文件" in out
    assert "add foo" in out
