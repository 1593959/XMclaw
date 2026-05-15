"""Persona MD renderer — write IDENTITY.md / USER.md from v2 L1 facts.

Wave-27 fix-12 / refactor B Phase 1.

Background
==========

The persona directory (``~/.xmclaw/persona/profiles/<id>/*.md``) holds
seven markdown files the agent reads verbatim each turn:

  IDENTITY.md  — AI 自我标识
  USER.md      — 关于人类用户
  AGENTS.md    — agent playbook
  TOOLS.md     — 工具小窍门
  SOUL.md      — 价值观
  MEMORY.md    — 长期事实 + 失败模式
  LEARNING.md  — 学到的规则

Pre-refactor, these files were written by TWO independent paths:

  * ``ExtractLessonsHook`` (post-sampling) writing AGENTS/TOOLS/MEMORY/
    SOUL/LEARNING via ``_append_under_section``
  * Manual user edits via the Web UI's 标识 tab

L1 v2 facts (LanceDB) was a SEPARATE store, written by
``KeyInfoExtractor`` / ``LLMFactExtractor`` / the ``remember`` tool /
the L1 panel's "+ 手动添加事实" form. The two stores didn't talk.

User complaint (2026-05-15): "AI 叫小咪 + 用户叫哥/敬宇" landed in L1
correctly, but IDENTITY.md / USER.md stayed empty templates →
visible inconsistency between what the panel showed and what the
file held.

This module
===========

Phase 1 deliverable: when a fact lands in L1 with a recognised
``bucket``, regenerate the corresponding persona MD file's auto
section from L1's state — so the on-disk file reflects what L1
holds, with no separate write path.

  bucket="agent_identity"   → IDENTITY.md  ## Auto-extracted
  bucket="user_identity"    → USER.md      ## Auto-identity
  bucket="user_preference"  → USER.md      ## Auto-extracted preferences

Phase 2 (next): AGENTS/TOOLS/SOUL/LEARNING/MEMORY auto sections, plus
``ExtractLessonsHook`` migration so EVERY auto-section write goes
through L1 only.

The agent's read path is untouched — it keeps reading the MD files
from disk every turn. We're changing WHO writes those files.
"""
from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

from xmclaw.utils.fs_locks import atomic_write_text


# ── Routing table ────────────────────────────────────────────────


# Each entry maps a bucket label → (target_filename, section_header,
# render_prefix). The renderer queries L1 for ``bucket=<label>``
# facts and replaces / appends the section.
BUCKET_TO_FILE: dict[str, tuple[str, str, str]] = {
    "agent_identity":  ("IDENTITY.md", "## Auto-extracted",             ""),
    "user_identity":   ("USER.md",     "## Auto-identity",              ""),
    "user_preference": ("USER.md",     "## Auto-extracted preferences", ""),
}

# Reverse map: which buckets feed which filename. Needed when one MD
# file aggregates multiple buckets (USER.md gets both identity AND
# preferences, each in its own section).
FILE_TO_BUCKETS: dict[str, list[str]] = {}
for _bucket, (_fn, _sec, _pfx) in BUCKET_TO_FILE.items():
    FILE_TO_BUCKETS.setdefault(_fn, []).append(_bucket)


# ── Section helpers ──────────────────────────────────────────────


def _render_section_body(facts: list[Any], prefix: str = "") -> str:
    """Format a list of Fact objects as a markdown bullet list.

    One bullet per fact, date-stamped (YYYY-MM-DD from ts_first).
    Facts are sorted by ts_first DESCENDING so the most recent ones
    appear first — matches how the user reads "auto-extracted" data
    in the panel.

    Each bullet body is the fact's text, optionally prefixed
    (e.g. "(置信度 0.95) ") so the reader sees which facts are
    high-confidence vs speculative.
    """
    if not facts:
        return ""
    sorted_facts = sorted(
        facts,
        key=lambda f: getattr(f, "ts_first", 0.0),
        reverse=True,
    )
    lines: list[str] = []
    for f in sorted_facts:
        date_str = _time.strftime(
            "%Y-%m-%d",
            _time.localtime(getattr(f, "ts_first", _time.time())),
        )
        text = (getattr(f, "text", "") or "").strip().replace("\n", " ")
        if not text:
            continue
        conf = getattr(f, "confidence", 0.0)
        conf_str = f" _(conf {conf:.2f})_" if conf else ""
        lines.append(f"- {date_str}: {prefix}{text}{conf_str}")
    return "\n".join(lines)


