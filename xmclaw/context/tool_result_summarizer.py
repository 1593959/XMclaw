"""Tool-result semantic summarizer — P0-2 Phase 1.

Rules-driven (zero LLM latency) summarization of large tool outputs
before they enter conversation history. Complements the existing
``prune_old_tool_results`` by reducing content *size*, not just
*dropping* messages.

Background
==========

Pre-fix: ``file_read`` of a 50KB log file produces a 50KB tool-result
message that survives pruning (pruning only drops messages outside the
protected tail; messages inside the tail stay verbatim). The model
rarely needs the full 50KB — the last 500 lines + error patterns are
usually enough.

This module provides per-tool-type summarization strategies that
preserve signal while discarding noise. All strategies are heuristic /
pattern-based — no LLM call, so zero added latency.
"""
from __future__ import annotations

import html
import re

# Thresholds — outputs shorter than this pass through unchanged.
_MIN_SUMMARIZE_LEN = 2000

# file_read / web_fetch: keep head + tail, collapse middle.
_CONTENT_HEAD_CHARS = 2000
_CONTENT_TAIL_CHARS = 500

# bash: max lines to keep from stdout.
_BASH_MAX_LINES = 30

# grep / sqlite_query: max matches to keep.
_QUERY_MAX_MATCHES = 20

# HTML tags to strip from web_fetch output.
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.S)


def _count_leading_lines(text: str, max_lines: int) -> int:
    """Return the character index after ``max_lines`` lines."""
    lines = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            lines += 1
            if lines >= max_lines:
                return i + 1
    return len(text)


def _head_tail(text: str, head: int, tail: int) -> str:
    """Return ``text`` with middle collapsed if longer than head+tail."""
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n\n...[truncated: " + str(len(text) - head - tail) + " chars]...\n\n" + text[-tail:]


def summarize_tool_result(
    tool_name: str,
    raw_output: str,
    user_query: str = "",
) -> str:
    """Summarize a tool result. Returns a shorter string or the original.

    Args:
        tool_name: canonical tool name (e.g. ``"file_read"``).
        raw_output: the tool's result content (may be HTML, JSON, plain
            text, or shell output).
        user_query: the user's current message — used for relevance hints
            in future LLM-based summarizers (currently unused in the
            heuristic path).

    Returns:
        The summarized output, or ``raw_output`` unchanged when it is
        already short or the tool type doesn't need summarization.
    """
    if not raw_output or len(raw_output) < _MIN_SUMMARIZE_LEN:
        return raw_output

    # Dispatch by tool name.
    name = (tool_name or "").lower()

    if name in ("file_read", "read_file"):
        return _summarize_file_read(raw_output)

    if name in ("list_dir", "dir_list"):
        return _summarize_list_dir(raw_output)

    if name == "bash":
        return _summarize_bash(raw_output)

    if name in ("web_fetch", "fetch_url", "web_get"):
        return _summarize_web_fetch(raw_output)

    if name in ("grep_files", "grep", "search_files"):
        return _summarize_grep(raw_output)

    if name in ("sqlite_query", "db_query", "sql_query"):
        return _summarize_sqlite_query(raw_output)

    if name in ("screen_ocr", "ocr", "image_ocr"):
        # OCR text is usually already compact; only truncate if huge.
        return _head_tail(raw_output, _CONTENT_HEAD_CHARS, _CONTENT_TAIL_CHARS)

    # Default: head+tail truncation for any unknown large output.
    return _head_tail(raw_output, _CONTENT_HEAD_CHARS, _CONTENT_TAIL_CHARS)


def _summarize_file_read(text: str) -> str:
    """Preserve head + tail; code files keep first N lines + last N lines."""
    # If it looks like code (has def/class/import), keep more structure.
    code_indicators = ("def ", "class ", "import ", "const ", "function ")
    is_code = any(ind in text[:2000] for ind in code_indicators)
    if is_code:
        head_lines = 50
        tail_lines = 20
        head_idx = _count_leading_lines(text, head_lines)
        tail_start = text.rfind("\n", 0, len(text) - _count_leading_lines(text[::-1], tail_lines))
        if tail_start < 0:
            tail_start = len(text) - _CONTENT_TAIL_CHARS
        if head_idx + (len(text) - tail_start) < len(text):
            return (
                text[:head_idx]
                + f"\n\n...[truncated: {len(text) - head_idx - (len(text) - tail_start)} chars]...\n\n"
                + text[tail_start:]
            )
    return _head_tail(text, _CONTENT_HEAD_CHARS, _CONTENT_TAIL_CHARS)


def _summarize_list_dir(text: str) -> str:
    """Directory listings: keep first 100 entries, count the rest."""
    lines = text.splitlines()
    if len(lines) <= 100:
        return text
    kept = lines[:100]
    return "\n".join(kept) + f"\n\n...[and {len(lines) - 100} more entries]..."


def _summarize_bash(text: str) -> str:
    """Shell output: keep stdout head + stderr intact."""
    # Try to separate stdout / stderr if the output has markers.
    stderr_marker = "stderr:"
    stderr_idx = text.lower().rfind(stderr_marker)
    if stderr_idx > 0 and len(text) - stderr_idx < 2000:
        # stderr is small and at the end — keep it fully, truncate stdout.
        stdout = text[:stderr_idx]
        return _head_tail(stdout, _CONTENT_HEAD_CHARS, 200) + text[stderr_idx:]

    # No clear separation — truncate by lines.
    lines = text.splitlines()
    if len(lines) <= _BASH_MAX_LINES:
        return text
    kept = lines[:_BASH_MAX_LINES]
    return "\n".join(kept) + f"\n\n...[and {len(lines) - _BASH_MAX_LINES} more lines]..."


def _summarize_web_fetch(text: str) -> str:
    """Web pages: strip HTML tags, keep text head+tail."""
    # If it's clearly HTML, strip tags.
    if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
        text = _HTML_TAG_RE.sub(" ", text)
        text = html.unescape(text)
        # Collapse whitespace.
        text = re.sub(r"\s+", " ", text)
        text = text.strip()
    return _head_tail(text, _CONTENT_HEAD_CHARS, _CONTENT_TAIL_CHARS)


def _summarize_grep(text: str) -> str:
    """Grep results: keep first N matches, count total."""
    lines = text.splitlines()
    if len(lines) <= _QUERY_MAX_MATCHES:
        return text
    kept = lines[:_QUERY_MAX_MATCHES]
    return "\n".join(kept) + f"\n\n...[and {len(lines) - _QUERY_MAX_MATCHES} more matches]..."


def _summarize_sqlite_query(text: str) -> str:
    """SQL results: keep first N rows, count total."""
    lines = text.splitlines()
    if len(lines) <= _QUERY_MAX_MATCHES + 2:  # +2 for header / separator
        return text
    # Try to find the separator line (----+----) and keep header.
    sep_idx = -1
    for i, line in enumerate(lines[:10]):
        if "---" in line and line.strip().startswith("|"):
            sep_idx = i
            break
    header_lines = lines[:sep_idx + 1] if sep_idx >= 0 else lines[:2]
    data_lines = lines[sep_idx + 1:] if sep_idx >= 0 else lines[2:]
    kept_data = data_lines[:_QUERY_MAX_MATCHES]
    return "\n".join(header_lines + kept_data) + f"\n\n...[and {len(data_lines) - len(kept_data)} more rows]..."


__all__ = ["summarize_tool_result"]
