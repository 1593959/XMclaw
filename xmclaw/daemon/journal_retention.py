"""Journal retention task — periodic cleanup of old session jsonl files.

Background
==========

``JournalWriter`` writes one ``.jsonl`` file per session under
``~/.xmclaw/v2/journal/<YYYY-MM>/<session_id>.jsonl``. Each is small
(usually < 100 KB) but the file count grows linearly with sessions.
The audit (2026-05-26) caught a single user's install with 413
files in ``2026-05/`` after three weeks of normal use. There was
no retention task at all — the disk path would grow forever.

This module ships the rotation analog of ``events_retention.py``:

* monthly directories older than ``max_age_days`` get deleted
  wholesale (the YYYY-MM granularity makes the prune O(months),
  not O(files), even at scale);
* individual files older than ``max_age_days`` inside a kept month
  also get pruned (handles the boundary where a month is younger
  than the cutoff but some of its files aren't);
* runs as an asyncio sleep loop, default daily.

Default config (matches events_retention defaults so operators
don't have to reason about two separate dials):

    {
      "journal_retention": {
        "enabled": true,
        "max_age_days": 30,
        "interval_hours": 24
      }
    }

Set ``enabled=false`` to disable, ``max_age_days=0`` to keep
forever.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class JournalRetentionTask:
    """Periodic prune of old journal/<YYYY-MM>/*.jsonl files."""

    def __init__(
        self,
        journal_root: Path,
        *,
        max_age_days: float = 30.0,
        interval_hours: float = 24.0,
        enabled: bool = True,
    ) -> None:
        self._root = Path(journal_root)
        self._max_age_seconds = max(0.0, float(max_age_days)) * 86400.0
        self._interval_s = max(60.0, float(interval_hours) * 3600.0)
        self._enabled = bool(enabled) and self._max_age_seconds > 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        log.info(
            "journal_retention.start root=%s max_age_days=%.1f "
            "interval_hours=%.1f",
            self._root,
            self._max_age_seconds / 86400.0,
            self._interval_s / 3600.0,
        )
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _loop(self) -> None:
        # Short startup delay so the daemon's boot writes finish
        # before we walk the journal tree.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=300.0)
            return
        except asyncio.TimeoutError:
            pass
        await self._tick()

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_s,
                )
                return
            except asyncio.TimeoutError:
                pass
            await self._tick()

    async def _tick(self) -> None:
        """Walk the journal root and delete anything past the cutoff.

        Sync filesystem work (small). Wrapped in run_in_executor so a
        large directory walk doesn't block the loop. Failures are
        logged + counted; the task never raises out (one bad permission
        shouldn't take down the daemon).
        """
        if not self._root.is_dir():
            return
        try:
            loop = asyncio.get_event_loop()
            deleted_files, deleted_dirs = await loop.run_in_executor(
                None, self._prune, self._root, self._max_age_seconds,
            )
            log.info(
                "journal_retention.tick deleted_files=%d deleted_dirs=%d",
                deleted_files, deleted_dirs,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("journal_retention.tick_failed err=%s", exc)
            try:
                from xmclaw.utils.swallowed_exceptions import (
                    record as _swallow,
                )
                _swallow("journal_retention.tick", exc)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _prune(root: Path, max_age_seconds: float) -> tuple[int, int]:
        """Delete journal files / month directories older than the cutoff.

        Returns ``(file_count, dir_count)`` actually removed. Pure
        function over the filesystem — exposed for tests so the
        retention logic can be exercised without the asyncio loop.
        """
        cutoff = time.time() - max_age_seconds
        files_deleted = 0
        dirs_deleted = 0
        # Month dirs are named ``YYYY-MM``. Iterate them and act on
        # the *directory* mtime as the coarse cutoff signal. A month
        # entirely older than the cutoff gets nuked wholesale.
        for month_dir in sorted(root.iterdir()):
            if not month_dir.is_dir():
                continue
            try:
                d_mtime = month_dir.stat().st_mtime
            except OSError:
                continue
            if d_mtime < cutoff:
                # Entire month older than cutoff — delete recursively.
                try:
                    for child in month_dir.rglob("*"):
                        if child.is_file():
                            try:
                                child.unlink()
                                files_deleted += 1
                            except OSError:
                                pass
                    # Now drop the directory itself.
                    for sub in sorted(
                        (p for p in month_dir.rglob("*") if p.is_dir()),
                        reverse=True,
                    ):
                        try:
                            sub.rmdir()
                        except OSError:
                            pass
                    month_dir.rmdir()
                    dirs_deleted += 1
                except OSError:
                    pass
                continue
            # Month is recent enough — check individual files for
            # the boundary case (month dir is < cutoff but contains
            # session files older than cutoff).
            for f in month_dir.glob("*.jsonl"):
                try:
                    f_mtime = f.stat().st_mtime
                except OSError:
                    continue
                if f_mtime < cutoff:
                    try:
                        f.unlink()
                        files_deleted += 1
                    except OSError:
                        pass
        return files_deleted, dirs_deleted


__all__ = ["JournalRetentionTask"]
