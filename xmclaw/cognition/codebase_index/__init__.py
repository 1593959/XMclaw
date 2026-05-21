"""CodebaseIndex — semantic symbol-aware indexing for local projects.

Public API
----------
:func:`scan` — discover source files (git-aware).
:func:`chunk_file` — split a file into indexable chunks.
:func:`extract_symbols` — language-aware symbol extraction.
:class:`CodebaseStore` — SQLite + sqlite-vec + FTS5 persistence.
:class:`CodebaseIndexer` — orchestrate scan → chunk → embed → store.
:class:`CodebaseToolProvider` — expose ``codebase_search`` / ``codebase_conventions`` tools.
"""
from __future__ import annotations

from xmclaw.cognition.codebase_index.chunker import Chunk, chunk_file
from xmclaw.cognition.codebase_index.indexer import CodebaseIndexer
from xmclaw.cognition.codebase_index.scanner import FileEntry, scan
from xmclaw.cognition.codebase_index.store import CodebaseStore
from xmclaw.cognition.codebase_index.symbols import Symbol, extract_symbols
from xmclaw.cognition.codebase_index.tools import CodebaseToolProvider

__all__ = [
    "Chunk",
    "chunk_file",
    "CodebaseIndexer",
    "CodebaseStore",
    "CodebaseToolProvider",
    "extract_symbols",
    "FileEntry",
    "scan",
    "Symbol",
]
