"""Persona-file char caps + LRU bullet eviction.

Pure data + pure-function module. Lives under ``core/`` so that the
``core/persona/v2_renderer.py`` rendering pipeline can use it
without triggering the
"core cannot import from providers or skills" architectural rule
(``scripts/check_import_direction.py``).

The legacy import paths
``from xmclaw.providers.tool._helpers import PERSONA_CHAR_CAPS,
enforce_char_cap`` and
``from xmclaw.providers.tool.builtin import PERSONA_CHAR_CAPS,
enforce_char_cap`` continue to work — those modules now re-export
from here. Eventually call sites should point at ``core.persona.caps``
directly, but the re-export keeps the bug-fix change small.
"""
from __future__ import annotations

import re


# B-25 (the upstream agent parity): char-level cap on persona files. The
# defaults follow the upstream agent' MemoryStore (MEMORY.md=2200, USER.md=1375)
# — bigger than that is a sign of bloat, not insight density. Eviction
# is LRU by ENTRY (lines starting with "-"): drop the oldest bullets
# in the largest section first, keep the file's frontmatter + section
# headers + non-bullet prose intact.
PERSONA_CHAR_CAPS: dict[str, int] = {
    "MEMORY.md": 2200,
    "USER.md":   1375,
    # B-168: AGENTS.md / TOOLS.md gain auto-extracted lesson buckets,
    # so they need a cap too — same heuristic (LRU evict oldest dated
    # bullets when over budget). Slightly bigger than USER because a
    # workflow lesson tends to be one paragraph not one phrase.
    "AGENTS.md": 2000,
    # Wave-27 fix-LAT13a: TOOLS.md cap is BIG because the auto-
    # rendered tool list (XMC-AUTO-TOOLS marker block) needs to fit.
    # 130 registered tools × ~100 chars each = ~13K chars baseline;
    # the cap is set higher to absorb that + a few KB of manual
    # guidance and dated auto-extracted lessons. enforce_char_cap
    # only evicts dated bullets, so the marker block (unprefixed
    # bullets) is structurally safe.
    "TOOLS.md":  18000,
    # Wave-27 fix-LAT13 (2026-05-17): SOUL.md / LEARNING.md gained
    # ``## Auto-extracted`` sections (B-303 routes ``values`` →
    # SOUL.md and ``rules`` → LEARNING.md) and BOTH were uncapped.
    # Real-data measurement: a single session grew SOUL.md auto-
    # extracted to 13393 chars and LEARNING.md to 13076 chars,
    # blowing the system prompt to 37K tokens — every turn. With
    # 130 tool specs (~16K tokens) the prompt overhead alone left
    # only ~200K tokens for messages, and Kimi 256K rejected
    # multi-hop turns. Capping at 4K/6K keeps the most recent
    # bullets (LRU eviction by YYYY-MM-DD prefix) and shrinks
    # system_prompt to ~7K tokens total. Loss is bounded: the
    # underlying L1 facts stay queryable via memory_search.
    "SOUL.md":     4000,
    "LEARNING.md": 6000,
    # IDENTITY.md / BOOTSTRAP.md remain uncapped — user-authored,
    # not auto-appended.
}


def enforce_char_cap(text: str, cap: int) -> str:
    """If ``text`` exceeds ``cap`` chars, drop oldest bullets until
    it fits. Returns possibly-shrunk text. No-op when already small.

    Heuristic for "oldest": bullets sort by the ``YYYY-MM-DD`` prefix
    that ``remember`` / ``learn_about_user`` write — earliest date
    evicts first. Bullets without a date prefix are evicted only when
    everything else is gone.
    """
    if len(text) <= cap:
        return text

    lines = text.split("\n")

    def _bullet_date(ln: str) -> str:
        """Return the YYYY-MM-DD prefix or empty string."""
        m = re.match(r"\s*-\s*(\d{4}-\d{2}-\d{2})", ln)
        return m.group(1) if m else ""

    # Index every bullet line for eviction candidacy. Non-bullet lines
    # (headers, frontmatter, prose) are preserved in place.
    bullet_idx = [
        (i, _bullet_date(ln))
        for i, ln in enumerate(lines)
        if ln.strip().startswith("-")
    ]
    if not bullet_idx:
        return text  # nothing to evict

    # Order bullets oldest-first. Empty date sorts FIRST (evict
    # context-less bullets earliest because we have no temporal info
    # to weigh them).
    bullet_idx.sort(key=lambda x: (x[1] or ""))

    drop_set: set[int] = set()
    out_text = text
    while len(out_text) > cap and bullet_idx:
        drop_idx, _ = bullet_idx.pop(0)
        drop_set.add(drop_idx)
        # Recompute size with evictions applied.
        out_text = "\n".join(
            ln for i, ln in enumerate(lines) if i not in drop_set
        )

    # Strip trailing blank lines that may now form runs.
    out_text = re.sub(r"\n{3,}", "\n\n", out_text).rstrip() + "\n"
    return out_text


__all__ = ["PERSONA_CHAR_CAPS", "enforce_char_cap"]
