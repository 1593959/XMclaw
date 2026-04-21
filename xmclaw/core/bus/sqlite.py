"""Persistent SQLite-backed event log.

Phase 1: stub. Only the InProcessEventBus is required for the go/no-go demo.
Sqlite-backed persistence lands alongside the replay tool in Phase 1.5.

See docs/V2_DEVELOPMENT.md §4.1 for the schema.
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.core.bus.memory import InProcessEventBus


class SqliteEventBus(InProcessEventBus):
    """Durable event bus — appends every event to a sqlite WAL before fan-out.

    Phase 1: stub. Methods delegate to the in-process implementation until
    the schema + append-only table land.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        # TODO(phase-1.5): open sqlite, create schema, prepare append stmt.

    async def publish(self, event: BehavioralEvent) -> None:
        # TODO(phase-1.5): append to sqlite BEFORE fan-out so crash doesn't lose.
        await super().publish(event)
