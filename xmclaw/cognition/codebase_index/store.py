"""CodebaseIndex store — SQLite + sqlite-vec + FTS5.

Schema
------
projects        root_path, name, last_indexed, file_count
files           relpath PK, size, mtime, content_hash, last_indexed
chunks          id PK, relpath, start_line, end_line, chunk_type,
                symbol_name, symbol_kind, signature, text
chunks_fts      FTS5 virtual table over chunks.text
chunks_vec      sqlite-vec virtual table (embedding float[dim])

Vector operations gracefully degrade to text/FTS when sqlite-vec is
unavailable (same pattern as SqliteVecMemory).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

from xmclaw.cognition.codebase_index.chunker import Chunk
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class CodebaseStore:
    """Persistent storage for codebase index data.

    Parameters
    ----------
    db_path : Path
        SQLite file location. Parent dirs are created if missing.
    embedding_dim : int | None
        Dimension of embeddings. When ``None``, vector table is not
        created and semantic search falls back to FTS5.
    """

    def __init__(self, db_path: Path, *, embedding_dim: int | None = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedding_dim = embedding_dim
        self._vec_supported = False
        self._conn = self._open_conn()
        self._ensure_schema()
        if embedding_dim is not None:
            try:
                self._ensure_vec_table(embedding_dim)
            except RuntimeError as exc:
                _log.warning("codebase.vec_unavailable: %s", exc)

    # ── connection & schema ──

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
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
            CREATE TABLE IF NOT EXISTS projects (
                root_path TEXT PRIMARY KEY,
                name TEXT,
                last_indexed REAL,
                file_count INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                relpath TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                content_hash TEXT,
                last_indexed REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                relpath TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                chunk_type TEXT,
                symbol_name TEXT,
                symbol_kind TEXT,
                signature TEXT,
                text TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_relpath ON chunks(relpath)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(symbol_name, symbol_kind)"
        )
        # FTS5 for text search fallback.
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, content='chunks', content_rowid='rowid'
            )
        """)
        self._conn.commit()

    def _ensure_vec_table(self, dim: int) -> None:
        if not self._vec_supported:
            raise RuntimeError("sqlite-vec extension not loadable")
        cur = self._conn.cursor()
        existing = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        if existing:
            sql = existing["sql"]
            try:
                existing_dim = int(sql.split("float[")[1].split("]")[0])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(f"chunks_vec dim unparseable: {sql!r}") from exc
            if existing_dim != dim:
                raise RuntimeError(
                    f"chunks_vec dim={existing_dim}, cannot switch to {dim}"
                )
            return
        cur.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0("
            f"chunk_id TEXT PRIMARY KEY, embedding float[{dim}]"
            f")"
        )
        self._conn.commit()

    # ── write ──

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def upsert_project(self, root_path: str, name: str, file_count: int) -> None:
        cur = self._conn.cursor()
        import time
        cur.execute(
            "INSERT OR REPLACE INTO projects (root_path, name, last_indexed, file_count) "
            "VALUES (?, ?, ?, ?)",
            (root_path, name, time.time(), file_count),
        )
        self._conn.commit()

    def delete_project(self, root_path: str) -> None:
        """Remove all data for a project."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM chunks_vec WHERE chunk_id IN (SELECT id FROM chunks WHERE relpath LIKE ?)", (root_path + "/%",))
        cur.execute("DELETE FROM chunks WHERE relpath LIKE ?", (root_path + "/%",))
        cur.execute("DELETE FROM files WHERE relpath LIKE ?", (root_path + "/%",))
        cur.execute("DELETE FROM projects WHERE root_path = ?", (root_path,))
        self._conn.commit()

    def upsert_file(self, relpath: str, size: int, mtime: float, text: str) -> str:
        """Upsert file metadata. Returns content hash."""
        h = self._hash(text)
        cur = self._conn.cursor()
        import time
        cur.execute(
            "INSERT OR REPLACE INTO files (relpath, size, mtime, content_hash, last_indexed) "
            "VALUES (?, ?, ?, ?, ?)",
            (relpath, size, mtime, h, time.time()),
        )
        self._conn.commit()
        return h

    def file_hash(self, relpath: str) -> str | None:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT content_hash FROM files WHERE relpath = ?", (relpath,)
        ).fetchone()
        return row["content_hash"] if row else None

    def delete_file_chunks(self, relpath: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM chunks_vec WHERE chunk_id IN (SELECT id FROM chunks WHERE relpath = ?)", (relpath,))
        cur.execute("DELETE FROM chunks WHERE relpath = ?", (relpath,))
        self._conn.commit()

    def insert_chunks(self, chunks: list[Chunk], embeddings: list[list[float]] | None = None) -> None:
        """Bulk insert chunks with optional embeddings.

        *chunks* and *embeddings* must be the same length when embeddings
        is provided.
        """
        if embeddings is not None and len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")

        cur = self._conn.cursor()
        for i, chunk in enumerate(chunks):
            cur.execute(
                "INSERT OR REPLACE INTO chunks (id, relpath, start_line, end_line, "
                "chunk_type, symbol_name, symbol_kind, signature, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.id, chunk.relpath, chunk.start_line, chunk.end_line,
                    chunk.chunk_type, chunk.symbol_name, chunk.symbol_kind,
                    chunk.signature, chunk.text,
                ),
            )
            if embeddings is not None and self._vec_supported and self._embedding_dim is not None:
                emb = embeddings[i]
                if len(emb) != self._embedding_dim:
                    raise ValueError(
                        f"embedding dim {len(emb)} != {self._embedding_dim}"
                    )
                blob = struct.pack(f"{len(emb)}f", *emb)
                cur.execute(
                    "DELETE FROM chunks_vec WHERE chunk_id = ?",
                    (chunk.id,),
                )
                cur.execute(
                    "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk.id, blob),
                )
        self._conn.commit()

    # ── read ──

    def list_projects(self) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        rows = cur.execute("SELECT * FROM projects ORDER BY last_indexed DESC").fetchall()
        return [dict(r) for r in rows]

    def project_stats(self, root_path: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        ).fetchone()
        if not row:
            return None
        stats = dict(row)
        # Count files and chunks for this project prefix.
        prefix = root_path + "/"
        stats["indexed_files"] = cur.execute(
            "SELECT COUNT(*) FROM files WHERE relpath LIKE ?", (prefix,)
        ).fetchone()[0]
        stats["indexed_chunks"] = cur.execute(
            "SELECT COUNT(*) FROM chunks WHERE relpath LIKE ?", (prefix,)
        ).fetchone()[0]
        return stats

    def search_semantic(
        self,
        embedding: list[float],
        *,
        k: int = 10,
        relpath_prefix: str | None = None,
        symbol_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """KNN search via sqlite-vec. Falls back to empty list if vec unavailable."""
        if not self._vec_supported or self._embedding_dim is None:
            return []
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        cur = self._conn.cursor()
        sql = (
            "SELECT c.*, v.distance "
            "FROM chunks_vec AS v "
            "JOIN chunks AS c ON c.id = v.chunk_id "
            "WHERE v.embedding MATCH ? AND k = ?"
        )
        params: list[Any] = [blob, k]
        if relpath_prefix:
            sql += " AND c.relpath LIKE ?"
            params.append(relpath_prefix + "%")
        if symbol_kind:
            sql += " AND c.symbol_kind = ?"
            params.append(symbol_kind)
        sql += " ORDER BY v.distance"
        rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_text(
        self,
        query: str,
        *,
        k: int = 10,
        relpath_prefix: str | None = None,
        symbol_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5 keyword search."""
        cur = self._conn.cursor()
        # Escape FTS5 special chars.
        safe_query = query.replace('"', '""')
        sql = (
            "SELECT c.*, rank "
            "FROM chunks_fts AS f "
            "JOIN chunks AS c ON c.rowid = f.rowid "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [safe_query]
        if relpath_prefix:
            sql += " AND c.relpath LIKE ?"
            params.append(relpath_prefix + "%")
        if symbol_kind:
            sql += " AND c.symbol_kind = ?"
            params.append(symbol_kind)
        sql += " ORDER BY rank LIMIT ?"
        params.append(k)
        rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_symbol(self, name: str, *, relpath_prefix: str | None = None) -> list[dict[str, Any]]:
        """Exact / prefix match on symbol_name."""
        cur = self._conn.cursor()
        sql = "SELECT * FROM chunks WHERE symbol_name LIKE ?"
        params: list[Any] = [f"%{name}%"]
        if relpath_prefix:
            sql += " AND relpath LIKE ?"
            params.append(relpath_prefix + "%")
        rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
