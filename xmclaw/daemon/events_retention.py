"""B-309: events.db retention task.

Deletes events older than ``max_age_days`` once a day and runs an
incremental vacuum to reclaim disk space. Without this, events.db
grows monotonically (~10 MB/day on a moderately active daemon),
hitting GB scale within a few months. Peers (LangGraph, Letta,
Codex CLI) all punt this back to the operator; XMclaw ships it
turnkey.

Runs as an asyncio sleep loop. Default config:

    {
      "events_retention": {
        "enabled": true,
        "max_age_days": 30,
        "interval_hours": 24
      }
    }

Set ``enabled=false`` to disable. Set ``max_age_days=0`` to keep
forever (matches legacy behaviour).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class EventsRetentionTask:
    """Periodic events.db prune + incremental vacuum."""

    def __init__(
        self,
        bus: Any,
        *,
        max_age_days: float = 30.0,
        interval_hours: float = 24.0,
        enabled: bool = True,
    ) -> None:
        self._bus = bus
        self._max_age_seconds = max(0.0, float(max_age_days)) * 86400.0
        self._interval_s = max(60.0, float(interval_hours) * 3600.0)
        self._enabled = bool(enabled) and self._max_age_seconds > 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        if not hasattr(self._bus, "prune_older_than"):
            log.info(
                "events_retention.skip: bus doesn't support prune (%s)",
                type(self._bus).__name__,
            )
            return
        log.info(
            "events_retention.start max_age_days=%.1f interval_hours=%.1f",
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
        # First pass after a short delay so daemon startup isn't
        # competing with prune for write lock.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=300.0)
            return  # stop_event fired
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
        try:
            # prune is sync (sqlite3 isn't async). Run in default
            # executor to avoid stalling the loop on a multi-MB delete.
            loop = asyncio.get_event_loop()
            deleted = await loop.run_in_executor(
                None, self._bus.prune_older_than, self._max_age_seconds,
            )
            log.info(
                "events_retention.tick deleted=%d max_age_days=%.1f",
                deleted, self._max_age_seconds / 86400.0,
            )
        except Exception as exc:  # noqa: BLE001 — prune must not crash daemon
            log.warning("events_retention.tick_failed err=%s", exc)
