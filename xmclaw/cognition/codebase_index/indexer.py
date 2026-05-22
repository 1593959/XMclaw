"""CodebaseIndexer — orchestrate scan, chunk, embed, store, convention extraction.

Usage::

    from xmclaw.cognition.codebase_index import CodebaseIndexer
    from xmclaw.providers.memory.embedding import build_embedding_provider

    embedder = build_embedding_provider(cfg)
    from xmclaw.utils.paths import v2_workspace_dir
    indexer = CodebaseIndexer(
        store_path=v2_workspace_dir() / "codebase" / "index.db",
        embedder=embedder,
    )
    await indexer.index_project(Path("/path/to/repo"))
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from xmclaw.cognition.codebase_index.chunker import Chunk, chunk_file
from xmclaw.cognition.codebase_index.conventions import extract_conventions, render_conventions
from xmclaw.cognition.codebase_index.scanner import FileEntry, scan
from xmclaw.cognition.codebase_index.store import CodebaseStore
from xmclaw.providers.memory.embedding import EmbeddingProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# Batch size for embedding calls.
_EMBED_BATCH = 32


class CodebaseIndexer:
    """High-level indexer for one or more projects.

    Parameters
    ----------
    store_path : Path
        Path to the SQLite store.
    embedder : EmbeddingProvider | None
        When ``None``, indexing proceeds text-only; semantic search
        falls back to FTS5.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._store = CodebaseStore(
            store_path,
            embedding_dim=embedder.dim if embedder else None,
        )
        self._embedder = embedder

    @property
    def store(self) -> CodebaseStore:
        return self._store

    async def index_project(
        self,
        root: Path | str,
        *,
        name: str | None = None,
        force_full: bool = False,
    ) -> dict[str, Any]:
        """Index (or incrementally update) a project.

        Returns a summary dict with ``indexed_files``, ``indexed_chunks``,
        ``skipped_files``, ``elapsed_seconds``.
        """
        root = Path(root).resolve()
        root_str = str(root)
        project_name = name or root.name

        start = time.monotonic()
        files = scan(root)
        if not files:
            return {"indexed_files": 0, "indexed_chunks": 0, "skipped_files": 0, "elapsed_seconds": 0.0}

        indexed_files = 0
        indexed_chunks = 0
        skipped_files = 0

        for entry in files:
            try:
                n_chunks = await self._index_file(entry, root_str, force_full=force_full)
                if n_chunks:
                    indexed_files += 1
                    indexed_chunks += n_chunks
                else:
                    skipped_files += 1
            except Exception as exc:
                _log.warning("index_file_failed: %s — %s", entry.relpath, exc)
                skipped_files += 1

        # Extract conventions after all files are indexed.
        try:
            conv = extract_conventions(self._store, root_str)
            self._write_conventions(root, conv)
        except Exception as exc:
            _log.warning("convention_extraction_failed: %s — %s", root_str, exc)

        self._store.upsert_project(root_str, project_name, len(files))

        elapsed = time.monotonic() - start
        _log.info(
            "codebase.indexed: root=%s files=%d chunks=%d skipped=%d elapsed=%.2fs",
            root_str, indexed_files, indexed_chunks, skipped_files, elapsed,
        )
        return {
            "indexed_files": indexed_files,
            "indexed_chunks": indexed_chunks,
            "skipped_files": skipped_files,
            "elapsed_seconds": elapsed,
        }

    async def _index_file(self, entry: FileEntry, root_str: str, *, force_full: bool) -> int:
        """Index a single file. Returns number of chunks created (0 = unchanged)."""
        relpath = f"{root_str}/{entry.relpath}"

        if not force_full:
            old_hash = self._store.file_hash(relpath)
            if old_hash is not None:
                # Quick mtime check before reading file.
                stored_mtime = self._store._conn.execute(
                    "SELECT mtime FROM files WHERE relpath = ?", (relpath,)
                ).fetchone()
                if stored_mtime and stored_mtime["mtime"] >= entry.mtime:
                    return 0  # unchanged

        try:
            text = entry.path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            _log.debug("read_failed: %s — %s", entry.relpath, exc)
            return 0

        # Skip binary / mostly non-text.
        if "\x00" in text:
            return 0

        chunks = chunk_file(text, relpath, entry.path)
        if not chunks:
            return 0

        # Delete old chunks for this file before inserting new ones.
        self._store.delete_file_chunks(relpath)
        self._store.upsert_file(relpath, entry.size, entry.mtime, text)

        # Compute embeddings in batches.
        embeddings: list[list[float]] | None = None
        if self._embedder is not None and self._embedder.is_available():
            texts = [c.text for c in chunks]
            embeddings = []
            for i in range(0, len(texts), _EMBED_BATCH):
                batch = texts[i : i + _EMBED_BATCH]
                try:
                    batch_embs = await self._embedder.embed(batch)
                    embeddings.extend(batch_embs)
                except Exception as exc:
                    _log.warning("embed_batch_failed: %s — %s", entry.relpath, exc)
                    # Fill remaining with zeros so we still store text.
                    zero = [0.0] * self._embedder.dim
                    embeddings.extend([zero] * len(batch))

        self._store.insert_chunks(chunks, embeddings)
        return len(chunks)

    def _write_conventions(self, root: Path, conv: Any) -> None:
        """Write conventions.md next to the project root (inside XMclaw workspace)."""
        from xmclaw.utils.paths import data_dir
        conventions_dir = data_dir() / "v2" / "codebase" / "conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)
        safe_name = root.name.replace(" ", "_")
        out_path = conventions_dir / f"{safe_name}.md"
        out_path.write_text(render_conventions(conv), encoding="utf-8")

    async def delete_project(self, root: Path | str) -> None:
        """Remove all index data for a project."""
        root_str = str(Path(root).resolve())
        self._store.delete_project(root_str)
        _log.info("codebase.deleted: %s", root_str)

    def close(self) -> None:
        self._store.close()
