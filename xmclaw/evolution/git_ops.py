"""Plan v2 E7 — git-level rollback substrate for evolution artifacts.

Each artifact promotion writes one discrete git commit; each rollback writes
a matching revert commit. Storing the SHAs on the lineage row gives the
audit trail a first-class handle back to the exact bytes that entered the
runtime, and lets operators inspect the history with plain ``git log``.

Design choices:
- **Opt-in.** The daemon config gates all write operations
  (``evolution.git_tracking``). Default OFF so tests, fresh installs, and
  read-only review environments never touch git.
- **Isolated authorship.** Commits are authored as ``xmclaw-evo`` with a
  dedicated email so they are easy to filter out of blame.
- **Fail soft.** Every subprocess is wrapped; a missing git binary, dirty
  worktree, or any other failure logs a warning and returns None. The
  evolution flow must never wedge because git is unavailable.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from xmclaw.utils.paths import BASE_DIR
from xmclaw.utils.log import logger


_AUTHOR_NAME = "xmclaw-evo"
_AUTHOR_EMAIL = "evo@xmclaw.local"
_COMMIT_PREFIX = "[evo]"


def is_git_tracking_enabled() -> bool:
    """Return True when the daemon config has opted into git commit tracking.

    Reads the config fresh on every call — cheap, and avoids stale state
    when the user toggles the flag mid-session via the settings UI.
    """
    try:
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.load()
        evo = cfg.evolution or {}
        return bool(evo.get("git_tracking", False))
    except Exception:
        return False


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a git subprocess with the xmclaw-evo identity baked in.

    Returns ``(returncode, stdout, stderr)``. Author env vars are set
    explicitly so the commit doesn't adopt the human operator's identity
    or trip a missing ``user.email`` on a fresh clone.
    """
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", _AUTHOR_NAME)
    env.setdefault("GIT_AUTHOR_EMAIL", _AUTHOR_EMAIL)
    env.setdefault("GIT_COMMITTER_NAME", _AUTHOR_NAME)
    env.setdefault("GIT_COMMITTER_EMAIL", _AUTHOR_EMAIL)
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd or BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("git_ops_subprocess_failed", args=args, error=str(e))
        return -1, "", str(e)


def _head_sha(cwd: Path | None = None) -> str | None:
    rc, out, _ = _git("rev-parse", "HEAD", cwd=cwd)
    return out if rc == 0 and out else None


def commit_artifact_change(
    artifact_id: str,
    kind: str,
    paths: list[Path | str],
    action: str = "promote",
    repo_dir: Path | None = None,
) -> str | None:
    """Stage ``paths`` and create one commit for a promote/rollback/retire.

    Returns the new commit SHA, or None if git tracking is disabled, the
    repo is unhealthy, or there was nothing to commit (e.g. the file was
    already staged elsewhere). Callers should treat None as "no audit
    trail available" rather than an error.
    """
    if not is_git_tracking_enabled():
        return None
    cwd = repo_dir or BASE_DIR
    # Sanity: refuse to commit outside a git repo.
    rc, _, _ = _git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0:
        return None
    # Stage the specific paths. If none exist on disk (e.g. a rollback that
    # already deleted the file), ``git add`` will still pick up deletions
    # because we pass -A over each explicit path.
    str_paths = [str(Path(p)) for p in paths]
    if not str_paths:
        return None
    add_rc, _, add_err = _git("add", "-A", "--", *str_paths, cwd=cwd)
    if add_rc != 0:
        logger.warning("git_ops_add_failed", paths=str_paths, error=add_err)
        return None
    # If nothing actually changed (add was a no-op) diff --cached is empty.
    diff_rc, _, _ = _git("diff", "--cached", "--quiet", cwd=cwd)
    if diff_rc == 0:
        return None  # nothing to commit
    msg = f"{_COMMIT_PREFIX} {action} {kind} {artifact_id}"
    c_rc, _, c_err = _git("commit", "-m", msg, "--", *str_paths, cwd=cwd)
    if c_rc != 0:
        logger.warning("git_ops_commit_failed", artifact_id=artifact_id,
                       action=action, error=c_err)
        return None
    sha = _head_sha(cwd=cwd)
    if sha:
        logger.info("git_ops_commit_ok", artifact_id=artifact_id,
                    action=action, sha=sha)
    return sha


def revert_commit(
    sha: str,
    reason: str = "auto_rollback",
    repo_dir: Path | None = None,
) -> str | None:
    """Create a ``git revert`` of ``sha`` and return the new revert SHA.

    Uses ``--no-edit`` so the commit message is the standard "Revert ..."
    subject appended with our reason tag. Returns None on any failure,
    including the common case of an already-reverted sha (git refuses with
    "nothing to commit"). The caller decides whether that is fatal.
    """
    if not is_git_tracking_enabled():
        return None
    if not sha:
        return None
    cwd = repo_dir or BASE_DIR
    rc, _, _ = _git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0:
        return None
    # Make sure the sha is actually in this repo before attempting revert.
    rc, _, _ = _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=cwd)
    if rc != 0:
        logger.warning("git_ops_revert_skipped_missing_sha", sha=sha)
        return None
    r_rc, _, r_err = _git(
        "revert", "--no-edit", "-m", "1", sha, cwd=cwd,
    )
    # -m 1 is ignored for non-merge commits but makes the helper safe
    # for merge commits too — git only errors if we omit it on a merge.
    if r_rc != 0:
        # Retry without -m if that's the complaint (non-merge commits
        # reject -m on some git versions).
        if "mainline" in r_err or "is not a merge" in r_err:
            r_rc, _, r_err = _git("revert", "--no-edit", sha, cwd=cwd)
    if r_rc != 0:
        logger.warning("git_ops_revert_failed", sha=sha, error=r_err)
        return None
    revert_sha = _head_sha(cwd=cwd)
    if revert_sha:
        # Amend the revert message with the reason tag so a bare ``git log``
        # makes it obvious *why* the revert happened.
        new_msg = (
            f"{_COMMIT_PREFIX} rollback {reason}\n\nReverts {sha}"
        )
        _git("commit", "--amend", "-m", new_msg, cwd=cwd)
        revert_sha = _head_sha(cwd=cwd)
        logger.info("git_ops_revert_ok", original=sha,
                    revert_sha=revert_sha, reason=reason)
    return revert_sha
