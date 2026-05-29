"""B-310: cross-worktree mutex for integration_real tests.

These tests hit a live daemon on 127.0.0.1:8766, send realistic WS
messages, and exercise multi-minute flows (flow-a-s2 ran 27 min in
one observed instance). When two Claude Code worktrees both run
``pytest tests/integration_real/`` they share the same daemon port
and step on each other — one observed crash had four worktrees
running simultaneously and exhausting the kimi quota in cascade.

Solution mirrors what Codex CLI's open issue #11435 calls for:
file-based mutex on a known location. Acquire on session start,
release on session end. Concurrent runners block on file lock
until the first finishes.

Lock path: ``~/.xmclaw/v2/integration_test.lock``
Behavior: blocking acquire (no timeout); first runner runs, others
wait. Set ``XMC_INTEGRATION_REAL_NOWAIT=1`` in env to skip + xfail
the entire suite if locked (CI matrix mode).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


_LOCK_PATH = Path.home() / ".xmclaw" / "v2" / "integration_test.lock"
_lock_handle = None  # held for the entire pytest session


def _try_acquire_lock(blocking: bool) -> tuple[bool, "object | None"]:
    """Cross-platform exclusive file lock. Returns (acquired, handle).

    Windows: msvcrt.locking()
    POSIX: fcntl.flock(LOCK_EX [| LOCK_NB])
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCK_PATH, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                # 1 byte at offset 0; non-blocking unless we loop.
                fh.seek(0)
                if blocking:
                    while True:
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                            break
                        except OSError:
                            time.sleep(0.5)
                else:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False, None
        else:
            import fcntl
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fh.fileno(), flags)
            except OSError:
                fh.close()
                return False, None
        # write our PID for inspection (purely informational)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} ts={time.time():.0f}\n")
        fh.flush()
        return True, fh
    except Exception:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        return False, None


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="session", autouse=True)
def _integration_real_mutex():
    """Acquires the cross-worktree mutex for the whole pytest session."""
    global _lock_handle
    no_wait = os.environ.get("XMC_INTEGRATION_REAL_NOWAIT") == "1"
    acquired, handle = _try_acquire_lock(blocking=not no_wait)
    if not acquired:
        pytest.skip(
            f"integration_real lock held by another runner "
            f"(check {_LOCK_PATH}); set XMC_INTEGRATION_REAL_NOWAIT=0 to wait."
        )
    _lock_handle = handle
    yield
    _release_lock(_lock_handle)
    _lock_handle = None
