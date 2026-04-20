"""Tests for PR-E7-1 — git-level rollback substrate.

The git-ops module is a thin subprocess wrapper, so the tests focus on two
guarantees:

1. **Off-by-default.** When the daemon config does not opt into
   ``evolution.git_tracking`` the helpers must be inert (return None, touch
   nothing). This matters because the default install, the test suite, and
   every CI run must never create commits in the user's repo.
2. **Round-trip correctness.** When enabled, ``commit_artifact_change`` and
   ``revert_commit`` must produce real, inspectable commits in a scratch
   repo and the returned SHAs must match ``HEAD``.

A throwaway ``git init`` fixture is used so tests never touch the actual
xmclaw repo — a rogue test run that commits into the real worktree would
be very hard to clean up.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


def _run(cmd: list[str], cwd: Path) -> str:
    """Helper — run a git subprocess and return stdout (trimmed)."""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def scratch_repo(tmp_path):
    """A throwaway git repo with an initial commit on ``main``.

    The initial commit gives us a clean HEAD to diff against; without it
    ``git commit`` complains about the unborn branch on some git versions.
    Config is set per-repo so we don't care about the machine's global
    identity.
    """
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


def _force_enable():
    """Monkey-patch context: pretend daemon config opts into git tracking."""
    return patch(
        "xmclaw.evolution.git_ops.is_git_tracking_enabled",
        return_value=True,
    )


def test_is_git_tracking_enabled_default_off():
    """Default config (no evolution.git_tracking key) must return False."""
    from xmclaw.evolution import git_ops
    # Under test env there is no daemon config; the except branch returns
    # False. Either way the answer must be False, never None or True.
    assert git_ops.is_git_tracking_enabled() is False


def test_commit_artifact_change_noop_when_disabled(scratch_repo):
    """With git tracking off, the helper must not run any git subprocess."""
    from xmclaw.evolution.git_ops import commit_artifact_change
    (scratch_repo / "a.txt").write_text("hello")
    sha = commit_artifact_change(
        "my_skill", "skill", [scratch_repo / "a.txt"],
        action="promote", repo_dir=scratch_repo,
    )
    assert sha is None
    # And crucially — the file is not committed. Only the init commit exists.
    log = _run(["git", "log", "--oneline"], scratch_repo).splitlines()
    assert len(log) == 1


def test_commit_artifact_change_round_trip(scratch_repo):
    """Enabled mode: staging a new file yields a new [evo] commit on HEAD."""
    from xmclaw.evolution.git_ops import commit_artifact_change
    target = scratch_repo / "skill.py"
    target.write_text("def run(): ...\n")
    with _force_enable():
        sha = commit_artifact_change(
            "my_skill", "skill", [target],
            action="promote", repo_dir=scratch_repo,
        )
    assert sha is not None and len(sha) == 40
    head = _run(["git", "rev-parse", "HEAD"], scratch_repo)
    assert head == sha
    subject = _run(
        ["git", "log", "-1", "--pretty=%s"], scratch_repo
    )
    assert subject == "[evo] promote skill my_skill"
    # Author identity must be the xmclaw-evo persona, not the repo user.
    author = _run(["git", "log", "-1", "--pretty=%an <%ae>"], scratch_repo)
    assert author == "xmclaw-evo <evo@xmclaw.local>"


def test_commit_artifact_change_returns_none_on_noop(scratch_repo):
    """If the staged paths don't actually change anything, return None."""
    from xmclaw.evolution.git_ops import commit_artifact_change
    # README.md already matches what's in the index/HEAD.
    (scratch_repo / "README.md").write_text("init\n")
    with _force_enable():
        sha = commit_artifact_change(
            "rd", "skill", [scratch_repo / "README.md"],
            action="promote", repo_dir=scratch_repo,
        )
    assert sha is None


def test_revert_commit_creates_revert(scratch_repo):
    """Enabled mode: reverting a promote commit produces a new revert commit.

    The revert's message must carry the ``[evo] rollback <reason>`` prefix
    and reference the original sha so ``git log`` tells the full story.
    """
    from xmclaw.evolution.git_ops import commit_artifact_change, revert_commit
    target = scratch_repo / "skill.py"
    target.write_text("def run(): ...\n")
    with _force_enable():
        promote_sha = commit_artifact_change(
            "my_skill", "skill", [target],
            action="promote", repo_dir=scratch_repo,
        )
        assert promote_sha is not None
        revert_sha = revert_commit(
            promote_sha, reason="harmful_ratio", repo_dir=scratch_repo,
        )
    assert revert_sha is not None
    assert revert_sha != promote_sha
    head = _run(["git", "rev-parse", "HEAD"], scratch_repo)
    assert head == revert_sha
    body = _run(["git", "log", "-1", "--pretty=%B"], scratch_repo)
    assert "[evo] rollback harmful_ratio" in body
    assert promote_sha in body
    # The working tree should be back to the pre-promote state.
    assert not target.exists()


def test_revert_commit_noop_when_disabled(scratch_repo):
    """With git tracking off, revert_commit must not run git."""
    from xmclaw.evolution.git_ops import revert_commit
    # Any plausible-looking sha — we never reach the cat-file check because
    # is_git_tracking_enabled() short-circuits first.
    assert revert_commit("deadbeef" * 5, reason="x", repo_dir=scratch_repo) is None


def test_revert_commit_missing_sha_returns_none(scratch_repo):
    """Referencing a sha that isn't in the repo must fail soft, not raise."""
    from xmclaw.evolution.git_ops import revert_commit
    with _force_enable():
        result = revert_commit(
            "0" * 40, reason="x", repo_dir=scratch_repo,
        )
    assert result is None
