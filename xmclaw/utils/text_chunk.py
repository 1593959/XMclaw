"""Markdown chunker — line-based split with overlap, per CoPaw/ReMe.

B-41. Splits markdown text into overlapping line ranges sized for
embedding models. Each chunk carries its line range so the indexer
can do append-only re-indexing (only re-embed lines past the last
indexed cutoff) and so the agent's search results can cite the
exact ``path:start-end`` location.

Heuristic: ~``chunk_chars`` chars per chunk (default 1200, roughly
matches ``chunk_tokens=300`` at the chars/4 estimate that the rest
of XMclaw uses), with ``overlap_lines`` lines of overlap so
sentences aren't split across chunk boundaries. Heading boundaries
(``#`` lines) are soft preferences — when a chunk would otherwise
split a section in the middle, we extend slightly to land on the
next ``#`` line if it's nearby.

Returned ``MarkdownChunk`` carries:
* ``start_line`` / ``end_line`` — 1-indexed, inclusive of start
  and end. Same convention as ``file_read`` tool.
* ``text`` — the concatenated chunk body.
* ``hash`` — blake2s of normalised chunk text. The indexer uses
  this to skip re-embedding when content didn't change.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownChunk:
    start_line: int   # 1-indexed inclusive
    end_line: int     # 1-indexed inclusive
    text: str
    hash: str


_DEFAULT_CHUNK_CHARS = 1200
_DEFAULT_OVERLAP_LINES = 2


def _hash_text(text: str) -> str:
    """Stable short hash for chunk content. Normalised: trim trailing
    whitespace per line + collapse blank-line runs so a save that
    only added empty lines doesn't trigger a re-embed."""
    norm_lines: list[str] = []
    blank_run = 0
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        norm_lines.append(s)
    norm = "\n".join(norm_lines).strip()
    return hashlib.blake2s(norm.encode("utf-8"), digest_size=12).hexdigest()


def _is_h2_or_h3(line: str) -> bool:
    """``## `` or ``### `` heading line — natural section boundary."""
    s = line.lstrip()
    return s.startswith("## ") or s.startswith("### ")


def chunk_markdown(
    text: str,
    *,
    chunk_chars: int = _DEFAULT_CHUNK_CHARS,
    overlap_lines: int = _DEFAULT_OVERLAP_LINES,
) -> list[MarkdownChunk]:
    """Split ``text`` into a list of overlapping line-range chunks.

    Two boundary triggers, whichever fires first:

    1. Size — adding the current line would push the chunk over
       ``chunk_chars``. Standard CoPaw / ReMe behaviour.
    2. Section heading — a fresh ``## `` or ``### `` line starts
       a new logical section. We force a split here even when
       size-budget hasn't been hit. Without this, a small file
       (e.g. 800-char MEMORY.md with ## 用户偏好 + ## 项目状态)
       fits in one chunk → every query lands the same hit. Section
       splitting gives the agent's vector search semantic
       granularity even on small files.

    The flush-and-overlap mechanic is identical whichever way the
    boundary triggered.

    Empty / whitespace-only input yields an empty list.
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []

    chunks: list[MarkdownChunk] = []
    cur_start = 0
    cur_chars = 0
    chunk_lines: list[str] = []

    def _flush(start_idx: int, end_idx: int) -> None:
        body = "\n".join(lines[start_idx:end_idx]).strip()
        if not body:
            return
        chunks.append(MarkdownChunk(
            start_line=start_idx + 1,
            end_line=end_idx,
            text=body,
            hash=_hash_text(body),
        ))

    i = 0
    while i < n:
        line = lines[i]
        line_len = len(line) + 1

        # Section-boundary trigger: a heading line AFTER we already
        # have body content starts a fresh chunk. Skip when chunk
        # is empty (the heading itself opens the next chunk).
        section_break = (
            _is_h2_or_h3(line)
            and chunk_lines
            and any(ln.strip() for ln in chunk_lines)
        )

        # Size trigger: adding this line would overflow.
        size_break = cur_chars + line_len > chunk_chars and chunk_lines

        if section_break or size_break:
            _flush(cur_start, i)
            # On section breaks we DON'T overlap — section boundaries
            # are deliberate semantic cuts. On size breaks we keep
            # ``overlap_lines`` of overlap so sentences aren't split.
            if section_break:
                cur_start = i
                cur_chars = 0
                chunk_lines = []
            else:
                cur_start = max(0, i - overlap_lines)
                cur_chars = sum(len(lines[k]) + 1 for k in range(cur_start, i))
                chunk_lines = lines[cur_start:i]

        chunk_lines.append(line)
        cur_chars += line_len
        i += 1

    if chunk_lines:
        _flush(cur_start, n)

    return chunks


__all__ = ["MarkdownChunk", "chunk_markdown"]
