"""sqlite-vec backed MemoryProvider — default v2 implementation.

Phase 1: stub. Migrates from ``xmclaw/memory/*`` v1 modules in Phase 2 and
adds the short/working/long layering required by anti-req #2.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider


class SqliteVecMemory(MemoryProvider):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def put(self, layer: Layer, item: MemoryItem) -> str:  # noqa: ARG002
        raise NotImplementedError("Phase 2")

    async def query(  # noqa: PLR0913
        self,
        layer: Layer,  # noqa: ARG002
        *,
        text: str | None = None,  # noqa: ARG002
        embedding: list[float] | None = None,  # noqa: ARG002
        k: int = 10,  # noqa: ARG002
        filters: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> list[MemoryItem]:
        raise NotImplementedError("Phase 2")

    async def forget(self, item_id: str) -> None:  # noqa: ARG002
        raise NotImplementedError("Phase 2")
