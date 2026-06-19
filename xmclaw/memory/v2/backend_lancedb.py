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

# Stage 2 fix: LanceDB operations wrapped with inner timeout + retry (lancedb_utils.py).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xmclaw.memory.v2.models import Fact, Relation, RelationKind
from xmclaw.memory.v2.lancedb_utils import _with_timeout_and_retry

if TYPE_CHECKING:
    import lancedb
    from lancedb.pydantic import LanceModel


DEFAULT_EMBEDDING_DIM = 1536


class _LanceDBConnectionManager:
    """Singleton async connection manager per db_path.

    Epic #27 sweep #8 (2026-06-19): LanceDBVectorBackend and
    LanceDBGraphBackend used to call ``connect_async`` independently,
    producing two connections + two open_table calls at boot. This
    manager shares one AsyncConnection per db_path.
    """

    _connections: dict[str, Any] = {}

    @classmethod
    async def get_connection(cls, db_path: str) -> Any:
        if db_path not in cls._connections:
            import lancedb
            cls._connections[db_path] = await lancedb.connect_async(db_path)
        return cls._connections[db_path]

    @classmethod
    def reset(cls, db_path: str | None = None) -> None:
        """Test helper: evict cached connection(s)."""
        if db_path is None:
            cls._connections.clear()
        else:
            cls._connections.pop(db_path, None)


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
    # Capture LanceDB's _distance from the search result so recall()
    # and downstream callers can compute actual relevance (audit 2026-06-11).
    _dist = row.get("_distance", 0.0)
    return Fact(
        id=row["id"],
        kind=row["kind"],
        scope=row["scope"],
        text=row["text"],
        confidence=float(row["confidence"]),
        evidence_count=int(row["evidence_count"]),
        embedding=tuple(emb) if emb else None,
        _distance=float(_dist) if _dist is not None else 0.0,
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
        db: Any | None = None,
    ) -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._dim = embedding_dim
        self._db = db  # injected shared connection, or None → lazy via manager
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
            db = await _LanceDBConnectionManager.get_connection(self._db_path)
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
        """Stage 2: final-line defence after _with_timeout_and_retry exhausted.

        _with_timeout_and_retry already retries transient errors (Timeout,
        lance error with busy/lock/unavailable, ConnectionError).  When
        the wrapped coroutine still raises a RuntimeError that reaches here,
        we only mark permanent corruption for non-transient lance errors.
        """
        if "lance error" not in str(exc).lower():
            raise
        # Already marked corrupted by _with_timeout_and_retry on exhaustion
        if self._corrupted:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.corrupted context=%s err=%s", context, exc,
            )
            return
        # Defensive: if a transient somehow leaked through, do not corrupt
        if self._is_transient_lance_error(exc):
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "lancedb.transient_leaked context=%s err=%s", context, exc,
            )
            return
        # Permanent corruption
        self._corrupted = True
        from xmclaw.utils.log import get_logger
        get_logger(__name__).error(
            "lancedb.permanent_corruption context=%s err=%s", context, exc,
        )

    async def _ensure_ready(self) -> None:
        if self._corrupted:
            return
        try:
            if self._db is None:
                self._db = await _with_timeout_and_retry(
                    _LanceDBConnectionManager.get_connection(self._db_path),
                    timeout_s=10,
                    context="connect",
                    corrupted_flag=self,
                )
            if self._schema_cls is None:
                self._schema_cls = _build_fact_schema(self._dim)
            if self._table is None:
                # LanceDB 0.30+: list_tables() returns a pageable with
                # a .tables attribute; older releases had table_names().
                # Use the new API + fall back gracefully.
                page = await _with_timeout_and_retry(
                    self._db.list_tables(),
                    timeout_s=10,
                    context="list_tables",
                    corrupted_flag=self,
                )
                existing = list(getattr(page, "tables", []) or page)
                if self._table_name in existing:
                    self._table = await _with_timeout_and_retry(
                        self._db.open_table(self._table_name),
                        timeout_s=10,
                        context="open_table",
                        corrupted_flag=self,
                    )
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
                    await self._check_embedding_dim()
                else:
                    self._table = await _with_timeout_and_retry(
                        self._db.create_table(
                            self._table_name, schema=self._schema_cls,
                        ),
                        timeout_s=15,
                        context="create_table",
                        corrupted_flag=self,
                    )
        except RuntimeError as exc:
            self._handle_lance_error(exc, "ensure_ready")
            if not self._corrupted:
                if self._table is None:
                    self._corrupted = True
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).error(
                        "lancedb.ensure_ready_failed path=%s — table init failed, marking corrupted",
                        self._db_path,
                    )
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
            schema = await _with_timeout_and_retry(
                self._table.schema(),
                timeout_s=10,
                context="schema",
                corrupted_flag=self,
            )
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
                await _with_timeout_and_retry(
                    self._table.add_columns({col: default}),
                    timeout_s=10,
                    context="add_columns",
                    corrupted_flag=self,
                )
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

    async def _check_embedding_dim(self) -> None:
        """Detect drift between the configured embedding dim and the
        on-disk table's vector width.

        Real incident (2026-06-19): a user switched their embedder from a
        1536-D model (OpenAI text-embedding-3-small) to qwen3-embedding
        (1024-D) by editing config, but the ``facts`` table on disk was
        created at 1536. The dim is baked into the LanceDB fixed-size-list
        column, so — unlike a missing scalar column — it CANNOT be patched
        by ``add_columns``. Every write of an existing 1536-D row
        (forget / correct / restore) AND every new 1024-D insert failed
        with a raw ``ValueError: embedding dim mismatch`` → HTTP 500;
        daemon.log showed 55 of them. We can't reconcile the two dims here
        (re-embedding needs the embedder, which lives in the service
        layer), so we surface a clear, actionable ``schema_error`` and let
        ``upsert`` refuse fast with guidance instead of 500-ing per call.
        """
        assert self._table is not None
        try:
            schema = await _with_timeout_and_retry(
                self._table.schema(),
                timeout_s=10,
                context="schema_dim",
                corrupted_flag=self,
            )
        except Exception:  # noqa: BLE001 — never let the probe break boot
            return
        on_disk_dim: int | None = None
        for f in schema:
            if f.name == "embedding":
                # pyarrow FixedSizeListType exposes the width as list_size.
                on_disk_dim = getattr(f.type, "list_size", None)
                break
        if (
            isinstance(on_disk_dim, int)
            and on_disk_dim > 0
            and on_disk_dim != self._dim
        ):
            self._schema_error = (
                f"embedding dim drift: table {self._table_name!r} stores "
                f"{on_disk_dim}-D vectors but the configured embedder "
                f"produces {self._dim}-D. The width is baked into the "
                f"LanceDB column and cannot be auto-migrated. Fix: re-embed "
                f"all facts at {self._dim}-D and rebuild the table "
                f"(`xmclaw memory reembed`), or revert the embedding config "
                f"to the {on_disk_dim}-D model."
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
            # ``schema_error`` now covers two distinct degradations, each
            # carrying its own actionable fix text: a missing required
            # column (fixed by a daemon restart re-running the migration)
            # and an embedding dim drift (fixed by a re-embed + rebuild).
            # So we just surface the specific message rather than appending
            # one hardcoded ADD COLUMN hint that's wrong half the time.
            raise LanceDBSchemaError(
                f"refusing upsert: on-disk schema is degraded. "
                f"{self._schema_error}"
            )
        rows = [_fact_to_record(f, self._dim) for f in records]
        # merge_insert("id") — the LanceDB-native upsert. Replaces
        # the matching row entirely; merge semantics (evidence
        # accumulation, max confidence) live in memory_service.remember,
        # NOT here, because the backend doesn't know L1 business rules.
        assert self._table is not None
        try:
            await _with_timeout_and_retry(
                self._table
                    .merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(rows),
                timeout_s=15,
                max_retries=2,
                context="upsert",
                corrupted_flag=self,
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

    async def rebuild(self, records: list[Fact], *, dim: int) -> int:
        """Drop the table and recreate it at ``dim``, then insert ``records``.

        The embedding width is baked into the LanceDB fixed-size-list
        column, so a dim change (e.g. the user switched embedders) can't be
        patched in place — the table must be recreated. Used by the
        re-embed migration (``MemoryService.reembed_rebuild`` /
        ``xmclaw memory reembed``). ``records`` MUST already carry
        ``dim``-wide embeddings.

        Resets the corrupted / schema_error / dim state so the backend
        resumes normal operation against the fresh table. The caller is
        responsible for ensuring no other process holds the table open
        (stop the daemon first — LanceDB is single-writer).
        """
        if self._db is None:
            self._db = await _with_timeout_and_retry(
                _LanceDBConnectionManager.get_connection(self._db_path),
                timeout_s=10,
                context="connect",
                corrupted_flag=self,
            )
        # Drop the existing table if present.
        page = await _with_timeout_and_retry(
            self._db.list_tables(),
            timeout_s=10,
            context="list_tables",
            corrupted_flag=self,
        )
        existing = list(getattr(page, "tables", []) or page)
        if self._table_name in existing:
            await _with_timeout_and_retry(
                self._db.drop_table(self._table_name),
                timeout_s=15,
                context="drop_table",
                corrupted_flag=self,
            )
        # Reset state and create a fresh table at the new dim.
        self._dim = dim
        self._schema_cls = _build_fact_schema(dim)
        self._table = await _with_timeout_and_retry(
            self._db.create_table(self._table_name, schema=self._schema_cls),
            timeout_s=15,
            context="rebuild_create_table",
            corrupted_flag=self,
        )
        self._schema_error = None
        self._corrupted = False
        self._transient_failures = 0
        # Delegate the write to the validated upsert path (re-checks dim
        # per row against the now-correct self._dim).
        return await self.upsert(records)

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
                # F2 fix: fetch WITHOUT limit first, sort in Python,
                # THEN truncate. Previously limit() ran before sort(),
                # so callers like dedup_scope got a random subset.
                builder = self._table.query()
                if where:
                    builder = builder.where(where)
                rows = await _with_timeout_and_retry(
                    builder.to_list(),
                    timeout_s=10,
                    context="search",
                    corrupted_flag=self,
                )
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
                rows = await _with_timeout_and_retry(
                    builder.limit(limit).to_list(),
                    timeout_s=10,
                    context="search",
                    corrupted_flag=self,
                )
                return [_record_to_fact(r) for r in rows[:limit]]

            # Vector query.
            # LanceDB's async API: ``table.search(vec)`` itself returns a
            # coroutine that yields the QueryBuilder — must await before
            # chaining .where / .limit. The 0.30+ AsyncStandardQuery.where
            # API no longer accepts a ``prefilter`` kwarg (filtering is
            # always applied pre-KNN); pass plain string.
            builder = await _with_timeout_and_retry(
                self._table.search(list(query)),
                timeout_s=10,
                context="search",
                corrupted_flag=self,
            )
            if where:
                builder = builder.where(where)
            rows = await _with_timeout_and_retry(
                builder.limit(limit).to_list(),
                timeout_s=10,
                context="search",
                corrupted_flag=self,
            )
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
            before = await _with_timeout_and_retry(
                self._table.count_rows(),
                timeout_s=10,
                context="delete",
                corrupted_flag=self,
            )
            await _with_timeout_and_retry(
                self._table.delete(where),
                timeout_s=10,
                context="delete",
                corrupted_flag=self,
            )
            after = await _with_timeout_and_retry(
                self._table.count_rows(),
                timeout_s=10,
                context="delete",
                corrupted_flag=self,
            )
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
                rows = await _with_timeout_and_retry(
                    self._table.query().where(where).to_list(),
                    timeout_s=10,
                    context="count",
                    corrupted_flag=self,
                )
                return len(rows)
            return await _with_timeout_and_retry(
                self._table.count_rows(),
                timeout_s=10,
                context="count",
                corrupted_flag=self,
            )
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
            rows = await _with_timeout_and_retry(
                self._table.query().where(f"id = '{safe}'").limit(1).to_list(),
                timeout_s=10,
                context="get",
                corrupted_flag=self,
            )
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
        db: Any | None = None,
    ) -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._db = db  # injected shared connection, or None → lazy via manager
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
        """Stage 2: final-line defence after _with_timeout_and_retry exhausted.

        _with_timeout_and_retry already retries transient errors (Timeout,
        lance error with busy/lock/unavailable, ConnectionError).  When
        the wrapped coroutine still raises a RuntimeError that reaches here,
        we only mark permanent corruption for non-transient lance errors.
        """
        if "lance error" not in str(exc).lower():
            raise
        if self._corrupted:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted context=%s err=%s", context, exc,
            )
            return
        if self._is_transient_lance_error(exc):
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "lancedb.graph_transient_leaked context=%s err=%s", context, exc,
            )
            return
        self._corrupted = True
        from xmclaw.utils.log import get_logger
        get_logger(__name__).error(
            "lancedb.graph_permanent_corruption context=%s err=%s", context, exc,
        )

    async def _ensure_ready(self) -> None:
        if self._corrupted:
            return
        try:
            if self._db is None:
                self._db = await _with_timeout_and_retry(
                    _LanceDBConnectionManager.get_connection(self._db_path),
                    timeout_s=10,
                    context="graph_connect",
                    corrupted_flag=self,
                )
            if self._schema_cls is None:
                self._schema_cls = _build_relation_schema()
            if self._table is None:
                # LanceDB 0.30+: list_tables() returns a pageable with
                # a .tables attribute; older releases had table_names().
                # Use the new API + fall back gracefully.
                page = await _with_timeout_and_retry(
                    self._db.list_tables(),
                    timeout_s=10,
                    context="graph_list_tables",
                    corrupted_flag=self,
                )
                existing = list(getattr(page, "tables", []) or page)
                if self._table_name in existing:
                    self._table = await _with_timeout_and_retry(
                        self._db.open_table(self._table_name),
                        timeout_s=10,
                        context="graph_open_table",
                        corrupted_flag=self,
                    )
                else:
                    self._table = await _with_timeout_and_retry(
                        self._db.create_table(
                            self._table_name, schema=self._schema_cls,
                        ),
                        timeout_s=15,
                        context="graph_create_table",
                        corrupted_flag=self,
                    )
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_ensure_ready")
            if not self._corrupted:
                if self._table is None:
                    self._corrupted = True
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).error(
                        "lancedb.graph_ensure_ready_failed path=%s — table init failed, marking corrupted",
                        self._db_path,
                    )
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
            await _with_timeout_and_retry(
                self._table
                    .merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(rows),
                timeout_s=15,
                max_retries=2,
                context="graph_add",
                corrupted_flag=self,
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
            await _with_timeout_and_retry(
                self._table.delete(f"id = '{safe}'"),
                timeout_s=10,
                context="graph_remove",
                corrupted_flag=self,
            )
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
                rows = await _with_timeout_and_retry(
                    self._table.query().where(where).to_list(),
                    timeout_s=10,
                    context="graph_neighbors",
                    corrupted_flag=self,
                )
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

    async def reverse_neighbors(
        self,
        fact_id: str,
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> list[tuple[Relation, str]]:
        """Return edges where ``fact_id`` is the TARGET (incoming edges).

        Each tuple is ``(Relation, source_fact_id)`` — the source is the
        node that points TO ``fact_id``.  This is the dual of
        :meth:`neighbors` which only traverses outgoing edges.
        """
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
                    f"'{f.replace(chr(39), chr(39) + chr(39))}'"
                    for f in frontier
                )
                where_parts = [f"target_fact_id IN ({quoted})"]
                if relation_types:
                    rels = ", ".join(f"'{r}'" for r in relation_types)
                    where_parts.append(f"relation IN ({rels})")
                where = " AND ".join(where_parts)
                rows = await _with_timeout_and_retry(
                    self._table.query().where(where).to_list(),
                    timeout_s=10,
                    context="graph_reverse_neighbors",
                    corrupted_flag=self,
                )
                next_frontier: list[str] = []
                for row in rows:
                    rel = _record_to_relation(row)
                    out.append((rel, rel.source_fact_id))
                    if rel.source_fact_id not in seen:
                        seen.add(rel.source_fact_id)
                        next_frontier.append(rel.source_fact_id)
                frontier = next_frontier
            return out
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_reverse_neighbors")
            if not self._corrupted:
                return []
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_reverse_neighbors err=%s — disabling",
                exc,
            )
            return []

    async def neighbors_batch(
        self,
        fact_ids: list[str],
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> dict[str, list[tuple[Relation, str]]]:
        """Batch outgoing-edge query via a single ``WHERE IN`` query per hop."""
        if not fact_ids or self._corrupted:
            return {fid: [] for fid in fact_ids}
        await self._ensure_ready()
        if self._corrupted:
            return {fid: [] for fid in fact_ids}
        assert self._table is not None
        from collections import defaultdict

        all_out: dict[str, list[tuple[Relation, str]]] = {fid: [] for fid in fact_ids}
        all_seen: dict[str, set[str]] = {fid: {fid} for fid in fact_ids}
        all_frontiers: dict[str, list[str]] = {fid: [fid] for fid in fact_ids}
        try:
            for _ in range(max(1, max_hops)):
                node_to_sources: dict[str, list[str]] = defaultdict(list)
                for fid in fact_ids:
                    for node in all_frontiers[fid]:
                        node_to_sources[node].append(fid)
                if not node_to_sources:
                    break
                quoted = ", ".join(
                    f"'{f.replace(chr(39), chr(39) + chr(39))}'"
                    for f in node_to_sources
                )
                where_parts = [f"source_fact_id IN ({quoted})"]
                if relation_types:
                    rels = ", ".join(f"'{r}'" for r in relation_types)
                    where_parts.append(f"relation IN ({rels})")
                where = " AND ".join(where_parts)
                rows = await _with_timeout_and_retry(
                    self._table.query().where(where).to_list(),
                    timeout_s=10,
                    context="graph_neighbors_batch",
                    corrupted_flag=self,
                )
                next_frontiers: dict[str, list[str]] = {fid: [] for fid in fact_ids}
                for row in rows:
                    rel = _record_to_relation(row)
                    src = rel.source_fact_id
                    for fid in node_to_sources.get(src, []):
                        all_out[fid].append((rel, rel.target_fact_id))
                        if rel.target_fact_id not in all_seen[fid]:
                            all_seen[fid].add(rel.target_fact_id)
                            next_frontiers[fid].append(rel.target_fact_id)
                all_frontiers = next_frontiers
            return all_out
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_neighbors_batch")
            if not self._corrupted:
                return {fid: [] for fid in fact_ids}
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_neighbors_batch err=%s — disabling",
                exc,
            )
            return {fid: [] for fid in fact_ids}

    async def reverse_neighbors_batch(
        self,
        fact_ids: list[str],
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> dict[str, list[tuple[Relation, str]]]:
        """Batch incoming-edge query via a single ``WHERE IN`` query per hop."""
        if not fact_ids or self._corrupted:
            return {fid: [] for fid in fact_ids}
        await self._ensure_ready()
        if self._corrupted:
            return {fid: [] for fid in fact_ids}
        assert self._table is not None
        from collections import defaultdict

        all_out: dict[str, list[tuple[Relation, str]]] = {fid: [] for fid in fact_ids}
        all_seen: dict[str, set[str]] = {fid: {fid} for fid in fact_ids}
        all_frontiers: dict[str, list[str]] = {fid: [fid] for fid in fact_ids}
        try:
            for _ in range(max(1, max_hops)):
                node_to_sources: dict[str, list[str]] = defaultdict(list)
                for fid in fact_ids:
                    for node in all_frontiers[fid]:
                        node_to_sources[node].append(fid)
                if not node_to_sources:
                    break
                quoted = ", ".join(
                    f"'{f.replace(chr(39), chr(39) + chr(39))}'"
                    for f in node_to_sources
                )
                where_parts = [f"target_fact_id IN ({quoted})"]
                if relation_types:
                    rels = ", ".join(f"'{r}'" for r in relation_types)
                    where_parts.append(f"relation IN ({rels})")
                where = " AND ".join(where_parts)
                rows = await _with_timeout_and_retry(
                    self._table.query().where(where).to_list(),
                    timeout_s=10,
                    context="graph_reverse_neighbors_batch",
                    corrupted_flag=self,
                )
                next_frontiers: dict[str, list[str]] = {fid: [] for fid in fact_ids}
                for row in rows:
                    rel = _record_to_relation(row)
                    tgt = rel.target_fact_id
                    for fid in node_to_sources.get(tgt, []):
                        all_out[fid].append((rel, rel.source_fact_id))
                        if rel.source_fact_id not in all_seen[fid]:
                            all_seen[fid].add(rel.source_fact_id)
                            next_frontiers[fid].append(rel.source_fact_id)
                all_frontiers = next_frontiers
            return all_out
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_reverse_neighbors_batch")
            if not self._corrupted:
                return {fid: [] for fid in fact_ids}
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_reverse_neighbors_batch err=%s — disabling",
                exc,
            )
            return {fid: [] for fid in fact_ids}

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

    async def all_nodes(self) -> list[str]:
        """Return every fact_id appearing as source or target in any edge."""
        if self._corrupted:
            return []
        await self._ensure_ready()
        if self._corrupted or self._table is None:
            return []
        try:
            # LanceDB doesn't have a distinct "select distinct" in the
            # async Python SDK; we read all rows and deduplicate in memory.
            rows = await _with_timeout_and_retry(
                self._table.query().limit(100_000).to_list(),
                timeout_s=10,
                context="graph_all_nodes",
                corrupted_flag=self,
            )
            nodes: set[str] = set()
            for row in rows:
                nodes.add(str(row.get("source_fact_id", "")))
                nodes.add(str(row.get("target_fact_id", "")))
            return sorted(nodes)
        except RuntimeError as exc:
            self._handle_lance_error(exc, "graph_all_nodes")
            if not self._corrupted:
                return []
            from xmclaw.utils.log import get_logger
            get_logger(__name__).error(
                "lancedb.graph_corrupted_all_nodes err=%s — disabling", exc,
            )
            return []

    async def close(self) -> None:
        self._table = None
        self._db = None


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "LanceDBGraphBackend",
    "LanceDBVectorBackend",
]
