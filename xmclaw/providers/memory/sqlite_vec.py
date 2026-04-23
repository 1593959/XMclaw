"""sqlite-vec backed MemoryProvider — default v2 implementation.

Anti-req #2 in code form:
* **Hierarchical** — three layers (short/working/long), each with its
  own TTL hint. Queries pick a layer; they don't scan everything flat.
* **Semantic** — when the caller supplies an embedding, we search by
  vector distance via sqlite-vec (not FTS5). FTS is available as a
  fallback only when no embedding is provided.
* **Not frozen in the system prompt** — this module is storage only.
  It returns matches on demand (RAG-style). The caller decides what to
  stitch into a prompt. There is NO "auto-inject recent memories"
  behaviour anywhere in this class.

Schema:

::

    memory_items (id TEXT PK, layer TEXT, text TEXT, metadata TEXT JSON,
                  ts REAL, has_embedding INTEGER)

    memory_vec (virtual table, vec0):
        item_id TEXT PK, embedding float[dim]

    memory_items_layer_ts: index for layer + ts range queries

Embedding dimension is fixed at table creation (sqlite-vec constraint).
It is provided via the constructor's ``embedding_dim`` parameter; if
callers need different-sized embeddings they should use separate
``SqliteVecMemory`` instances pointing at different db files.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# Default TTL hints (seconds). These are hints — eviction is explicit via
# ``forget`` or ``prune``, never silent during queries.
_DEFAULT_TTL: dict[str, float | None] = {
    "short":   60 * 60,          # 1 hour
    "working": 24 * 60 * 60,     # 1 day
    "long":    None,             # never
}


class SqliteVecMemory(MemoryProvider):
    """sqlite-vec backed memory with hierarchical layers.

    Parameters
    ----------
    db_path : Path
        Location of the SQLite DB. Use ``":memory:"`` for an in-process
        ephemeral store (tests, ad-hoc work).
    embedding_dim : int | None
        Dimension of vectors the caller will supply via
        ``MemoryItem.embedding``. When ``None`` (default), the vector
        table is NOT created; only timestamp-ordered retrieval works
        until the first time an embedding is put (at which point the
        dimension is frozen from that embedding's length).
    ttl : dict[str, float | None] | None
        Override TTL hints per layer. None for a layer means never expire.
    pinned_tags : list[str] | None
        Admin-level allowlist: items whose metadata has a matching
        ``tag`` / ``tags`` / ``category`` (or truthy ``pinned`` flag)
        are exempt from ``evict()``. Use this to protect "identity" /
        "promise" / "user-profile" items without editing each row.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        embedding_dim: int | None = None,
        ttl: dict[str, float | None] | None = None,
        pinned_tags: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._embedding_dim = embedding_dim
        self._ttl = {**_DEFAULT_TTL, **(ttl or {})}
        self._pinned_tags: frozenset[str] = frozenset(pinned_tags or ())
        self._conn = self._open_conn()
        self._ensure_schema()
        if embedding_dim is not None:
            self._ensure_vec_table(embedding_dim)

    # ── setup ──

    _vec_supported: bool = False

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Extension loading is a compile-time option in CPython's
        # sqlite3 module. Distributions ship varying levels of support:
        #   * Linux (Ubuntu GitHub runners, most distros): enabled
        #   * macOS (system Python / Homebrew / pyenv builds): often disabled
        #   * Windows: depends on the Python build
        # When the extension can't load, the non-vector paths
        # (timestamp-ordered retrieval, LIKE substring match, metadata
        # filters) still work. The vector-query path raises a clear
        # error at call time, which the conformance / unit tests skip
        # via ``skipif_no_vec``.
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_supported = True
        except (AttributeError, ImportError, sqlite3.OperationalError):
            self._vec_supported = False
        return conn

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY,
                layer TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT,
                ts REAL NOT NULL,
                has_embedding INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS memory_items_layer_ts "
            "ON memory_items(layer, ts)"
        )
        self._conn.commit()

    def _ensure_vec_table(self, dim: int) -> None:
        """Create the sqlite-vec virtual table if missing.

        sqlite-vec requires dimension in the DDL. If the table already
        exists at a different dim, that's a caller error — we raise.

        Raises ``RuntimeError`` when the sqlite-vec extension wasn't
        loadable (see ``_open_conn``'s docstring for why that happens).
        """
        if not self._vec_supported:
            raise RuntimeError(
                "sqlite-vec extension is not loadable on this Python "
                "build — vector retrieval is unavailable. Install a "
                "Python distribution with sqlite3 extension support, or "
                "skip vector queries and use text/timestamp retrieval."
            )
        cur = self._conn.cursor()
        # Check existing dim if table exists
        existing = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_vec'"
        ).fetchone()
        if existing:
            # Parse the declared dim from the sql (e.g. "... embedding float[1536])")
            sql = existing["sql"]
            # Best-effort parse; sqlite-vec formats this consistently.
            try:
                existing_dim = int(sql.split("float[")[1].split("]")[0])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(
                    f"memory_vec table exists but dim is unparseable: {sql!r}"
                ) from exc
            if existing_dim != dim:
                raise RuntimeError(
                    f"memory_vec created with dim={existing_dim}, "
                    f"cannot switch to dim={dim} on the same DB"
                )
            self._embedding_dim = existing_dim
            return
        cur.execute(
            f"CREATE VIRTUAL TABLE memory_vec USING vec0("
            f"item_id TEXT PRIMARY KEY, "
            f"embedding float[{dim}]"
            f")"
        )
        self._conn.commit()
        self._embedding_dim = dim

    # ── public API ──

    async def put(self, layer: Layer, item: MemoryItem) -> str:
        """Store an item. Returns the id (uses ``item.id`` or generates one)."""
        item_id = item.id or uuid.uuid4().hex
        ts = item.ts if item.ts else time.time()
        metadata = json.dumps(item.metadata) if item.metadata else None
        has_embedding = 1 if item.embedding else 0

        if item.embedding:
            # Lazy-init the vec table with the dimension of this first
            # embedding, unless the caller already specified one.
            if self._embedding_dim is None:
                self._ensure_vec_table(len(item.embedding))
            if len(item.embedding) != self._embedding_dim:
                raise ValueError(
                    f"embedding dim {len(item.embedding)} ≠ "
                    f"configured dim {self._embedding_dim}"
                )

        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO memory_items "
            "(id, layer, text, metadata, ts, has_embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, layer, item.text, metadata, ts, has_embedding),
        )
        if item.embedding:
            # Serialize vector as little-endian float32 bytes for sqlite-vec.
            import struct
            blob = struct.pack(f"{len(item.embedding)}f", *item.embedding)
            cur.execute(
                "INSERT OR REPLACE INTO memory_vec (item_id, embedding) VALUES (?, ?)",
                (item_id, blob),
            )
        self._conn.commit()
        return item_id

    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        """Retrieve up to ``k`` items from ``layer``.

        Retrieval mode (in order of preference):
          1. ``embedding`` provided → vector-similarity (cosine / L2 via sqlite-vec)
          2. ``text`` provided, no embedding → LIKE scan (substring match)
          3. neither → most-recent (ORDER BY ts DESC)

        ``filters`` is a ``metadata`` JSON dict matcher — every key/value
        in ``filters`` must match the item's metadata exactly.
        """
        cur = self._conn.cursor()
        filter_clause, filter_params = self._filter_sql(filters)

        if embedding is not None:
            if self._embedding_dim is None:
                # No vectors stored yet; fall through to the text / recent path.
                return await self.query(
                    layer, text=text, k=k, filters=filters,
                )
            if len(embedding) != self._embedding_dim:
                raise ValueError(
                    f"query embedding dim {len(embedding)} ≠ "
                    f"configured dim {self._embedding_dim}"
                )
            import struct
            qblob = struct.pack(f"{len(embedding)}f", *embedding)
            # vec0 KNN: join to memory_items, constrain layer.
            sql = f"""
                SELECT m.id, m.layer, m.text, m.metadata, m.ts,
                       m.has_embedding, v.distance
                FROM memory_vec v
                JOIN memory_items m ON m.id = v.item_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                  AND m.layer = ?
                  {filter_clause}
                ORDER BY v.distance
            """
            rows = cur.execute(sql, (qblob, k, layer, *filter_params)).fetchall()
        elif text:
            sql = f"""
                SELECT id, layer, text, metadata, ts, has_embedding
                FROM memory_items
                WHERE layer = ?
                  AND text LIKE ?
                  {filter_clause}
                ORDER BY ts DESC
                LIMIT ?
            """
            rows = cur.execute(
                sql, (layer, f"%{text}%", *filter_params, k),
            ).fetchall()
        else:
            sql = f"""
                SELECT id, layer, text, metadata, ts, has_embedding
                FROM memory_items
                WHERE layer = ?
                  {filter_clause}
                ORDER BY ts DESC
                LIMIT ?
            """
            rows = cur.execute(sql, (layer, *filter_params, k)).fetchall()

        return [self._row_to_item(r) for r in rows]

    async def forget(self, item_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        # memory_vec is separate; remove the vector too if it exists.
        try:
            cur.execute("DELETE FROM memory_vec WHERE item_id = ?", (item_id,))
        except sqlite3.OperationalError:
            # Vec table doesn't exist yet — nothing to forget there.
            pass
        self._conn.commit()

    # ── maintenance (explicit, never silent) ──

    async def prune(self, layer: Layer, *, older_than: float | None = None) -> int:
        """Remove items older than ``older_than`` seconds ago. Returns count.

        If ``older_than`` is None, uses the layer's configured TTL. A layer
        with TTL None is a no-op (returns 0) — ``long`` by default.
        """
        ttl = older_than if older_than is not None else self._ttl.get(layer)
        if ttl is None:
            return 0
        cutoff = time.time() - ttl
        cur = self._conn.cursor()
        ids = [
            r["id"] for r in cur.execute(
                "SELECT id FROM memory_items WHERE layer = ? AND ts < ?",
                (layer, cutoff),
            ).fetchall()
        ]
        if not ids:
            return 0
        self._delete_ids(ids)
        _log.info(
            "memory.evicted",
            layer=layer,
            count=len(ids),
            reason="age",
        )
        return len(ids)

    async def evict(
        self,
        layer: Layer,
        *,
        max_items: int | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """Cap-based LRU eviction. Returns count of removed items.

        Policy:
          * Within ``layer``, rows ordered by ``ts ASC`` are the LRU
            candidates. Items whose metadata carries a truthy ``pinned``
            flag (``{"pinned": true}`` etc.) are never evicted.
          * ``max_items`` caps the count of non-pinned rows. Rows beyond
            the cap — from oldest — are evicted.
          * ``max_bytes`` caps the sum of ``len(text.encode('utf-8'))``
            across non-pinned rows. Rows are dropped oldest-first until
            the sum fits.
          * Both caps may be passed together; union of both eviction
            sets is removed in one transaction.
          * Either cap may be ``None`` to disable that axis. Both ``None``
            is a no-op returning 0.

        Pinned items still count toward neither cap (they are neither
        evicted nor charged against the budget). This matches the
        expected admin pattern: operator pins critical items and sets
        caps for the rest.
        """
        if max_items is None and max_bytes is None:
            return 0
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT id, text, metadata FROM memory_items "
            "WHERE layer = ? ORDER BY ts ASC",
            (layer,),
        ).fetchall()
        non_pinned: list[tuple[str, int]] = []  # (id, byte_len), oldest first
        for r in rows:
            if self._is_pinned(r["metadata"]):
                continue
            non_pinned.append((r["id"], len(r["text"].encode("utf-8"))))

        victim_ids: set[str] = set()

        if max_items is not None and len(non_pinned) > max_items:
            # Drop oldest such that len == max_items.
            to_drop = len(non_pinned) - max_items
            for vid, _ in non_pinned[:to_drop]:
                victim_ids.add(vid)

        if max_bytes is not None:
            # Walk newest→oldest; keep items until budget is exhausted,
            # mark the rest for eviction. Equivalently: drop oldest
            # until the tail sum ≤ max_bytes.
            budget = max_bytes
            keep: set[str] = set()
            for vid, nbytes in reversed(non_pinned):
                if nbytes <= budget:
                    keep.add(vid)
                    budget -= nbytes
                else:
                    # This one and everything older gets evicted.
                    break
            for vid, _ in non_pinned:
                if vid not in keep:
                    victim_ids.add(vid)

        if not victim_ids:
            return 0
        ids = sorted(victim_ids)
        self._delete_ids(ids)
        reason = (
            "cap"
            if max_items is not None and max_bytes is not None
            else ("cap_items" if max_items is not None else "cap_bytes")
        )
        _log.info(
            "memory.evicted",
            layer=layer,
            count=len(ids),
            reason=reason,
        )
        return len(ids)

    def _delete_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        cur = self._conn.cursor()
        placeholders = ",".join("?" * len(ids))
        cur.execute(
            f"DELETE FROM memory_items WHERE id IN ({placeholders})",
            ids,
        )
        try:
            cur.execute(
                f"DELETE FROM memory_vec WHERE item_id IN ({placeholders})",
                ids,
            )
        except sqlite3.OperationalError:
            # Vec table doesn't exist — nothing to clean there.
            pass
        self._conn.commit()

    def _is_pinned(self, metadata_json: str | None) -> bool:
        """Return True when a row is exempt from cap-based eviction.

        Two sources of exemption:

        1. Per-row flag — truthy ``metadata.pinned``. Use when a caller
           knows a specific item must survive.
        2. Admin allowlist — ``pinned_tags`` constructor arg matches any
           of ``metadata.tag`` (scalar), ``metadata.tags`` (list), or
           ``metadata.category``. Use for coarse policy like "never
           evict identity or promise memories".

        Tolerant of malformed JSON — treats unparseable metadata as
        unpinned so a bad row can't accidentally become immortal.
        """
        if not metadata_json:
            return False
        try:
            meta = json.loads(metadata_json)
        except (ValueError, TypeError):
            return False
        if not isinstance(meta, dict):
            return False
        if meta.get("pinned"):
            return True
        if not self._pinned_tags:
            return False
        tag = meta.get("tag")
        if isinstance(tag, str) and tag in self._pinned_tags:
            return True
        category = meta.get("category")
        if isinstance(category, str) and category in self._pinned_tags:
            return True
        tags = meta.get("tags")
        if isinstance(tags, list) and any(
            isinstance(t, str) and t in self._pinned_tags for t in tags
        ):
            return True
        return False

    def close(self) -> None:
        self._conn.close()

    # ── helpers ──

    @staticmethod
    def _filter_sql(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        """Render a metadata filter as additional WHERE clauses.

        Uses SQLite's ``json_extract`` — requires the items' metadata to be
        stored as valid JSON (the put() path ensures this).
        """
        if not filters:
            return "", []
        clauses: list[str] = []
        params: list[Any] = []
        for k, v in filters.items():
            clauses.append(f"AND json_extract(metadata, '$.{k}') = ?")
            params.append(v)
        return " ".join(clauses), params

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return MemoryItem(
            id=row["id"],
            layer=row["layer"],
            text=row["text"],
            metadata=meta,
            embedding=None,  # embedding is not re-fetched (stays in vec table)
            ts=row["ts"],
        )
