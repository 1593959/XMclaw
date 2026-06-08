"""LanceDB backends — production VectorBackend + GraphBackend.

Lazy-imports lancedb / pyarrow so the rest of xmclaw works in
environments that haven't installed the extras yet (CI minimal,
``pip install xmclaw`` without ``[memory-full]``).

Design notes:

* **One LanceDB connection** (``db = lancedb.connect_async(path)``)
  shared between vec + graph backends — both tables live in the
  same dataset directory.
* **Tables created lazily** on first ``upsert``. Schema is the
  Pydantic models embedded below; LanceDB infers PyArrow from them.
* **Upsert via ``merge_insert``** — the API we adopted LanceDB
  specifically for. No DELETE+INSERT workaround like sqlite-vec.
* **Filters via SQL strings** — LanceDB consumes SQL-flavoured
  ``where`` clauses directly; same syntax the InMemory backend's
  small parser accepts. Tests written against InMemory thus
  exercise the SAME filter shape.

The pydantic schemas are kept tight (no ``Optional`` for fields
that are required from L1) so type errors fail loud at write time.

Embedding dimension is configurable at construction (default 1536
for text-embedding-3-small / qwen-3-embedding-0.6b).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xmclaw.memory.v2.models import Fact, Relation, RelationKind

if TYPE_CHECKING:
    import lancedb
    from lancedb.pydantic import LanceModel


DEFAULT_EMBEDDING_DIM = 1536


class LanceDBSchemaError(RuntimeError):
    """Epic #27 sweep #7 (2026-05-19): raised by upsert when the
    on-disk schema is known to be missing a required column.

    Distinct from the generic ``ValueError("Field 'bucket' not found
    in target schema")`` that LanceDB raises per-row — this one fires
    ONCE per call (not 538×/day) with an actionable error message
    that names the fix path. Callers should catch it + emit a
    MEMORY_SCHEMA_DEGRADED bus event ONCE per daemon lifetime."""


def _build_fact_schema(dim: int):
    """Build the Fact Pydantic LanceModel for given embedding dim.

    Imports lazily so the module imports cleanly without lancedb.
    """
    from lancedb.pydantic import LanceModel, Vector

    class FactRecord(LanceModel):
        id: str
        kind: str
        scope: str
        text: str
        confidence: float
        evidence_count: int
        embedding: Vector(dim)  # type: ignore[valid-type]
        source_event_id: str  # "" when absent
        contradicts_json: str  # JSON-encoded list
        superseded_by: str  # "" when absent
        layer: str
        # Wave-27 fix-12: persona-renderer routing label. See
        # xmclaw/memory/v2/models.py:Fact.bucket for valid values.
        # Stored as plain string ("" when unbucketed).
        bucket: str
        ts_first: float
        ts_last: float
        # Phase 8 ⑩: bi-temporal validity. Stored as float with 0.0
        # as the "None / unset" sentinel (real timestamps are always
        # > 0). See models.py:Fact.valid_at / invalid_at.
        valid_at: float
        invalid_at: float
        # Wave-1 fix (2026-06-06): provenance for audit / defense.
        provenance: str

    return FactRecord


def _build_relation_schema():
    from lancedb.pydantic import LanceModel

    class RelationRecord(LanceModel):
        id: str
        source_fact_id: str
        target_fact_id: str
        relation: str
        strength: float
        auto_extracted: bool
        ts: float

    return RelationRecord


def _fact_to_record(fact: Fact, dim: int) -> dict[str, Any]:
    import json
    emb = list(fact.embedding) if fact.embedding else [0.0] * dim
    if len(emb) != dim:
        raise ValueError(
            f"embedding dim mismatch: got {len(emb)}, expected {dim}",
        )
    return {
        "id": fact.id,
        "kind": fact.kind,
        "scope": fact.scope,
        "text": fact.text,
        "confidence": fact.confidence,
        "evidence_count": fact.evidence_count,
        "embedding": emb,
        "source_event_id": fact.source_event_id or "",
        "contradicts_json": json.dumps(list(fact.contradicts)),
        "superseded_by": fact.superseded_by or "",
        "layer": fact.layer,
        "bucket": fact.bucket or "",
        "ts_first": fact.ts_first,
        "ts_last": fact.ts_last,
        "valid_at": fact.valid_at or 0.0,
        "invalid_at": fact.invalid_at or 0.0,
        "provenance": fact.provenance or "unknown",
    }


def _record_to_fact(row: dict[str, Any]) -> Fact:
    import json
    emb = row.get("embedding")
    if emb is not None and hasattr(emb, "tolist"):
        emb = emb.tolist()
    return Fact(
        id=row["id"],
        kind=row["kind"],
        scope=row["scope"],
        text=row["text"],
        confidence=float(row["confidence"]),
        evidence_count=int(row["evidence_count"]),
        embedding=tuple(emb) if emb else None,
        source_event_id=row["source_event_id"] or None,
        contradicts=tuple(json.loads(row.get("contradicts_json") or "[]")),
        superseded_by=row["superseded_by"] or None,
        layer=row["layer"],
        bucket=row.get("bucket") or "",  # Wave-27 fix-12; absent on legacy rows.
        ts_first=float(row["ts_first"]),
        ts_last=float(row["ts_last"]),
        # Phase 8 ⑩: 0.0 sentinel ⇒ None. Absent on legacy rows.
        valid_at=(
            float(row["valid_at"])
            if row.get("valid_at") not in (None, 0, 0.0) else None
        ),
        invalid_at=(
            float(row["invalid_at"])
            if row.get("invalid_at") not in (None, 0, 0.0) else None
        ),
        # Wave-1 fix: provenance field. Absent on legacy rows → "unknown".
        provenance=str(row.get("provenance") or "unknown"),
    )


def _relation_to_record(rel: Relation) -> dict[str, Any]:
    return {
        "id": rel.id,
        "source_fact_id": rel.source_fact_id,
        "target_fact_id": rel.target_fact_id,
        "relation": rel.relation,
        "strength": rel.strength,
        "auto_extracted": rel.auto_extracted,
        "ts": rel.ts,
    }


def _record_to_relation(row: dict[str, Any]) -> Relation:
    return Relation(
        id=row["id"],
        source_fact_id=row["source_fact_id"],
        target_fact_id=row["target_fact_id"],
        relation=row["relation"],
        strength=float(row["strength"]),
        auto_extracted=bool(row["auto_extracted"]),
        ts=float(row["ts"]),
    )


# ── Vector backend ────────────────────────────────────────────────


class LanceDBVectorBackend:
    """Production VectorBackend backed by LanceDB.

    Holds a lazy connection + table handle. Both are created on
    first write (or when ``ensure_ready`` is called explicitly).
    """

    def __init__(
        self,
        db_path: str,
        *,
        table_name: str = "facts",
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._dim = embedding_dim
        self._db: Any | None = None
        self._table: Any | None = None
        self._schema_cls: Any | None = None
        # Epic #27 sweep #7 (2026-05-19): track schema-migration state.
        # Pre-fix _maybe_add_missing_columns logged + continued when a
        # required column couldn't be added; every subsequent upsert
        # then failed with the cryptic "Field 'bucket' not found in
        # target schema" — daemon.log showed 538 such errors/day on
        # the user's machine, each one a LOST fact write (user input
        # silently dropped). Now: cache the migration failure, raise a
        # clear LanceDBSchemaError on the very first upsert call, and
        # let the daemon-level bus subscriber (memory_v2 router) emit
        # MEMORY_SCHEMA_DEGRADED ONCE per daemon lifetime instead of
        # 538×. ``schema_error`` is None when healthy; populated to
        # the original exception string when migration tried + failed
        # AND a required column is still missing.
        self._schema_error: str | None = None
        # Columns the code REQUIRES to be present (any write requires
        # them). Keep in sync with _MIGRATIONS below.
        self._required_columns: frozenset[str] = frozenset({"bucket"})
        # 2026-05-29 perf fix: when LanceDB data files are corrupted on
        # disk, every table access can hang the event loop for minutes
        # (Rust I/O never yields back to Python asyncio).  Once we see
        # a lance error we permanently short-circuit this backend so the
        # daemon stays responsive and falls back to V1 memory.
        # Wave-4 fix (2026-06-06): transient error retry before marking
        # permanently corrupted.
        self._corrupted: bool = False
        self._transient_failures: int = 0
        self._MAX_TRANSIENT_RETRIES: int = 3

    def _is_transient_lance_error(self, exc: RuntimeError) -> bool:
        """Distinguish recoverable errors from permanent corruption."""
        msg = str(exc).lower()
        transient_signatures = [
            "resource temporarily unavailable",
            "file is locked",
            "no space left",
            "permission denied",
            "device or resource busy",
        ]
        return any(sig in msg for sig in transient_signatures)

    async def attempt_repair(self) -> bool:
        """Try lance.dataset.cleanup() or table recovery."""
        if not self._corrupted:
            return True
        try:
            import lancedb
            db = await lancedb.connect_async(self._db_path)
            for name in await db.table_names():
                tbl = await db.open_table(name)
                await tbl.cleanup_old_versions()
            self._corrupted = False
            self._transient_failures = 0
            from xmclaw.utils.log import get_logger
            get_logger(__name__).info("lancedb.repair_success")
            return True
        except Exception as exc:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error("lancedb.repair_failed err=%s", exc)
            return False

    def _handle_lance_error(self, exc: RuntimeError, context: str) -> None:
        """Wave-4: transient retry before permanent corruption."""
        if "lance error" not in str(exc).lower():
            raise
        if self._is_transient_lance_error(exc):
            self._transient_failures += 1
            if self._transient_failures < self._MAX_TRANSIENT_RETRIES:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "lancedb.transient_retry attempt=%d/%d context=%s err=%s",
                    self._transient_failures, self._MAX_TRANSIENT_RETRIES,
                    context, exc,
                )
                return  # caller may retry
        # Permanent corruption or exhausted retries
        self._corrupted = True
        from xmclaw.utils.log import get_logger
        get_logger(__name__).error(
            "lancedb.permanent_corruption context=%s err=%s", context, exc,
        )

    async def _ensure_ready(self) -> None:
        if self._corrupted:
            return
        import lancedb
        try:
            if self._db is None:
                self._db = await lancedb.connect_async(self._db_path)
            if self._schema_cls is None:
                self._schema_cls = _build_fact_schema(self._dim)
            if self._table is None:
                # LanceDB 0.30+: list_tables() returns a pageable with
                # a .tables attribute; older releases had table_names().
                # Use the new API + fall back gracefully.
                page = await self._db.list_tables()
                existing = list(getattr(page, "tables", []) or page)
                if self._table_name in existing:
                    self._table = await self._db.open_table(self._table_name)
                    # Wave-27 fix-LAT15 (2026-05-17): migrate schema when
                    # the on-disk table predates a code-side schema
                    # addition. The 2026-05-15 ``bucket`` field add
                    # (refactor B Phase 1) shipped without a migration
                    # step, so every prod install with a table created
                    # before that date silently fails every fact write
                    # with "Field 'bucket' not found in target schema".
                    # Real-data: chat-c7040f1e on 2026-05-17 logged
                    # ``key_info_extractor.remember_failed`` every user
                    # message because of this. add_columns() is
                    # idempotent through the missing-check below.
                    await self._maybe_add_missing_columns()
                else:
                    self._table = await self._db.create_table(
                        self._table_name, schema=self._schema_cls,
                    )
        except RuntimeError as exc:
            self._handle_lance_error(exc, "ensure_ready")
            if not self._corrupted:
                return  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted path=%s err=%s — "
                "backend short-circuited, V2 memory disabled",
                self._db_path, exc,
            )
            return

    async def _maybe_add_missing_columns(self) -> None:
        """Add columns that the code-side schema declares but the
        on-disk table is missing. Each new column is added with an
        empty-string / 0.0 default so existing rows fit the new
        schema without rewrite.

        Idempotent: only adds columns when missing. Safe to call on
        every ``_ensure_ready``.

        Epic #27 sweep #7 (2026-05-19): on add_columns() failure we
        now set ``self._schema_error`` so subsequent writes refuse
        immediately with a clear error instead of producing 538
        cryptic "Field 'bucket' not found" failures per day.
        """
        from xmclaw.utils.log import get_logger
        log = get_logger(__name__)
        assert self._table is not None
        try:
            schema = await self._table.schema()
            on_disk = {f.name for f in schema}
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lancedb.schema_introspect_failed table=%s err=%s",
                self._table_name, exc,
            )
            return
        # (col_name, SQL default) — extend this list whenever the
        # code-side schema gains a new field. Defaults must be valid
        # Lance SQL expressions evaluated per-row.
        _MIGRATIONS: list[tuple[str, str]] = [
            ("bucket", "''"),  # Wave-27 fix-LAT15 / refactor B Phase 1
            ("valid_at", "0.0"),    # Phase 8 ⑩ — bi-temporal validity
            ("invalid_at", "0.0"),  # Phase 8 ⑩ — bi-temporal validity
            # 2026-06-07: the Wave-1 provenance field (2026-06-06) shipped
            # WITHOUT registering its migration here, so every table
            # created before then failed every fact write with
            # "Field 'provenance' not found in target schema" — daemon.log
            # showed key_info_extractor.remember_failed on every turn.
            ("provenance", "'unknown'"),
        ]
        for col, default in _MIGRATIONS:
            if col in on_disk:
                continue
            try:
                await self._table.add_columns({col: default})
                log.info(
                    "lancedb.schema_migrated table=%s column=%s "
                    "default=%s",
                    self._table_name, col, default,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "lancedb.schema_migration_failed table=%s "
                    "column=%s err=%s — future writes will refuse "
                    "until daemon restart + manual repair",
                    self._table_name, col, exc,
                )
                if col in self._required_columns:
                    self._schema_error = (
                        f"required column {col!r} missing and "
                        f"migration failed: {type(exc).__name__}: {exc}"
                    )

    @property
    def schema_error(self) -> str | None:
        """Epic #27 sweep #7: read-only check for "is the on-disk
        schema in a degraded state?". The memory_v2 router exposes
        this via ``/api/v2/memory/v2/status`` so the UI can show a
        red banner instead of users guessing why ``remember()`` is
        silently dropping their input."""
        return self._schema_error

    # ── Protocol surface ────────────────────────────────────────

    async def upsert(self, records: list[Fact]) -> int:
        if not records or self._corrupted:
            return 0
        await self._ensure_ready()
        if self._corrupted:
            return 0
        # Epic #27 sweep #7 (2026-05-19): refuse early when the
        # on-disk schema is known to be missing a required column.
        # Pre-fix every doomed write produced "Field 'bucket' not
        # found in target schema" — 538 of those/day on the user's
        # machine, each one a silently-dropped fact. Now: one clear
        # error per call, no cryptic stack trace bubbling up.
        if self._schema_error is not None:
            raise LanceDBSchemaError(
                f"refusing upsert: on-disk schema is degraded. "
                f"{self._schema_error} "
                f"Fix: restart daemon (re-runs schema migration), "
                f"or manually run ``ALTER TABLE {self._table_name} "
                f"ADD COLUMN bucket STRING DEFAULT ''`` via LanceDB "
                f"CLI then restart."
            )
        rows = [_fact_to_record(f, self._dim) for f in records]
        # merge_insert("id") — the LanceDB-native upsert. Replaces
        # the matching row entirely; merge semantics (evidence
        # accumulation, max confidence) live in memory_service.remember,
        # NOT here, because the backend doesn't know L1 business rules.
        assert self._table is not None
        try:
            await (
                self._table
                    .merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(rows)
            )
            return len(rows)
        except RuntimeError as exc:
            self._handle_lance_error(exc, "upsert")
            if not self._corrupted:
                return 0  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted_upsert err=%s — disabling V2 backend",
                exc,
            )
            return 0

    async def search(
        self,
        query: list[float] | str | None = None,
        *,
        where: str | None = None,
        limit: int = 8,
    ) -> list[Fact]:
        if self._corrupted:
            return []
        await self._ensure_ready()
        if self._corrupted:
            return []
        assert self._table is not None
        try:
            if query is None:
                # Pure-filter listing — order by ts_last DESC.
                builder = self._table.query()
                if where:
                    builder = builder.where(where)
                rows = await builder.limit(limit).to_list()
                rows.sort(key=lambda r: r.get("ts_last", 0.0), reverse=True)
                return [_record_to_fact(r) for r in rows[:limit]]

            if isinstance(query, str):
                # Keyword: LanceDB FTS requires an FTS index — Phase
                # 1a skips that and falls back to LIKE-style filter. The
                # MaterializeFTSIndex phase is Phase 5b (vis-network search).
                safe = query.replace("'", "''")
                where_combined = f"text LIKE '%{safe}%'"
                if where:
                    where_combined = f"({where_combined}) AND ({where})"
                builder = self._table.query().where(where_combined)
                rows = await builder.limit(limit).to_list()
                return [_record_to_fact(r) for r in rows[:limit]]

            # Vector query.
            # LanceDB's async API: ``table.search(vec)`` itself returns a
            # coroutine that yields the QueryBuilder — must await before
            # chaining .where / .limit. The 0.30+ AsyncStandardQuery.where
            # API no longer accepts a ``prefilter`` kwarg (filtering is
            # always applied pre-KNN); pass plain string.
            builder = await self._table.search(list(query))
            if where:
                builder = builder.where(where)
            rows = await builder.limit(limit).to_list()
            return [_record_to_fact(r) for r in rows[:limit]]
        except RuntimeError as exc:
            self._handle_lance_error(exc, "search")
            if not self._corrupted:
                return []  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted_search err=%s — disabling V2 backend",
                exc,
            )
            return []

    async def delete(self, where: str) -> int:
        if self._corrupted:
            return 0
        await self._ensure_ready()
        if self._corrupted:
            return 0
        assert self._table is not None
        try:
            before = await self._table.count_rows()
            await self._table.delete(where)
            after = await self._table.count_rows()
            return max(0, before - after)
        except RuntimeError as exc:
            self._handle_lance_error(exc, "delete")
            if not self._corrupted:
                return 0  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted_delete err=%s — disabling V2 backend",
                exc,
            )
            return 0

    async def count(self, where: str | None = None) -> int:
        if self._corrupted:
            return 0
        await self._ensure_ready()
        if self._corrupted:
            return 0
        assert self._table is not None
        try:
            if where:
                rows = await self._table.query().where(where).to_list()
                return len(rows)
            return await self._table.count_rows()
        except RuntimeError as exc:
            self._handle_lance_error(exc, "count")
            if not self._corrupted:
                return 0  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted_count err=%s — disabling V2 backend",
                exc,
            )
            return 0

    async def get(self, fact_id: str) -> Fact | None:
        if self._corrupted:
            return None
        await self._ensure_ready()
        if self._corrupted:
            return None
        assert self._table is not None
        try:
            safe = fact_id.replace("'", "''")
            rows = await self._table.query().where(f"id = '{safe}'").limit(1).to_list()
            if not rows:
                return None
            return _record_to_fact(rows[0])
        except RuntimeError as exc:
            self._handle_lance_error(exc, "get")
            if not self._corrupted:
                return None  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted_get err=%s — disabling V2 backend",
                exc,
            )
            return None

    async def close(self) -> None:
        # LanceDB async connection has no explicit close API yet;
        # garbage collection releases the file handles. Method exists
        # for Protocol parity + future migration.
        self._table = None
        self._db = None


# ── Graph backend ─────────────────────────────────────────────────


class LanceDBGraphBackend:
    """Production GraphBackend backed by LanceDB.

    Stores typed edges in the same Lance dataset directory as the
    facts table (different table). 1-hop neighbour traversal is a
    simple ``where`` query; multi-hop is application-side BFS.
    """

    def __init__(
        self,
        db_path: str,
        *,
        table_name: str = "relations",
    ) -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._db: Any | None = None
        self._table: Any | None = None
        self._schema_cls: Any | None = None
        # 2026-05-29 perf fix: mirror VectorBackend corruption guard.
        self._corrupted: bool = False
        self._transient_failures: int = 0
        self._MAX_TRANSIENT_RETRIES: int = 3

    def _is_transient_lance_error(self, exc: RuntimeError) -> bool:
        """Distinguish recoverable errors from permanent corruption."""
        msg = str(exc).lower()
        transient_signatures = [
            "timeout", "temporarily unavailable", "try again",
            "resource busy", "lock",
        ]
        return any(sig in msg for sig in transient_signatures)

    def _handle_lance_error(self, exc: RuntimeError, context: str) -> None:
        """Wave-4: transient retry before permanent corruption."""
        if "lance error" not in str(exc).lower():
            raise
        if self._is_transient_lance_error(exc):
            self._transient_failures += 1
            if self._transient_failures < self._MAX_TRANSIENT_RETRIES:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "lancedb.graph_transient_retry attempt=%d/%d context=%s err=%s",
                    self._transient_failures, self._MAX_TRANSIENT_RETRIES,
                    context, exc,
                )
                return  # caller may retry
        # Permanent corruption or exhausted retries
        self._corrupted = True
        from xmclaw.utils.log import get_logger
        get_logger(__name__).error(
            "lancedb.graph_permanent_corruption context=%s err=%s", context, exc,
        )

    async def _ensure_ready(self) -> None:
        if self._corrupted:
            return
        import lancedb
        try:
            if self._db is None:
                self._db = await lancedb.connect_async(self._db_path)
            if self._schema_cls is None:
                self._schema_cls = _build_relation_schema()
            if self._table is None:
                # LanceDB 0.30+: list_tables() returns a pageable with
                # a .tables attribute; older releases had table_names().
                # Use the new API + fall back gracefully.
                page = await self._db.list_tables()
                existing = list(getattr(page, "tables", []) or page)
                if self._table_name in existing:
                    self._table = await self._db.open_table(self._table_name)
                else:
                    self._table = await self._db.create_table(
                        self._table_name, schema=self._schema_cls,
                    )
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_ensure_ready")
            if not self._corrupted:
                return  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted path=%s err=%s — "
                "backend short-circuited",
                self._db_path, exc,
            )
            return

    # ── Protocol surface ────────────────────────────────────────

    async def add_relation(self, rel: Relation) -> None:
        await self.add_relations([rel])

    async def add_relations(self, rels: list[Relation]) -> int:
        if not rels or self._corrupted:
            return 0
        await self._ensure_ready()
        if self._corrupted:
            return 0
        assert self._table is not None
        rows = [_relation_to_record(r) for r in rels]
        try:
            await (
                self._table
                    .merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(rows)
            )
            return len(rels)
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_add")
            if not self._corrupted:
                return 0  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_add err=%s — disabling",
                exc,
            )
            return 0

    async def remove_relation(self, rel_id: str) -> None:
        if self._corrupted:
            return
        await self._ensure_ready()
        if self._corrupted:
            return
        assert self._table is not None
        safe = rel_id.replace("'", "''")
        try:
            await self._table.delete(f"id = '{safe}'")
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_remove")
            if not self._corrupted:
                return  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_remove err=%s — disabling",
                exc,
            )
            return

    async def neighbors(
        self,
        fact_id: str,
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> list[tuple[Relation, str]]:
        if self._corrupted:
            return []
        await self._ensure_ready()
        if self._corrupted:
            return []
        assert self._table is not None
        seen: set[str] = {fact_id}
        frontier = [fact_id]
        out: list[tuple[Relation, str]] = []
        try:
            for _ in range(max(1, max_hops)):
                if not frontier:
                    break
                quoted = ", ".join(
                    f"'{f.replace(chr(39), chr(39) + chr(39))}'" for f in frontier
                )
                where_parts = [f"source_fact_id IN ({quoted})"]
                if relation_types:
                    rels = ", ".join(f"'{r}'" for r in relation_types)
                    where_parts.append(f"relation IN ({rels})")
                where = " AND ".join(where_parts)
                rows = await self._table.query().where(where).to_list()
                next_frontier: list[str] = []
                for row in rows:
                    rel = _record_to_relation(row)
                    out.append((rel, rel.target_fact_id))
                    if rel.target_fact_id not in seen:
                        seen.add(rel.target_fact_id)
                        next_frontier.append(rel.target_fact_id)
                frontier = next_frontier
            return out
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_neighbors")
            if not self._corrupted:
                return []  # transient — caller may retry
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_neighbors err=%s — disabling",
                exc,
            )
            return []

    async def find_related(
        self,
        fact_ids: list[str],
        *,
        max_hops: int = 1,
        relation_types: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self._corrupted:
            return {"nodes": [], "edges": []}
        nodes: set[str] = set(fact_ids)
        edges: list[Relation] = []
        for fid in fact_ids:
            for rel, target in await self.neighbors(
                fid, relation_types=relation_types, max_hops=max_hops,
            ):
                edges.append(rel)
                nodes.add(target)
        edges.sort(key=lambda r: r.strength, reverse=True)
        edges = edges[:limit]
        seen_e: dict[str, Relation] = {e.id: e for e in edges}
        return {
            "nodes": sorted(nodes),
            "edges": list(seen_e.values()),
        }

    async def contradictions_of(self, fact_id: str) -> list[str]:
        if self._corrupted:
            return []
        out = []
        for rel, target in await self.neighbors(
            fact_id, relation_types=[RelationKind.CONTRADICTS.value],
        ):
            out.append(target)
        return out

    async def close(self) -> None:
        self._table = None
        self._db = None


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "LanceDBGraphBackend",
    "LanceDBVectorBackend",
]