def _replace_section(text: str, header: str, new_body: str) -> str:
    """Replace the ``header`` section's body (everything up to the
    next ``## `` heading or EOF) with ``new_body``.

    When the header doesn't exist in ``text``, append a fresh
    section at the end. Empty ``new_body`` → still emits the header
    + a single placeholder line so the user sees the section exists
    but isn't populated yet.
    """
    rendered_section = (
        f"{header}\n{new_body}\n"
        if new_body
        else f"{header}\n_(nothing extracted yet)_\n"
    )

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    found = False
    while i < len(lines):
        line = lines[i]
        if line.strip() == header:
            found = True
            # Emit replacement, skip until next ## heading or EOF.
            out.append(rendered_section.rstrip("\n"))
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped.startswith("## ") and stripped != header:
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    if not found:
        # Append the new section to the bottom.
        result = text.rstrip("\n")
        if result:
            result += "\n\n"
        result += rendered_section
        return result
    return "\n".join(out).rstrip("\n") + "\n"


# ── Public API ───────────────────────────────────────────────────


async def render_persona_file(
    memory_service: Any,
    profile_dir: Path,
    basename: str,
) -> bool:
    """Re-render one persona MD file from current L1 state.

    ``profile_dir`` is the user's active persona profile directory
    (e.g. ``~/.xmclaw/persona/profiles/default``). ``basename`` is
    the MD file name (``"IDENTITY.md"`` etc.).

    Returns True when the file was rewritten, False when nothing
    needed updating (basename has no bucket mapping, or the section
    body matches what's already on disk — avoid pointless writes).

    Failures (filesystem errors, recall errors) are caught and
    logged; this function MUST NOT raise — it runs on the hot path
    of extractor write.
    """
    from xmclaw.utils.log import get_logger
    log = get_logger(__name__)

    buckets = FILE_TO_BUCKETS.get(basename)
    if not buckets:
        return False
    target = Path(profile_dir) / basename
    try:
        current = (
            target.read_text(encoding="utf-8")
            if target.exists() else ""
        )
    except OSError as exc:
        log.warning(
            "v2_renderer.read_failed file=%s err=%s",
            basename, exc,
        )
        return False

    new_text = current
    changed = False
    for bucket in buckets:
        cfg = BUCKET_TO_FILE[bucket]
        _fn, section_header, prefix = cfg
        try:
            hits = await memory_service.recall(
                None,
                buckets=[bucket],
                k=100,
                min_confidence=0.0,
                include_relations=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "v2_renderer.recall_failed bucket=%s err=%s",
                bucket, exc,
            )
            continue
        facts = [h.fact for h in hits]
        body = _render_section_body(facts, prefix=prefix)
        candidate = _replace_section(new_text, section_header, body)
        if candidate != new_text:
            new_text = candidate
            changed = True

    if not changed:
        return False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, new_text)
        log.info(
            "v2_renderer.wrote file=%s len=%d",
            basename, len(new_text),
        )
        return True
    except OSError as exc:
        log.warning(
            "v2_renderer.write_failed file=%s err=%s",
            basename, exc,
        )
        return False


async def render_all_persona_files(
    memory_service: Any,
    profile_dir: Path,
) -> dict[str, bool]:
    """Re-render every persona MD file the renderer knows about.

    Returns ``{basename: wrote_changes}`` — useful for the migration
    path or for an admin / debug endpoint that wants to force a
    full refresh.
    """
    report: dict[str, bool] = {}
    for basename in FILE_TO_BUCKETS:
        report[basename] = await render_persona_file(
            memory_service, profile_dir, basename,
        )
    return report


async def render_affected_files(
    memory_service: Any,
    profile_dir: Path,
    written_facts: list[Any],
) -> set[str]:
    """Render only the files affected by a batch of recent writes.

    Inspects each ``Fact``'s ``bucket`` field, determines which MD
    files (if any) need refresh, and renders each at most once.

    Returns the set of basenames actually rewritten — empty when
    none of the new facts had a routable bucket.
    """
    affected: set[str] = set()
    for f in written_facts:
        b = getattr(f, "bucket", "") or ""
        if not b:
            continue
        mapping = BUCKET_TO_FILE.get(b)
        if mapping is None:
            continue
        affected.add(mapping[0])

    rewritten: set[str] = set()
    for basename in affected:
        if await render_persona_file(
            memory_service, profile_dir, basename,
        ):
            rewritten.add(basename)
    return rewritten


__all__ = [
    "BUCKET_TO_FILE",
    "FILE_TO_BUCKETS",
    "render_affected_files",
    "render_all_persona_files",
    "render_persona_file",
]
