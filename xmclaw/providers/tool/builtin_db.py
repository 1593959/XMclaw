from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail



class BuiltinToolsDbMixin:
    """Database tools: memory_search, sqlite_query."""

    async def _memory_search(self, call: ToolCall, t0: float) -> ToolResult:
        """B-40: unified memory_search — fan a query across every wired
        memory provider via MemoryManager.query.

        B-42: when an EmbeddingProvider is wired, the query gets
        embedded first and the manager routes the dense vector to
        SqliteVecMemory's KNN path — real semantic hits, not just
        substring. Without an embedder we fall through to the
        keyword path (same behaviour as B-40).

        Returns up to k hits per provider, each row carrying its
        originating provider in metadata.provider so the agent can
        tell vector hits from persona-bullet keyword hits.
        """
        query = str(call.args.get("query") or "").strip()
        if not query:
            return _fail(call, t0, "missing 'query'")
        try:
            k = int(call.args.get("k") or 5)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 20))
        layer = str(call.args.get("layer") or "long")
        if layer not in ("short", "working", "long"):
            return _fail(call, t0, f"unknown layer: {layer!r}")

        # B-197: optional kind filter — agent narrows to one record
        # type (preference / lesson / principle / etc.) instead of
        # searching across the whole store. Implemented as a metadata
        # filter forwarded to MemoryProvider.query — sqlite_vec already
        # supports `filters={"kind": ...}` on both vector and keyword
        # paths via _filter_sql.
        kind_filter = (call.args.get("kind") or "").strip() or None
        filters: dict[str, Any] | None = (
            {"kind": kind_filter} if kind_filter else None
        )

        # B-42: try semantic via the embedder; fall back to keyword on
        # any failure so an embedding outage degrades gracefully.
        embedding: list[float] | None = None
        used_mode = "keyword"
        if self._embedder is not None:
            try:
                vecs = await self._embedder.embed([query])
                if vecs and vecs[0]:
                    embedding = list(vecs[0])
                    used_mode = "semantic"
            except Exception:  # noqa: BLE001
                embedding = None

        try:
            # B-50: hybrid Vector + keyword RRF when both signals are
            # available; manager falls back to plain vector / keyword
            # for providers that don't implement hybrid_query.
            hits = await self._memory_manager.query(  # type: ignore[union-attr]
                layer, text=query, embedding=embedding, k=k, hybrid=True,
                filters=filters,
            )
        except TypeError:
            # Older MemoryManager without hybrid kwarg — still works.
            hits = await self._memory_manager.query(  # type: ignore[union-attr]
                layer, text=query, embedding=embedding, k=k, filters=filters,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"memory_search failed: {exc}")
        # Tag the mode reflecting what the manager actually used.
        if embedding and used_mode == "semantic":
            used_mode = "hybrid"

        # B-53: total-chars cap so a wide search doesn't flood the
        # context. Defaults to 6000 chars (~1500 tokens at chars/4) —
        # enough for ~15 chunks of typical MEMORY.md size, well under
        # most context windows. The agent can opt for a shorter cap
        # via ``max_chars``.
        try:
            max_chars = int(call.args.get("max_chars") or 6000)
        except (TypeError, ValueError):
            max_chars = 6000
        max_chars = max(500, min(max_chars, 20000))

        rows: list[dict[str, Any]] = []
        used_chars = 0
        truncated = False
        for h in hits[:k * 4]:  # 4 = max possible providers
            md = dict(getattr(h, "metadata", None) or {})
            text = (getattr(h, "text", "") or "")[:400]
            # Stop accumulating once budget is exhausted; flag in result.
            if used_chars + len(text) > max_chars and rows:
                truncated = True
                break
            rows.append({
                "id": getattr(h, "id", ""),
                "text": text,
                "ts": getattr(h, "ts", 0.0),
                "kind": md.get("kind") or "?",  # B-197: surface kind
                "provider": md.get("provider") or md.get("backend") or md.get("file") or "?",
                "metadata": md,
            })
            used_chars += len(text)
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "query": query,
                "layer": layer,
                "k": k,
                "mode": used_mode,
                "rows": rows,
                "row_count": len(rows),
                "total_chars": used_chars,
                "truncated": truncated,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _sqlite_query(self, call: ToolCall, t0: float) -> ToolResult:
        """B-37: read-only SQL against the agent's own state DBs.

        Refuses anything that mutates. We use sqlite3's ``authorizer``
        callback as the primary defence: every action the engine
        considers gets vetted, so even sneaky things like
        ``WITH RECURSIVE foo AS (...) DELETE FROM bar`` are caught
        before any rows move. A whitelist statement-prefix check is
        a second belt — keeps obvious garbage out of the parser.

        Connections open with ``mode=ro`` URI so the file itself is
        opened read-only at the OS level too — three layers of
        protection, defensive enough for a tool the LLM can call.
        """
        import sqlite3
        from xmclaw.utils.paths import data_dir

        db_choice = str(call.args.get("db") or "").strip().lower()
        sql = str(call.args.get("sql") or "").strip()
        params_raw = call.args.get("params") or []
        limit = call.args.get("limit")

        # Resolve the DB path (allowlisted).
        if db_choice == "events":
            db_path = data_dir() / "v2" / "events.db"
        elif db_choice == "memory":
            db_path = data_dir() / "v2" / "memory.db"
        else:
            return _fail(
                call, t0,
                f"unknown db {db_choice!r}; expected 'events' or 'memory'",
            )
        if not db_path.is_file():
            return _fail(call, t0, f"db not yet created: {db_path}")

        if not sql:
            return _fail(call, t0, "missing 'sql'")

        # Statement-prefix whitelist. Strip leading comment lines first.
        cleaned_lines = []
        for ln in sql.splitlines():
            s = ln.strip()
            if not s or s.startswith("--"):
                continue
            cleaned_lines.append(ln)
        cleaned = "\n".join(cleaned_lines).strip()
        head = cleaned.split(None, 1)[0].upper() if cleaned else ""
        if head not in ("SELECT", "PRAGMA", "EXPLAIN", "WITH"):
            return _fail(
                call, t0,
                f"only SELECT/PRAGMA/EXPLAIN/WITH allowed (got {head!r})",
            )
        # Reject multi-statement input via stripped trailing-semicolon-aware
        # check — sqlite3 in Python's default mode would only execute the
        # first statement anyway, but we want a clean error.
        body_no_trailing_semi = cleaned.rstrip(";").strip()
        if ";" in body_no_trailing_semi:
            return _fail(
                call, t0,
                "multi-statement input rejected; pass one statement at a time",
            )

        # Coerce params.
        params: tuple = ()
        if isinstance(params_raw, list):
            try:
                params = tuple(params_raw)
            except (TypeError, ValueError):
                return _fail(call, t0, "params must be a list of scalars")

        # Cap row count.
        try:
            n = int(limit) if limit is not None else 50
        except (TypeError, ValueError):
            n = 50
        n = max(1, min(n, 200))

        # Authorizer: deny anything that isn't a pure read.
        ALLOWED_ACTIONS = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_ANALYZE,
            sqlite3.SQLITE_RECURSIVE,
        }

        def _authorizer(action, *_args):  # type: ignore[no-untyped-def]
            if action in ALLOWED_ACTIONS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        # ``mode=ro`` makes the OS-level handle read-only.
        uri = f"file:{db_path}?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            return _fail(call, t0, f"open failed: {exc}")

        con.row_factory = sqlite3.Row
        con.set_authorizer(_authorizer)
        try:
            cur = con.execute(cleaned, params)
            rows = cur.fetchmany(n)
            cols = [d[0] for d in (cur.description or [])]
        except sqlite3.Error as exc:
            # B-203: probe data showed 6/11 sqlite_query calls in
            # one audit_pref_kinds turn failed with "no such table:
            # memories" — agent guessed a name that doesn't exist
            # and re-tried multiple times instead of introspecting.
            # When the error is a schema-shape error, surface the
            # actual available tables (or columns of the named
            # table) alongside the error so the next hop has the
            # info to recover without a second tool call.
            err_str = str(exc)
            schema_hint = ""
            try:
                low = err_str.lower()
                # Re-disable authorizer for the meta-query so
                # sqlite_master access is allowed (it's a read,
                # but using the authorizer adds noise here).
                con.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                if "no such table" in low:
                    meta = con.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' ORDER BY name"
                    ).fetchall()
                    names = [r[0] for r in meta if r[0]]
                    if names:
                        schema_hint = (
                            f" — available tables in '{db_choice}': "
                            f"{', '.join(names)}"
                        )
                    # B-205 cross-tie: if the user is querying memory.db
                    # and the table didn't exist, the question is almost
                    # always semantic ("what does the agent know about X")
                    # — point them at memory_search so they don't keep
                    # guessing schema. This is the recovery path B-205's
                    # prompt change was nudging for; surface it from the
                    # error itself too in case the prompt nudge misses.
                    if db_choice == "memory":
                        schema_hint += (
                            ". For 'what does the agent remember about "
                            "<topic>' queries, use ``memory_search(query, "
                            "kind=?)`` instead of raw SQL — it's faster "
                            "and won't fail with 'no such table'."
                        )
                elif "no such column" in low:
                    # Try to extract the table name from the SQL
                    # ("FROM <table>") to point at its real columns.
                    import re as _re
                    m = _re.search(r"FROM\s+([A-Za-z_][A-Za-z_0-9]*)", cleaned, _re.IGNORECASE)
                    if m:
                        tbl = m.group(1)
                        try:
                            meta = con.execute(
                                f"PRAGMA table_info({tbl})"
                            ).fetchall()
                            names = [r[1] for r in meta if r[1]]
                            if names:
                                schema_hint = (
                                    f" — columns of '{tbl}': "
                                    f"{', '.join(names)}"
                                )
                        except sqlite3.Error:
                            pass
            except sqlite3.Error:
                pass
            return _fail(call, t0, f"query failed: {err_str}{schema_hint}")
        finally:
            con.close()

        out_rows = [dict(r) for r in rows]
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "db": db_choice,
                "columns": cols,
                "rows": out_rows,
                "row_count": len(out_rows),
                "truncated": len(out_rows) >= n,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

