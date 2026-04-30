"""B-93: scan ``~/.xmclaw/memory/*.md`` and surface a per-file
header (description + tags) for the LLM-pick top-K relevant-memories
flow (free-code memdir parity).

Two header formats are supported:

1. **YAML frontmatter** at the top of the file::

       ---
       description: Build pipeline notes — Bun + Vite quirks
       tags: [build, frontend, deps]
       ---

       (markdown body…)

   The agent's :func:`note_write` tool writes this shape on save (B-93
   adds the optional ``description`` / ``tags`` parameters).

2. **Header-less files** — fall back to the first 200 chars of body
   as a synthetic description. This means existing user notes work
   immediately without the user having to retrofit headers.

Output shape mirrors free-code's ``MemoryHeader``: filename + path +
description + tags + mtime + size. The relevant-picker takes a list of
these and renders a manifest the LLM picks from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Cap fallback description length. Long enough to capture the topic
# of a typical note (first sentence or two), short enough that a
# 100-file memory dir manifest stays under ~20 KB.
_FALLBACK_DESCRIPTION_CHARS = 200

# YAML-style frontmatter delimited by '---' on its own line. We only
# care about ``description`` and ``tags`` — anything else passes
# through unread. No PyYAML dependency: we hand-parse a minimal
# subset (key: value, list values as ``[a, b, c]``) since note_write
# never writes anything more exotic.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class MemoryFileEntry:
    """One memory file with its index header."""

    path: Path
    name: str          # filename without .md
    description: str   # from frontmatter OR first 200 chars
    tags: tuple[str, ...] = ()
    mtime: float = 0.0
    size: int = 0


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Extract leading ``---`` frontmatter and return (fields, body).

    On no match returns ``({}, text)``. Best-effort key/value parser:
    a line ``foo: bar`` becomes ``{"foo": "bar"}``; ``foo: [a, b]``
    becomes ``{"foo": ["a", "b"]}``. Anything that doesn't look like
    a recognised shape is skipped silently — we never want one weird
    line to break note loading.
    """
    if not text:
        return ({}, "")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ({}, text)
    block = m.group(1)
    body = text[m.end():]
    fields: dict[str, str | list[str]] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # List shape: ``[a, b, c]`` or ``[a]``
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                fields[key] = []
            else:
                fields[key] = [
                    p.strip().strip("'\"")
                    for p in inner.split(",")
                    if p.strip()
                ]
            continue
        # Quoted scalar
        if (val.startswith("\"") and val.endswith("\"")) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        fields[key] = val
    return (fields, body)


def _fallback_description(body: str) -> str:
    """First ~200 chars of body, normalised to a single line.

    Used when a file has no frontmatter description — most existing
    user notes will fall through this path until they're rewritten
    with metadata. Strips leading ``# heading`` lines so the
    description doesn't just say "# Title" with no real content.
    """
    # Drop leading blank lines and Markdown headings until we hit
    # a content line.
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Keep heading text WITHOUT the #'s — it's often the most
            # informative one-liner in a note.
            stripped = line.lstrip("#").strip()
            if stripped:
                return stripped[:_FALLBACK_DESCRIPTION_CHARS]
            continue
        break
    flat = " ".join(body.split())
    return flat[:_FALLBACK_DESCRIPTION_CHARS]


def scan_memory_files(memory_dir: Path) -> list[MemoryFileEntry]:
    """Return a MemoryFileEntry per ``*.md`` in ``memory_dir``.

    Skips anything inside a ``journal/`` subdir — daily journal
    entries are huge, time-stamped, and the LLM-picker would just
    see a wall of "2026-04-29.md / 2026-04-30.md / …" with no
    meaningful description signal. They're served by the dedicated
    journal recall path instead.

    Best-effort throughout: a corrupt file (unreadable, weird
    encoding) is skipped, not raised.
    """
    out: list[MemoryFileEntry] = []
    if not memory_dir.is_dir():
        return out
    for entry in sorted(memory_dir.glob("*.md")):
        if not entry.is_file():
            continue
        if "journal" in entry.parts:
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
            stat = entry.stat()
        except OSError:
            continue
        fields, body = parse_frontmatter(text)
        desc_field = fields.get("description")
        description = (
            str(desc_field).strip() if isinstance(desc_field, str) else ""
        )
        if not description:
            description = _fallback_description(body or text)
        tags_field = fields.get("tags")
        if isinstance(tags_field, list):
            tags = tuple(str(t) for t in tags_field if t)
        elif isinstance(tags_field, str) and tags_field.strip():
            # Allow inline ``tags: foo, bar`` (no brackets).
            tags = tuple(t.strip() for t in tags_field.split(",") if t.strip())
        else:
            tags = ()
        out.append(MemoryFileEntry(
            path=entry,
            name=entry.stem,
            description=description,
            tags=tags,
            mtime=stat.st_mtime,
            size=stat.st_size,
        ))
    return out


def render_manifest(entries: list[MemoryFileEntry], *, max_chars: int = 8000) -> str:
    """Format an LLM-readable manifest from a list of entries.

    One line per file: ``filename: description [tags]``. Truncates
    individual descriptions and the whole manifest so a 100-file dir
    doesn't blow the context window.
    """
    lines: list[str] = []
    used = 0
    for e in entries:
        desc = e.description.replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:197] + "…"
        tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
        line = f"- {e.name}: {desc}{tag_str}"
        if used + len(line) > max_chars and lines:
            lines.append(f"… ({len(entries) - len(lines)} more files truncated)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


__all__ = [
    "MemoryFileEntry",
    "parse_frontmatter",
    "scan_memory_files",
    "render_manifest",
]
