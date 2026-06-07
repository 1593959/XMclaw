"""Code chunking — split source files into indexable units.

Dual strategy per file (inspired by codebase-indexer and the upstream agent):

1. **Symbol-aware**: if ``extract_symbols`` covers ≥ 50 % of the file's
   non-blank lines, emit one chunk per top-level symbol (function / class /
   interface). Each chunk includes the symbol body plus a small header with
   file path and signature.

2. **Sliding-window fallback**: for files where regex/AST doesn't find
   enough structure (configs, markdown, small scripts), fall back to
   character-based windows with overlap.

Every chunk carries metadata so the indexer can reconstruct provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from xmclaw.cognition.codebase_index.symbols import Symbol, extract_symbols


ChunkType = Literal["symbol", "fallback", "header"]


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str                      # deterministic: "{relpath}:{start}:{end}"
    relpath: str
    start_line: int              # 1-based, inclusive
    end_line: int                # 1-based, inclusive
    text: str
    chunk_type: ChunkType
    symbol_name: str | None
    symbol_kind: str | None
    signature: str | None


# Fallback window parameters.
_FALLBACK_SIZE = 1500          # characters
_FALLBACK_OVERLAP = 200        # characters
_MAX_FILE_SIZE = 1024 * 1024   # 1 MB — skip files larger than this


def _count_non_blank_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def _symbol_coverage(symbols: list[Symbol], total_non_blank: int) -> float:
    if total_non_blank == 0:
        return 0.0
    covered = sum(
        max(0, s.end_line - s.start_line + 1)
        for s in symbols
        if s.kind not in {"module", "unknown"}
    )
    return min(covered / total_non_blank, 1.0)


def _make_symbol_chunks(text: str, relpath: str, symbols: list[Symbol]) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    for sym in symbols:
        if sym.kind == "module":
            continue
        start = max(1, sym.start_line)
        end = min(len(lines), sym.end_line)
        body_lines = lines[start - 1:end]
        if not body_lines:
            continue
        # Prefix with a one-line header for embedding context.
        header = f"# {relpath}:{sym.start_line} {sym.kind} {sym.name}"
        if sym.signature:
            header += f"\n{sym.signature}"
        chunk_text = header + "\n" + "\n".join(body_lines)
        chunks.append(Chunk(
            id=f"{relpath}:{start}:{end}",
            relpath=relpath,
            start_line=start,
            end_line=end,
            text=chunk_text,
            chunk_type="symbol",
            symbol_name=sym.name,
            symbol_kind=sym.kind,
            signature=sym.signature,
        ))
    return chunks


def _make_fallback_chunks(text: str, relpath: str) -> list[Chunk]:
    """Sliding-window fallback for unstructured files."""
    chunks: list[Chunk] = []
    pos = 0
    lines = text.splitlines()
    # Map character offset → line number for boundary recovery.
    offset_to_line: list[int] = []
    off = 0
    for i, line in enumerate(lines, start=1):
        for _ in line + "\n":
            offset_to_line.append(i)

    while pos < len(text):
        window = text[pos:pos + _FALLBACK_SIZE]
        if not window.strip():
            break
        start_line = offset_to_line[pos] if pos < len(offset_to_line) else 1
        end_off = min(pos + len(window) - 1, len(offset_to_line) - 1)
        end_line = offset_to_line[end_off] if end_off >= 0 else start_line
        chunks.append(Chunk(
            id=f"{relpath}:{start_line}:{end_line}",
            relpath=relpath,
            start_line=start_line,
            end_line=end_line,
            text=window,
            chunk_type="fallback",
            symbol_name=None,
            symbol_kind=None,
            signature=None,
        ))
        # Advance with overlap.
        advance = max(len(window) - _FALLBACK_OVERLAP, _FALLBACK_OVERLAP)
        pos += advance
        if advance <= 0:
            break
    return chunks


def chunk_file(text: str, relpath: str, path: Path) -> list[Chunk]:
    """Split *text* into :class:`Chunk` objects.

    Parameters
    ----------
    text : str
        Full file contents (UTF-8 decoded).
    relpath : str
        Relative path inside the project (POSIX separators).
    path : Path
        Absolute path (used for language inference).

    Returns
    -------
    list[Chunk]
        Non-empty chunks only. Empty list if file is too large or unreadable.
    """
    if len(text.encode("utf-8")) > _MAX_FILE_SIZE:
        return []

    symbols = extract_symbols(text, path)
    total_non_blank = _count_non_blank_lines(text)
    coverage = _symbol_coverage(symbols, total_non_blank)

    if coverage >= 0.5 and any(s.kind != "module" for s in symbols):
        return _make_symbol_chunks(text, relpath, symbols)
    else:
        return _make_fallback_chunks(text, relpath)
