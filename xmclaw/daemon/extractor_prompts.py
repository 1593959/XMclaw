"""Per-extractor prompt loader. B-182.

Pre-B-182 the LLM extractor prompts (skill / profile / lessons) were
hardcoded multi-line strings inside ``xmclaw/daemon/llm_extractors.py``
and ``xmclaw/daemon/post_sampling_hooks.py``. Editing a prompt was a
code change → wheel build → reinstall. Real-data audit found this
forced architectural friction onto operational tuning ("the LLM
keeps producing primitive wrappers — let me tweak the rejection
list" became a multi-day cycle).

This module makes prompts file-backed:

    ~/.xmclaw/v2/extractor_prompts/
        skill_extractor.md       ← LLM extractor for SkillProposer
        profile_extractor.md     ← LLM extractor for ProfileExtractor
        extract_lessons.md       ← post-sampling lesson hook
        extract_memories.md      ← post-sampling memory hook

Each file is plain text — the prompt verbatim. No frontmatter, no
parsing. Editing a file changes the next extractor invocation.
First run writes the bundled default; subsequent runs read whatever
the user left in place.

The bundled defaults stay in code (passed to ``load_prompt`` as
fallback) so a fresh install or a partially-cleaned ``~/.xmclaw/v2``
still works without surprises.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from xmclaw.utils.paths import data_dir

_log = logging.getLogger(__name__)

# Cache reads so a 30-minute dream-cycle interval doesn't hit the
# disk on every prompt-using call. Invalidated by (path, mtime) so
# editing a file is picked up on the next call without restart.
_CACHE_LOCK = threading.Lock()
_CACHE: dict[Path, tuple[float, str]] = {}


def prompts_dir() -> Path:
    """Where extractor prompts live. Mirrors ``user_skills_dir()``
    pattern: under ``data_dir()`` so backup tooling sweeps it up
    automatically."""
    return data_dir() / "v2" / "extractor_prompts"


def load_prompt(name: str, default: str) -> str:
    """Return ``<prompts_dir>/<name>.md`` content; fall back to
    ``default`` if file is missing, unreadable, or empty after strip.

    First-run side effect: when the file is missing AND the parent
    dir is writable, write ``default`` to disk so subsequent reads
    pick it up and the user has a discoverable starting point. Disk
    failures during write are swallowed — the in-memory default
    still works.
    """
    path = prompts_dir() / f"{name}.md"

    # Cache hit when file mtime hasn't changed since last read.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # Missing file → seed with default if we can.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(default, encoding="utf-8")
            _log.info(
                "extractor_prompt.seeded path=%s len=%d", path, len(default),
            )
        except OSError as exc:
            _log.warning(
                "extractor_prompt.seed_failed path=%s err=%s", path, exc,
            )
        return default

    with _CACHE_LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("extractor_prompt.read_failed path=%s err=%s", path, exc)
        return default

    if not text.strip():
        return default

    with _CACHE_LOCK:
        _CACHE[path] = (mtime, text)
    return text


def reset_cache() -> None:
    """Drop the in-memory cache. Tests use this to isolate runs;
    production never needs it (mtime-based invalidation handles
    edits)."""
    with _CACHE_LOCK:
        _CACHE.clear()
