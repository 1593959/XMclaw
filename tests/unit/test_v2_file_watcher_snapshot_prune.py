"""FileWatcher._take_snapshot — pin the directory-level pruning + the
non-blocking poll contract.

Real bug surfaced 2026-06-05 (diagnosed from a hung daemon): the old
``_take_snapshot`` used ``Path.rglob('*')`` to enumerate the ENTIRE
watched tree, then dropped ignored paths one-by-one via
``_should_ignore``. With the default watch path == the repo root, that
meant walking ~87k files (``.venv`` alone was ~80k) every 5 seconds,
SYNCHRONOUSLY, on the daemon's main asyncio event loop. Each snapshot
took >5s → the loop was starved → ``/health`` and the chat WebSocket
handshake both timed out → the CLI/TUI showed "未连接 / connection
failed" even though the daemon process was alive and bound to 8766.

py-spy on the live hang caught the smoking gun::

    normcase (ntpath.py:68)
    fnmatch (fnmatch.py:41)
    <genexpr> (xmclaw/cognition/file_watcher.py:76)   # _should_ignore
    _take_snapshot (file_watcher.py:166)              # rglob loop
    _poll_loop (file_watcher.py:110)
    run_forever ...

Two fixes pinned here:
  1. ``os.walk`` with in-place ``dirnames[:]`` pruning so we never even
     descend into ``.venv`` / ``.git`` / ``node_modules`` etc.
  2. ``_poll_loop`` offloads the (still-synchronous) snapshot via
     ``asyncio.to_thread`` so the event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from xmclaw.cognition.file_watcher import FileWatcher


def _build_tree(root: Path) -> None:
    """Lay down a tiny repo-like tree with both real files and the kind
    of giant noise dirs that caused the hang."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "README.md").write_text("# proj", encoding="utf-8")

    # Noise dirs that MUST be pruned (and would dominate a real repo).
    for noise in (".git", ".venv", "node_modules", "__pycache__"):
        d = root / noise
        d.mkdir()
        # Several files deep inside, plus a nested subdir, to prove we
        # never descend.
        (d / "a.txt").write_text("x", encoding="utf-8")
        sub = d / "deep" / "deeper"
        sub.mkdir(parents=True)
        (sub / "b.txt").write_text("y", encoding="utf-8")


def test_snapshot_prunes_noise_dirs(tmp_path: Path) -> None:
    """The snapshot must contain ONLY the real files, never anything
    under ``.venv`` / ``.git`` / ``node_modules`` / ``__pycache__``."""
    _build_tree(tmp_path)
    w = FileWatcher(watch_paths=[str(tmp_path)])
    snap = w._take_snapshot()

    keys = set(snap)
    # Real files present.
    assert any(k.endswith("main.py") for k in keys), snap
    assert any(k.endswith("README.md") for k in keys), snap
    # No noise dir leaked — not a single file from any pruned tree.
    for noise in (".git", ".venv", "node_modules", "__pycache__"):
        leaked = [k for k in keys if noise in Path(k).parts]
        assert not leaked, f"{noise} leaked into snapshot: {leaked[:3]}"


def test_snapshot_does_not_descend_into_pruned_dirs(tmp_path: Path) -> None:
    """Stronger than the above: even DEEPLY nested files under a pruned
    dir must be absent — proves directory-level pruning, not per-file
    filtering after a full walk."""
    _build_tree(tmp_path)
    w = FileWatcher(watch_paths=[str(tmp_path)])
    snap = w._take_snapshot()
    assert not any("deeper" in Path(k).parts for k in snap), (
        "a file 2 levels deep inside a pruned dir leaked — pruning "
        "is not happening at the directory boundary"
    )


def test_snapshot_values_are_stat_triples(tmp_path: Path) -> None:
    """``_diff_snapshots`` depends on (mtime, size, inode) — pin the
    value shape so the os.walk rewrite didn't change the contract."""
    _build_tree(tmp_path)
    w = FileWatcher(watch_paths=[str(tmp_path)])
    snap = w._take_snapshot()
    assert snap, "snapshot unexpectedly empty"
    for v in snap.values():
        assert isinstance(v, tuple) and len(v) == 3, v
        mtime, size, _ino = v
        assert isinstance(mtime, float)
        assert isinstance(size, int)


def test_poll_loop_does_not_block_event_loop(tmp_path: Path) -> None:
    """REGRESSION: the snapshot must run off the event loop. We make
    ``_take_snapshot`` deliberately slow, start the watcher, and assert
    the event loop stays responsive (a concurrent 50ms sleeper finishes
    on time instead of being starved for the snapshot's duration)."""
    _build_tree(tmp_path)
    w = FileWatcher(watch_paths=[str(tmp_path)])

    SNAPSHOT_BLOCK_S = 0.6

    def _slow_snapshot() -> dict:
        time.sleep(SNAPSHOT_BLOCK_S)  # simulate a big-tree walk
        return {}

    w._take_snapshot = _slow_snapshot  # type: ignore[method-assign]

    async def _scenario() -> float:
        await w.start()  # kicks off _poll_loop → initial snapshot
        # If the snapshot ran inline on the loop, this sleeper would be
        # delayed by ~SNAPSHOT_BLOCK_S. With to_thread it finishes ~on time.
        t0 = asyncio.get_running_loop().time()
        await asyncio.sleep(0.05)
        elapsed = asyncio.get_running_loop().time() - t0
        await w.stop()
        return elapsed

    elapsed = asyncio.run(_scenario())
    assert elapsed < SNAPSHOT_BLOCK_S / 2, (
        f"event loop was blocked for {elapsed:.3f}s — snapshot is "
        "running inline instead of via asyncio.to_thread"
    )
