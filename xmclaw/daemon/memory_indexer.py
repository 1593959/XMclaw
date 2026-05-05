"""MemoryFileIndexer — auto-index persona files into the vector store.

B-41 (CoPaw parity). XMclaw had a vector backend (``SqliteVecMemory``)
and storage tables, but nothing was actually pumping content INTO the
vector index — semantic search silently degraded to keyword scan.
CoPaw / QwenPaw solves this by file-watching ``MEMORY.md`` +
``memory/*.md`` and incrementally embedding chunks. This indexer is
the XMclaw counterpart, minus the ``reme-ai`` black-box dependency.

What it watches (B-43: unified across all memory roots)::

    <persona_dir>/MEMORY.md
    <persona_dir>/USER.md
    <persona_dir>/memory/*.md      # daily episodic logs (B-40)
    <file_memory_dir>/*.md         # web UI memory editor notes
    <file_memory_dir>/journal/*.md # journal entries

How it works:

1. Every ``poll_interval_s`` seconds, ``tick()`` walks the watched
   paths and compares each file's mtime against the last-indexed
   mtime cached in-memory.
2. On change: re-chunk the file via :func:`chunk_markdown`, fetch
   existing chunks for this path from SqliteVecMemory by metadata
   filter, diff by ``hash``:
     • new (start_line + content not seen) → embed + insert
     • deleted (was in DB but not in the new chunk list) → forget
     • unchanged → skip (no embed cost)
3. On file delete: drop every chunk whose ``source_path`` equals
   the missing file.

The indexer is a no-op (with a single warning log) when no
EmbeddingProvider is configured — keeps fresh installs working
without forcing the user to provide an embedding key just to run
the daemon.

Schema convention written into ``MemoryItem.metadata``::

    {
      "kind": "file_chunk",
      "source_path": "/abs/path/MEMORY.md",
      "start_line": 5, "end_line": 18,
      "chunk_hash": "abc123..."
    }

The deterministic id is ``blake2s(source_path + ":" + start_line)``
so a file re-saved with identical first chunk doesn't create a
duplicate row.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmclaw.utils.log import get_logger
from xmclaw.utils.text_chunk import MarkdownChunk, chunk_code, chunk_markdown


# B-210: workspace code-indexing scope. The persona/journal indexer
# only needs to skip ``journal/`` (handled inline). Code workspaces
# are noisier — vendored libs, build outputs, lockfiles. Hard-coded
# denylist + extension allowlist keeps the index focused on
# user-authored source.
_CODE_DIR_DENYLIST = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox",
    ".venv", "venv", "env",
    "dist", "build", "target", "out",
    ".next", ".nuxt", ".cache", ".parcel-cache",
    "coverage", ".coverage", "htmlcov",
    ".idea", ".vscode",
    # XMclaw-specific scratch
    ".claude",
})

_CODE_FILE_EXTENSIONS = frozenset({
    # Python
    ".py", ".pyi",
    # JS / TS
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Web
    ".html", ".css", ".scss",
    # Systems
    ".rs", ".go", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    # JVM
    ".java", ".kt", ".scala",
    # Other common
    ".rb", ".php", ".swift", ".lua", ".sh", ".ps1",
    # Markdown / config (often part of code repos and useful for recall)
    ".md", ".rst", ".toml", ".yaml", ".yml", ".json",
    # SQL / queries
    ".sql",
})

# Per-file size cap. Above this we skip — embedding a 1MB minified
# JS bundle just wastes the API call and pollutes recall.
_CODE_FILE_MAX_BYTES = 256 * 1024


def _is_code_path_allowed(path: Path) -> bool:
    """B-210: filter for ``_iter_workspace_files``. False ⇒ skip."""
    if path.suffix.lower() not in _CODE_FILE_EXTENSIONS:
        return False
    # Any parent directory in the denylist disqualifies.
    for part in path.parts:
        if part in _CODE_DIR_DENYLIST:
            return False
    try:
        if path.stat().st_size > _CODE_FILE_MAX_BYTES:
            return False
    except OSError:
        return False
    return True


def _iter_workspace_files(roots: list[Path]):
    """B-210: yield code files under the configured workspace roots."""
    seen: set[Path] = set()
    for root in roots:
        if not root or not root.is_dir():
            continue
        for entry in root.rglob("*"):
            if not entry.is_file():
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            if not _is_code_path_allowed(resolved):
                continue
            seen.add(resolved)
            yield resolved

if TYPE_CHECKING:
    from xmclaw.providers.memory.embedding import EmbeddingProvider
    from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


_log = get_logger(__name__)


def _chunk_id(source_path: str, start_line: int) -> str:
    return hashlib.blake2s(
        f"{source_path}:{start_line}".encode("utf-8"),
        digest_size=12,
    ).hexdigest()


class MemoryFileIndexer:
    """Background service that keeps the vector index in sync with
    the persona file tree.

    Construct once per daemon, ``start()`` from the lifespan, ``stop()``
    on shutdown. Manual one-shot ``tick()`` calls are also OK (used
    in tests).
    """

    def __init__(
        self,
        *,
        persona_dir_provider,                    # callable () -> Path
        sqlite_vec: "SqliteVecMemory",
        embedder: "EmbeddingProvider",
        poll_interval_s: float = 10.0,
        layer: str = "long",
        bus: "Any | None" = None,
        # B-210: optional workspace code paths. When non-empty, the
        # indexer also walks these dirs every tick and chunks any
        # source file (allowed extension + not in denylist) into the
        # vector store as ``kind=code_chunk``. memory_search can
        # filter on this kind to do code-aware recall; the auto-
        # injected ``<memory-context>`` block skips it by default
        # (would otherwise drown persona facts in code).
        workspace_paths: list[str] | None = None,
    ) -> None:
        self._persona_dir_provider = persona_dir_provider
        self._vec = sqlite_vec
        self._embedder = embedder
        self._poll_s = max(1.0, float(poll_interval_s))
        self._layer = layer
        # B-210: resolve workspace_paths into Path objects once;
        # filter out paths that don't exist so we don't spam
        # warnings every tick.
        self._workspace_roots: list[Path] = []
        for raw in (workspace_paths or []):
            try:
                p = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if p.is_dir():
                self._workspace_roots.append(p)
            else:
                _log.warning(
                    "memory_indexer.workspace_path_missing path=%s "
                    "(skipped — create the directory or remove from config)",
                    raw,
                )
        # Per-file mtime cache so we skip unchanged files.
        self._mtime_cache: dict[str, float] = {}
        # In-memory set of paths we've indexed at least once. After
        # restart it's empty; first tick will re-scan everything but
        # the chunk-hash diff makes that a no-op (no embeddings
        # actually called).
        self._known_paths: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        # B-43: optional bus for MEMORY_INDEXED events. Only emitted
        # when a tick actually changed something so quiet polling
        # doesn't flood the Trace page.
        self._bus = bus

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="memory-indexer-loop",
        )

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                _log.warning("memory_indexer.tick_failed err=%s", exc)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_s,
                )
                return  # stopped event triggered
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """One indexing pass over every watched file. Returns a dict
        of counters for callers / tests.

        Counters: ``files_scanned``, ``files_changed``, ``chunks_added``,
        ``chunks_deleted``, ``chunks_unchanged``, ``files_removed``.

        B-43: emits a MEMORY_INDEXED bus event when the tick actually
        changed something (skipped on quiet polls).
        """
        import time as _t
        t0 = _t.perf_counter()
        counters = {
            "files_scanned": 0,
            "files_changed": 0,
            "chunks_added": 0,
            "chunks_deleted": 0,
            "chunks_unchanged": 0,
            "files_removed": 0,
        }
        watched = list(self._watched_paths())
        live_paths: set[str] = set()
        for path in watched:
            counters["files_scanned"] += 1
            try:
                stat = path.stat()
            except OSError:
                continue
            live_paths.add(str(path))
            mtime = stat.st_mtime
            cached = self._mtime_cache.get(str(path))
            if cached is not None and abs(cached - mtime) < 1e-6:
                continue  # file hasn't changed since last index
            try:
                added, deleted, unchanged = await self._index_file(path)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_indexer.file_failed path=%s err=%s", path, exc,
                )
                continue
            self._mtime_cache[str(path)] = mtime
            self._known_paths.add(str(path))
            counters["files_changed"] += 1
            counters["chunks_added"] += added
            counters["chunks_deleted"] += deleted
            counters["chunks_unchanged"] += unchanged

        # Purge chunks whose source files have disappeared.
        gone = self._known_paths - live_paths
        for missing in list(gone):
            try:
                # Try both kinds — _drop_path is idempotent and
                # the missing path could have been a code file or
                # a persona/journal file.
                removed = await self._drop_path(missing, kind="file_chunk")
                removed += await self._drop_path(missing, kind="code_chunk")
                counters["chunks_deleted"] += removed
                counters["files_removed"] += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_indexer.purge_failed path=%s err=%s",
                    missing, exc,
                )
            self._mtime_cache.pop(missing, None)
            self._known_paths.discard(missing)

        # B-43: emit MEMORY_INDEXED if we actually moved any rows.
        if (
            self._bus is not None
            and (counters["files_changed"]
                 or counters["files_removed"]
                 or counters["chunks_added"]
                 or counters["chunks_deleted"])
        ):
            try:
                from xmclaw.core.bus import EventType, make_event
                payload = dict(counters)
                payload["elapsed_ms"] = (_t.perf_counter() - t0) * 1000.0
                ev = make_event(
                    session_id="_system", agent_id="indexer",
                    type=EventType.MEMORY_INDEXED, payload=payload,
                )
                await self._bus.publish(ev)
            except Exception:  # noqa: BLE001 — telemetry never blocks
                pass
        return counters

    def _watched_paths(self):
        # 1. Persona dir — agent's identity files + B-40 daily logs.
        try:
            pdir = Path(self._persona_dir_provider())
        except Exception:  # noqa: BLE001
            pdir = None
        if pdir is not None and pdir.is_dir():
            for name in ("MEMORY.md", "USER.md"):
                p = pdir / name
                if p.is_file():
                    yield p
            log_dir = pdir / "memory"
            if log_dir.is_dir():
                for entry in sorted(log_dir.glob("*.md")):
                    if entry.is_file():
                        yield entry

        # 2. Shared file_memory_dir — Web UI's "memory editor" panel
        # writes here. B-43 unifies these into the same vector index
        # so user-authored notes are searchable alongside agent-curated
        # bullets and daily logs.
        try:
            from xmclaw.utils.paths import file_memory_dir
            fmd = file_memory_dir()
        except Exception:  # noqa: BLE001
            fmd = None
        if fmd is not None and fmd.is_dir():
            # Top-level user notes
            for entry in sorted(fmd.glob("*.md")):
                if entry.is_file():
                    yield entry
            # Journal sub-dir entries
            jdir = fmd / "journal"
            if jdir.is_dir():
                for entry in sorted(jdir.glob("*.md")):
                    if entry.is_file():
                        yield entry

        # 3. Workspace code roots (B-210). Yields source files that
        # pass the extension allowlist + denylist filter. Tagged via
        # ``_is_code_file()`` so ``_index_file`` knows to use the
        # sliding-window code chunker + ``kind=code_chunk``.
        for code_path in _iter_workspace_files(self._workspace_roots):
            yield code_path

    def _classify_path(self, path: Path) -> str:
        """B-210: 'file_chunk' for persona/journal/.md notes,
        'code_chunk' for workspace source files. The kind dimension
        becomes the source axis without needing a schema migration."""
        # Workspace roots: any file inside one of the configured
        # workspace dirs that passed the allowlist is code.
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        for root in self._workspace_roots:
            try:
                resolved.relative_to(root)
                return "code_chunk"
            except ValueError:
                continue
        return "file_chunk"

    async def _index_file(self, path: Path) -> tuple[int, int, int]:
        """Index one file. Returns (added, deleted, unchanged)."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return (0, 0, 0)

        kind = self._classify_path(path)
        if kind == "code_chunk":
            new_chunks = chunk_code(text)
        else:
            new_chunks = chunk_markdown(text)
        new_by_id: dict[str, MarkdownChunk] = {
            _chunk_id(str(path), c.start_line): c for c in new_chunks
        }

        existing = await self._existing_chunk_ids(str(path), kind=kind)
        existing_ids: set[str] = set(existing.keys())
        new_ids: set[str] = set(new_by_id.keys())

        # Plan the diff: which chunks would change vs unchanged.
        to_drop: list[str] = []
        unchanged_ids: set[str] = set()
        for cid in existing_ids:
            if cid not in new_ids:
                to_drop.append(cid)
                continue
            old_hash = existing[cid]
            if old_hash != new_by_id[cid].hash:
                to_drop.append(cid)
            else:
                unchanged_ids.add(cid)

        # Determine what needs embedding.
        to_embed: list[tuple[str, MarkdownChunk]] = [
            (cid, c) for cid, c in new_by_id.items()
            if cid not in unchanged_ids
        ]
        # B-66: previously we dropped old chunks BEFORE embedding. If
        # the embedder timed out / API down / returned empty, we'd
        # return early with the file's old chunks already deleted —
        # the file would temporarily have ZERO indexed chunks until
        # the next successful tick. Now: embed FIRST; only drop the
        # old chunks AFTER we've got the new vectors in hand. If
        # embedding fails the old chunks remain valid (slightly stale
        # vs current text, but discoverable). The next tick retries.
        vectors: list[list[float]] = []
        if to_embed:
            try:
                vectors = await self._embedder.embed(
                    [c.text for _, c in to_embed]
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_indexer.embed_failed path=%s err=%s "
                    "(keeping old chunks, will retry next tick)",
                    path, exc,
                )
                return (0, 0, len(unchanged_ids))
            # Provider can return per-row empties for partial failure.
            # If we got nothing usable, keep the old chunks too.
            if not any(vectors):
                return (0, 0, len(unchanged_ids))

        # Now safe to drop superseded chunks — we have a working
        # replacement for each (or the candidate had no embedding,
        # in which case its old chunk lingering is the lesser evil).
        for cid in to_drop:
            await self._vec.forget(cid)

        from xmclaw.providers.memory.base import MemoryItem
        added = 0
        for (cid, chunk), vec in zip(to_embed, vectors):
            if not vec:
                continue  # provider failed for this entry; skip silently
            await self._vec.put(
                self._layer,
                MemoryItem(
                    id=cid,
                    layer=self._layer,  # type: ignore[arg-type]
                    text=chunk.text,
                    metadata={
                        "kind": kind,
                        "source_path": str(path),
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "chunk_hash": chunk.hash,
                        "provider": "indexer",
                    },
                    embedding=tuple(vec),
                    ts=os.path.getmtime(path),
                ),
            )
            added += 1
        return (added, len(to_drop), len(unchanged_ids))

    async def _existing_chunk_ids(
        self, source_path: str, *, kind: str = "file_chunk",
    ) -> dict[str, str]:
        """Return ``{chunk_id: chunk_hash}`` for chunks whose
        ``metadata.source_path`` matches ``source_path``. ``kind``
        defaults to ``file_chunk`` (persona/journal); B-210 passes
        ``code_chunk`` for workspace files so the diff doesn't get
        confused if the same path is somehow indexed under both."""
        rows = await self._vec.query(
            self._layer,  # type: ignore[arg-type]
            text=None,
            k=10000,  # effectively all chunks for this file
            filters={"source_path": source_path, "kind": kind},
        )
        out: dict[str, str] = {}
        for r in rows:
            chunk_hash = (r.metadata or {}).get("chunk_hash")
            if isinstance(chunk_hash, str):
                out[r.id] = chunk_hash
        return out

    async def _drop_path(self, source_path: str, *, kind: str = "file_chunk") -> int:
        """Delete every chunk whose ``source_path`` equals the given
        path (used when the source file is gone). Returns the count."""
        existing = await self._existing_chunk_ids(source_path, kind=kind)
        for cid in existing:
            await self._vec.forget(cid)
        return len(existing)
