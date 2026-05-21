"""Git status snapshot — lightweight branch/status/commits summary.

Codex CLI parity: ``getGitStatus()`` injects branch, dirty-state, and
recent commit context so the agent knows what codebase it's working on
without having to call ``bash git status`` every turn.

Safe to call from any coroutine: each invocation spawns a short-lived
subprocess.  Returns ``None`` when the path is not inside a git repo or
when git is unavailable.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitStatus:
    """Structured git snapshot."""

    branch: str
    commit: str  # short hash
    is_dirty: bool
    modified_count: int
    untracked_count: int
    ahead_behind: str | None  # e.g. "+2-1" or None
    recent_commits: list[str]  # first line of last N commits

    def render(self, *, max_commits: int = 3) -> str:
        """Return a compact markdown block for prompt injection."""
        lines: list[str] = ["## Git 状态"]
        dirty = "（有未保存更改）" if self.is_dirty else "（干净）"
        ab = f" [{self.ahead_behind}]" if self.ahead_behind else ""
        lines.append(f"- 分支: `{self.branch}`{ab} @ `{self.commit}` {dirty}")
        if self.modified_count:
            lines.append(f"- 修改: {self.modified_count} 个文件")
        if self.untracked_count:
            lines.append(f"- 未跟踪: {self.untracked_count} 个文件")
        if self.recent_commits:
            lines.append("- 最近提交:")
            for c in self.recent_commits[:max_commits]:
                lines.append(f"  - {c}")
        return "\n".join(lines)


def _git(*args: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3.0,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def get_git_status(cwd: Path | str, *, commit_limit: int = 3) -> GitStatus | None:
    """Capture a lightweight git snapshot for *cwd*.

    Returns ``None`` when:
      * the directory is not inside a git repo,
      * the ``git`` executable is missing,
      * any git command times out or fails.
    """
    p = Path(cwd)
    # Verify we're in a repo.
    if _git("rev-parse", "--git-dir", cwd=p) is None:
        return None

    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=p) or "unknown"
    commit = _git("rev-parse", "--short", "HEAD", cwd=p) or "???????"

    # Porcelain status: one line per path.
    status_out = _git("status", "--porcelain", cwd=p) or ""
    modified = 0
    untracked = 0
    for line in status_out.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1

    # Ahead/behind upstream (best-effort; no upstream → None).
    ahead_behind: str | None = None
    upstream = _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=p)
    if upstream and upstream != "@{upstream}":
        ab_out = _git(
            "rev-list", "--left-right", "--count",
            f"HEAD...{upstream}", cwd=p,
        )
        if ab_out:
            parts = ab_out.split()
            if len(parts) == 2:
                try:
                    a, b = int(parts[0]), int(parts[1])
                    if a or b:
                        ahead_behind = f"+{a}-{b}"
                except ValueError:
                    pass

    # Recent commits.
    recent: list[str] = []
    log_out = _git(
        "log", "--oneline", "-n", str(commit_limit),
        "--no-decorate", cwd=p,
    )
    if log_out:
        recent = [ln.strip() for ln in log_out.splitlines() if ln.strip()]

    return GitStatus(
        branch=branch,
        commit=commit,
        is_dirty=(modified + untracked) > 0,
        modified_count=modified,
        untracked_count=untracked,
        ahead_behind=ahead_behind,
        recent_commits=recent,
    )
