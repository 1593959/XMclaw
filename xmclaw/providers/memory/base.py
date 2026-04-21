"""MemoryProvider ABC."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal

Layer = Literal["short", "working", "long"]


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    layer: Layer
    text: str
    metadata: dict[str, Any]
    embedding: tuple[float, ...] | None = None
    ts: float = 0.0


class MemoryProvider(abc.ABC):
    @abc.abstractmethod
    async def put(self, layer: Layer, item: MemoryItem) -> str: ...

    @abc.abstractmethod
    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]: ...

    @abc.abstractmethod
    async def forget(self, item_id: str) -> None: ...
