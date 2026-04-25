"""Periodic workspace backups — Epic #20 Phase 2.

Runs :func:`xmclaw.backup.create_backup` on a fixed interval (default
24h) and prunes stale auto-backups so the ``~/.xmclaw/backups/``
directory does not grow unbounded. Wired from ``create_app`` lifespan
when ``config['backup']['auto_daily']`` is truthy.

Design notes:
  * Asyncio background task, not OS cron — same rationale as
    :mod:`xmclaw.daemon.memory_sweep`: one event loop, no cross-process
    locking, shuts down cleanly with the daemon.
  * Backup + prune run inside an executor (``asyncio.to_thread``) so the
    tar.gz + sha256 pass does not block the event loop for the duration
    of a large workspace. ``create_backup`` / ``list_backups`` /
    ``delete_backup`` are all synchronous on purpose (rescue-env
    compatibility — see ``xmclaw/backup/AGENTS.md``), and we honor that
    boundary by scheduling them off-loop rather than demanding an async
    refactor of ``xmclaw.backup``.
  * Prune is **prefix-scoped**: only backups whose name starts with
    ``policy.name_prefix`` (default ``"auto-"``) are candidates for
    deletion. Manual backups made via ``xmclaw backup create <name>``
    are untouched — users pick their own names there and do not want
    the scheduler reaping them.
  * Failures in a tick are caught + logged — a bad tick (full disk,
    perms, transient I/O) must not kill the daemon.
  * ``tick_once()`` is public so tests can exercise one pass without
    the sleep loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xmclaw.backup import (
    BackupNotFoundError,
    create_backup,
    default_backups_dir,
    delete_backup,
    list_backups,
)
from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import data_dir

_log = get_logger(__name__)

DEFAULT_INTERVAL_S = 86400  # 24h
DEFAULT_KEEP = 7
DEFAULT_NAME_PREFIX = "auto-"


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """Resolved auto-backup config.

    ``auto_daily`` is the master switch. When False the task's
    :meth:`BackupSchedulerTask.start` is a no-op so daemons that don't
    opt in pay zero background cost.
    """

    auto_daily: bool = False
    interval_s: float = float(DEFAULT_INTERVAL_S)
    keep: int = DEFAULT_KEEP
    name_prefix: str = DEFAULT_NAME_PREFIX


def parse_backup_config(cfg: dict[str, Any] | None) -> BackupPolicy:
    """Build a :class:`BackupPolicy` from ``cfg['backup']``.

    Missing / malformed input returns the default disabled policy — a
    daemon that boots with a typo in its backup config is still more
    useful than one that refuses to start. Bad individual fields fall
    back to their defaults and log a warning.
    """
    if not isinstance(cfg, dict):
        return BackupPolicy()

    auto_daily = bool(cfg.get("auto_daily", False))

    interval_raw = cfg.get("interval_s", DEFAULT_INTERVAL_S)
    if isinstance(interval_raw, (int, float)) and interval_raw > 0:
        interval = float(interval_raw)
    else:
        _log.warning(
            "backup_scheduler.bad_interval",
            value=repr(interval_raw),
        )
        interval = float(DEFAULT_INTERVAL_S)

    keep_raw = cfg.get("keep", DEFAULT_KEEP)
    if isinstance(keep_raw, int) and keep_raw >= 0:
        keep = keep_raw
    else:
        _log.warning(
            "backup_scheduler.bad_keep",
            value=repr(keep_raw),
        )
        keep = DEFAULT_KEEP

    prefix_raw = cfg.get("name_prefix", DEFAULT_NAME_PREFIX)
    if isinstance(prefix_raw, str) and prefix_raw.strip():
        # Keep it filesystem-safe: no separators, no "." / ".." tricks.
        if (
            "/" in prefix_raw
            or "\\" in prefix_raw
            or prefix_raw in (".", "..")
        ):
            _log.warning(
                "backup_scheduler.bad_prefix",
                value=repr(prefix_raw),
            )
            prefix = DEFAULT_NAME_PREFIX
        else:
            prefix = prefix_raw
    else:
        _log.warning(
            "backup_scheduler.bad_prefix",
            value=repr(prefix_raw),
        )
        prefix = DEFAULT_NAME_PREFIX

    return BackupPolicy(
        auto_daily=auto_daily,
        interval_s=interval,
        keep=keep,
        name_prefix=prefix,
    )


def _auto_backup_name(prefix: str, *, now: float | None = None) -> str:
    """Sortable lex-ordered name — ``auto-20260425-143007``.

    Explicit UTC so daemons that run across DST / in containers without
    a local tz still produce monotonically-sortable names.
    """
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{prefix}{dt.strftime('%Y%m%d-%H%M%S')}"


class BackupSchedulerTask:
    """Background driver for auto-daily backups.

    Usage (from FastAPI lifespan)::

        scheduler = BackupSchedulerTask(source_dir, policy)
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    In tests, call :meth:`tick_once` directly — it runs the
    create+prune pair synchronously-as-async and returns the manifest
    so assertions don't need to re-read the JSON.
    """

    def __init__(
        self,
        source_dir: Path | None,
        policy: BackupPolicy,
        *,
        backups_dir: Path | None = None,
        clock: Any | None = None,
    ) -> None:
        """
        Args:
            source_dir: Workspace root to archive. ``None`` resolves via
                :func:`xmclaw.utils.paths.data_dir` at tick time.
            policy: Resolved :class:`BackupPolicy`.
            backups_dir: Where to write. ``None`` uses
                :func:`xmclaw.backup.default_backups_dir`.
            clock: Optional callable returning ``time.time()``-shaped
                floats. Tests inject a fake clock to make backup names
                deterministic without monkeypatching globals.
        """
        self._source_dir = source_dir
        self._policy = policy
        self._backups_dir = backups_dir
        self._clock = clock if clock is not None else time.time
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def tick_once(self) -> Any | None:
        """Run one create + prune pass.

        Returns the :class:`~xmclaw.backup.Manifest` on success or
        ``None`` when the tick was skipped (policy disabled, source
        missing, or an exception was caught). Failures log a structured
        event — the loop treats skipped ticks the same as successful
        ones so a bad day doesn't stall the cadence.
        """
        if not self._policy.auto_daily:
            return None

        source = self._source_dir if self._source_dir is not None else data_dir()
        if not source.exists():
            _log.warning(
                "backup_scheduler.source_missing",
                source=str(source),
            )
            return None

        now = self._clock()
        name = _auto_backup_name(self._policy.name_prefix, now=now)

        try:
            manifest = await asyncio.to_thread(
                create_backup,
                source,
                name,
                backups_dir=self._backups_dir,
                overwrite=True,
            )
        except Exception as exc:  # noqa: BLE001 — a bad tick must not kill the daemon
            _log.warning(
                "backup_scheduler.create_failed",
                name=name,
                error=repr(exc),
            )
            return None

        try:
            await asyncio.to_thread(self._prune_old_autos)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backup_scheduler.prune_failed",
                error=repr(exc),
            )

        _log.info(
            "backup_scheduler.tick_ok",
            name=name,
            entries=manifest.entries,
            archive_bytes=manifest.archive_bytes,
        )
        return manifest

    def _prune_old_autos(self) -> list[str]:
        """Delete all but the newest ``policy.keep`` prefix-matched backups.

        Runs on a thread pool, so it must stay synchronous. Returns the
        names of deleted backups for test assertions.
        """
        root = self._backups_dir or default_backups_dir()
        if not root.exists():
            return []
        all_entries = list_backups(backups_dir=root)
        # Prefix-scoped so manual backups survive the prune.
        autos = [
            e for e in all_entries if e.name.startswith(self._policy.name_prefix)
        ]
        # list_backups sorts ascending by created_ts — oldest first.
        excess = len(autos) - self._policy.keep
        if excess <= 0:
            return []
        deleted: list[str] = []
        for entry in autos[:excess]:
            try:
                delete_backup(entry.name, backups_dir=root)
            except BackupNotFoundError:
                continue
            deleted.append(entry.name)
        return deleted

    async def start(self) -> None:
        """Spawn the background loop if the policy is enabled.

        Idempotent — a second call while already running is a no-op.
        """
        if self._task is not None:
            return
        if not self._policy.auto_daily:
            # Master switch off — don't start the loop at all.
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="backup_scheduler")

    async def stop(self) -> None:
        """Request shutdown and wait for the loop to exit.

        Idempotent. Any CancelledError (or other exception raised during
        teardown) is swallowed — we are in a ``finally`` block on the
        daemon's critical shutdown path.
        """
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        interval = self._policy.interval_s
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            await self.tick_once()
