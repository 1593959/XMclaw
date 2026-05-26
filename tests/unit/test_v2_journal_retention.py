"""Tests for ``JournalRetentionTask``.

Locks the behavior: journal/<YYYY-MM>/*.jsonl files older than the
configured cutoff get pruned. The audit (2026-05-26) caught one
user's install with 413 files in 2026-05/ after three weeks —
there was no rotation at all before this task was added.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from xmclaw.daemon.journal_retention import JournalRetentionTask


def _touch(path: Path, *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_prune_drops_old_month_directories(tmp_path: Path) -> None:
    now = time.time()
    old = now - 90 * 86400  # 90 days ago
    new = now - 5 * 86400   # 5 days ago

    old_dir = tmp_path / "2026-02"
    new_dir = tmp_path / "2026-05"
    _touch(old_dir / "sess-a.jsonl", mtime=old)
    _touch(old_dir / "sess-b.jsonl", mtime=old)
    _touch(new_dir / "sess-c.jsonl", mtime=new)
    # Force the month dir mtimes — touching files updates the
    # parent dir mtime on some filesystems.
    os.utime(old_dir, (old, old))
    os.utime(new_dir, (new, new))

    files, dirs = JournalRetentionTask._prune(tmp_path, 30 * 86400.0)
    assert files == 2
    assert dirs == 1
    assert not old_dir.exists()
    assert new_dir.exists()
    assert (new_dir / "sess-c.jsonl").exists()


def test_prune_drops_individual_old_files_in_recent_month(tmp_path: Path) -> None:
    """A month directory's mtime can be recent (last write was 5d ago)
    while it still contains a session file from 90d ago. The boundary
    case: don't delete the whole month, just the stale files."""
    now = time.time()
    old_file_mtime = now - 90 * 86400
    new_file_mtime = now - 5 * 86400

    month = tmp_path / "2026-05"
    _touch(month / "old-sess.jsonl", mtime=old_file_mtime)
    _touch(month / "new-sess.jsonl", mtime=new_file_mtime)
    os.utime(month, (new_file_mtime, new_file_mtime))  # month dir is "recent"

    files, dirs = JournalRetentionTask._prune(tmp_path, 30 * 86400.0)
    assert files == 1
    assert dirs == 0
    assert not (month / "old-sess.jsonl").exists()
    assert (month / "new-sess.jsonl").exists()


def test_prune_noop_when_everything_is_recent(tmp_path: Path) -> None:
    now = time.time()
    month = tmp_path / "2026-05"
    _touch(month / "sess-a.jsonl", mtime=now - 3 * 86400)
    _touch(month / "sess-b.jsonl", mtime=now - 7 * 86400)
    os.utime(month, (now - 3 * 86400, now - 3 * 86400))

    files, dirs = JournalRetentionTask._prune(tmp_path, 30 * 86400.0)
    assert files == 0
    assert dirs == 0


def test_prune_missing_root_is_noop(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist"
    # _prune walks iterdir which would raise — but the public _tick
    # entry guards with is_dir() upstream. _prune itself raises if
    # called directly on a missing dir; that's by design — only the
    # task wrapper handles the missing-root case.
    import pytest
    with pytest.raises((FileNotFoundError, OSError)):
        JournalRetentionTask._prune(nonexistent, 30 * 86400.0)


def test_disabled_task_does_not_start(tmp_path: Path) -> None:
    task = JournalRetentionTask(
        tmp_path, max_age_days=30.0, enabled=False,
    )
    assert task._enabled is False


def test_zero_max_age_disables(tmp_path: Path) -> None:
    """max_age_days=0 must disable the task even when enabled=true.
    Matches events_retention semantics ("0 = keep forever")."""
    task = JournalRetentionTask(
        tmp_path, max_age_days=0.0, enabled=True,
    )
    assert task._enabled is False
