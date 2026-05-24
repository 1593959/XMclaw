"""LanceDB-backed MemoryProvider — workspace-index backend (Phase 7+).

Replaces sqlite-vec for ``file_chunk`` / ``code_chunk`` storage in
``MemoryFileIndexer``. Shares the same LanceDB dataset directory as
V2 facts (``~/.xmclaw/v2/facts/``) but lives in a separate table so
schema churn on either side doesn't break the other.

Lazy-imports ``lancedb`` / ``pyarrow`` so environments without the
extras still import cleanly.
"""
from __future__ import annotations

from typing import Any

from xmclaw.providers.memory.base import MemoryItem, MemoryProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class LanceDBMemoryProvider(MemoryProvider):
    """MemoryProvider backed by LanceDB.

    Parameters
    ----------
    db_path : str
        Directory path passed to ``lancedb.connect_async``.
    table_name : str
        Defaults to ``"workspace_chunks"``.
    embedding_dim : int
        Fixed at table creation; must match the embedder output.
    """

    name = "lancedb"

    def __init__(
        self,
        db_path: str,
        *,
        table_name: str = "workspace_chunks",
        embedding_dim: int = 1536,
    ) -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._dim = embedding_dim
        self._db: Any | None = None
        self._table: Any | None = None
        self._schema_cls: Any | None = None

    async def _ensure_ready(self) -> None:
        import lancedb

        if self._db is None:
            self._db = await lancedb.connect_async(self._db_path)
        if self._schema_cls is None:
            self._schema_cls = self._build_schema()
        if self._table is None:
            page = await self._db.list_tables()
            existing = list(getattr(page, "tables", []) or page)
            if self._table_name in existing:
                self._table = await self._db.open_table(self._table_name)
            else:
                self._table = await self._db.create_table(
                    self._table_name, schema=self._schema_cls,
                )

    def _build_schema(self):
        from lancedb.pydantic import LanceModel, Vector

        class ChunkRecord(LanceModel):
            id: str
            layer: str
            text: str
            kind: str
            source_path: str
            start_line: int
            end_line: int
            chunk_hash: str
            provider: str
            embedding: Vector(self._dim)  # type: ignore[valid-type]
            ts: float

        return ChunkRecord

    @staticmethod
    def _item_to_row(item: MemoryItem, dim: int) -> dict[str, Any]:
        emb = list(item.embedding) if item.embedding else [0.0] * dim
        if len(emb) != dim:
            raise ValueError(
                f"embedding dim mismatch: got {len(emb)}, expected {dim}",
            )
        md = item.metadata or {}
        return {
            "id": item.id,
            "layer": item.layer,
            "text": item.text,
            "kind": md.get("kind", "file_chunk"),
            "source_path": md.get("source_path", ""),
            "start_line": md.get("start_line", 0),
            "end_line": md.get("end_line", 0),
            "chunk_hash": md.get("chunk_hash", ""),
            "provider": md.get("provider", ""),
            "embedding": emb,
            "ts": item.ts,
        }

    @staticmethod
    def _row_to_item(row: dict[str, Any]) -> MemoryItem:
        emb = row.get("embedding")
        if emb is not None and hasattr(emb, "tolist"):
            emb = emb.tolist()
        return MemoryItem(
            id=row["id"],
            layer=row["layer"],
            text=row["text"],
            metadata={
                "kind": row.get("kind", ""),
                "source_path": row.get("source_path", ""),
                "start_line": row.get("start_line", 0),
                "end_line": row.get("end_line", 0),
                "chunk_hash": row.get("chunk_hash", ""),
                "provider": row.get("provider", ""),
            },
            embedding=tuple(emb) if emb else None,
            ts=float(row.get("ts", 0.0)),
        )

    async def put(self, layer: str, item: MemoryItem) -> str:
        await self._ensure_ready()
        row = self._item_to_row(item, self._dim)
        assert self._table is not None
        await (
            self._table
            .merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([row])
        )
        return item.id

    async def query(
        self,
        layer: str,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        await self._ensure_ready()
        assert self._table is not None

        # Build LanceDB where clause.
        where_parts = [f"layer = '{layer.replace(chr(39), chr(39)+chr(39))}'"]
        if filters:
            for key, val in filters.items():
                safe_val = str(val).replace(chr(39), chr(39)+chr(39))
                where_parts.append(f"{key} = '{safe_val}'")
        where_clause = " AND ".join(where_parts)

        if embedding:
            builder = await self._table.search(list(embedding))
            builder = builder.where(where_clause)
            rows = await builder.limit(k).to_list()
        elif text:
            safe = text.replace(chr(39), chr(39)+chr(39))
            like = f"text LIKE '%{safe}%'"
            where_clause = f"({like}) AND ({where_clause})"
            rows = await self._table.query().where(where_clause).limit(k).to_list()
        else:
            rows = await self._table.query().where(where_clause).limit(k).to_list()

        return [self._row_to_item(r) for r in rows]

    async def forget(self, item_id: str) -> None:
        await self._ensure_ready()
        assert self._table is not None
        safe = item_id.replace(chr(39), chr(39)+chr(39))
        await self._table.delete(f"id = '{safe}'")
