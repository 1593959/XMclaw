"""Shared helpers for channel adapters.

Extracted to eliminate duplication in the text-chunking logic that was
previously copy-pasted across every adapter.
"""
from __future__ import annotations


def split_text(text: str, cap: int) -> list[str]:
    """Chunk *text* into pieces <= *cap* chars each.

    Prefers paragraph / line breaks; falls back to a hard cut when a
    single line is itself longer than the cap.
    """
    if not text:
        return []
    if len(text) <= cap:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > cap:
        # Try newline boundary within the cap window.
        cut = remaining.rfind("\n", 0, cap)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, cap)
        if cut <= 0:
            cut = cap  # hard cut — no whitespace in the window
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out
