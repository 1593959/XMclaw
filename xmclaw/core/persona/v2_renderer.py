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
#
# Phase 1 (2026-05-15): identity + preference. UI's 标识 tab visibly
# inconsistent with L1 events panel — fixed by the first three rows.
#
# Phase 2 (2026-05-16): lesson buckets — the five that
# ExtractLessonsHook already emits. Header strings match the
# legacy PersonaStore's AUTO_SECTIONS so the agent's existing
# read path (full MD file → system prompt) sees the same section
# names it always saw.
BUCKET_TO_FILE: dict[str, tuple[str, str, str]] = {
    # ── Phase 1 ────────────────────────────────────────────────
    "agent_identity":  ("IDENTITY.md", "## Auto-extracted",             ""),
    "user_identity":   ("USER.md",     "## Auto-identity",              ""),
    "user_preference": ("USER.md",     "## Auto-extracted preferences", ""),
    # ── Phase 2 ────────────────────────────────────────────────
    "workflow":        ("AGENTS.md",   "## Auto-extracted",             ""),
    "tool_quirks":     ("TOOLS.md",    "## Auto-extracted",             ""),
    "failure_modes":   ("MEMORY.md",   "## Failure Modes",              ""),
    "values":          ("SOUL.md",     "## Auto-extracted",             ""),
    "rules":           ("LEARNING.md", "## Auto-extracted",             ""),
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
    # 2026-05-25 (chat-b3c614bc fix): read-time defense in depth.
    # Even if a poisoned fact somehow got past the write-time gate
    # (e.g. it was written before the gate existed, or via a tool
    # path that bypasses _write_facts_to_memory), skip it at render
    # time so the persona MD file stops re-asserting the lie. This
    # also means existing poisoned LanceDB rows clean themselves
    # out of the rendered output the moment the renderer runs.
    from xmclaw.core.persona.toxic_facts import (
        is_toxic_self_capability_denial,
    )
    sorted_facts = sorted(
        facts,
        key=lambda f: getattr(f, "ts_first", 0.0),
        reverse=True,
    )

    # 2026-05-26: render-time semantic dedup cap. When a single
    # section has more than ``_DEDUP_CAP`` facts we cluster by
    # embedding cosine similarity and emit only the survivor of each
    # cluster (highest confidence + most evidence + newest). This is
    # a read-time defense — bulk dedup via MemoryService.dedup_scope
    # is the proper fix, this just keeps the rendered file from
    # ballooning when no one has run dedup recently. Pre-fix, USER.md
    # had 7 bullets all paraphrasing "user prefers brief Chinese
    # messages" which crowded out genuinely distinct facts.
    if len(sorted_facts) > _DEDUP_CAP:
        sorted_facts = _cluster_representatives(sorted_facts)

    lines: list[str] = []
    from xmclaw.utils.log import get_logger
    _log = get_logger(__name__)
    for f in sorted_facts:
        date_str = _time.strftime(
            "%Y-%m-%d",
            _time.localtime(getattr(f, "ts_first", _time.time())),
        )
        text = (getattr(f, "text", "") or "").strip().replace("\n", " ")
        if not text:
            continue
        toxic, pid = is_toxic_self_capability_denial(text)
        if toxic:
            _log.info(
                "v2_renderer.toxic_fact_filtered pattern=%s text=%s",
                pid, text[:160],
            )
            continue
        conf = getattr(f, "confidence", 0.0)
        conf_str = f" _(conf {conf:.2f})_" if conf else ""
        lines.append(f"- {date_str}: {prefix}{text}{conf_str}")
    return "\n".join(lines)


# 2026-05-26: cluster representatives for the render-time dedup cap.
# Public-ish (no leading underscore on the file-scope constant) so
# tests can monkey-patch the cap when probing the cluster path.
_DEDUP_CAP = 12
_DEDUP_COSINE_THRESHOLD = 0.86


def _cluster_representatives(facts: list[Any]) -> list[Any]:
    """Cosine-cluster facts that carry an embedding and return only
    one survivor per cluster. Facts without embeddings pass through
    unchanged (we can't measure them, so we don't drop them).

    Survivor pick within a cluster: max(confidence) → max(evidence_count)
    → max(ts_last). Pure-Python loop is fine; clusters of O(50) at
    most in practice. Preserves the original order of the survivors
    by ts_first (the caller already sorted that way).
    """
    from math import sqrt

    clusters: list[list[Any]] = []
    for f in facts:
        emb = getattr(f, "embedding", None)
        if not emb:
            clusters.append([f])
            continue
        placed = False
        for cluster in clusters:
            ref = getattr(cluster[0], "embedding", None)
            if not ref:
                continue
            dot = sum(a * b for a, b in zip(emb, ref))
            na = sqrt(sum(a * a for a in emb))
            nb = sqrt(sum(b * b for b in ref))
            cos = dot / (na * nb) if (na and nb) else 0.0
            if cos >= _DEDUP_COSINE_THRESHOLD:
                cluster.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])

    survivors: list[Any] = []
    for cluster in clusters:
        if len(cluster) == 1:
            survivors.append(cluster[0])
            continue
        survivors.append(max(
            cluster,
            key=lambda g: (
                float(getattr(g, "confidence", 0.0)),
                int(getattr(g, "evidence_count", 0)),
                float(getattr(g, "ts_last", 0.0)),
            ),
        ))
    # Re-sort survivors by ts_first DESC to preserve the caller's
    # original ordering contract.
    survivors.sort(
        key=lambda g: float(getattr(g, "ts_first", 0.0)),
        reverse=True,
    )
    return survivors


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
    needed updating (basename has no bucket mapping AND no manual
    section, or the rendered content matches what's already on
    disk — avoid pointless writes).

    Phase 3b (2026-05-16): now combines the v2-stored MANUAL
    section (``kind=persona_manual, bucket=<basename>``) with the
    auto-section content. Result mirrors the legacy
    ``manual_part + auto_section`` shape so the agent's read path
    sees identical layout regardless of which renderer wrote it.

    Failures (filesystem errors, recall errors) are caught and
    logged; this function MUST NOT raise — it runs on the hot path
    of extractor write.
    """
    from xmclaw.utils.log import get_logger
    log = get_logger(__name__)

    buckets = FILE_TO_BUCKETS.get(basename) or []
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

    # Phase 3b: pull the v2-stored manual section. ``None`` when the
    # user has never touched this file via UI / update_persona tool.
    manual_text: str | None = None
    try:
        manual_fact = await memory_service.get_persona_manual(basename)
        if manual_fact is not None and manual_fact.text:
            manual_text = manual_fact.text.rstrip() + "\n"
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "v2_renderer.manual_read_failed file=%s err=%s",
            basename, exc,
        )

    # When neither buckets nor manual content exist → nothing to do.
    if not buckets and manual_text is None:
        return False

    # If we have v2 manual content, start fresh from it (replaces
    # whatever was on disk's manual region). Otherwise preserve the
    # existing disk content's manual portion verbatim so user-edited
    # IDENTITY.md / etc. survives Phase 1/2 callers that don't write
    # v2 manual rows yet.
    if manual_text is not None:
        new_text = manual_text
    else:
        new_text = current

    changed = (new_text != current) if manual_text is not None else False

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

    # Wave-27 fix-LAT13: apply PERSONA_CHAR_CAPS at render time.
    # Pre-fix the v2 renderer wrote whatever the L1 facts produced,
    # bypassing the caps that builtin_persona.update_persona enforces
    # on the legacy write path. SOUL.md / LEARNING.md grew to 13K+
    # chars each and blew system_prompt to 37K tokens per turn —
    # leaving Kimi 256K with too little room for messages on multi-
    # hop tasks. ``enforce_char_cap`` LRU-evicts oldest dated
    # bullets first, preserving prose/headers, so curated content
    # survives even when auto-extracted lessons get truncated.
    try:
        # 2026-05-17: import direct from core.persona.caps (not
        # providers.tool._helpers) so the import-direction check in
        # scripts/check_import_direction.py stays clean.
        from xmclaw.core.persona.caps import (
            PERSONA_CHAR_CAPS, enforce_char_cap,
        )
        cap = PERSONA_CHAR_CAPS.get(basename)
        if cap is not None and len(new_text) > cap:
            original_len = len(new_text)
            new_text = enforce_char_cap(new_text, cap)
            log.info(
                "v2_renderer.capped file=%s %d→%d (cap=%d)",
                basename, original_len, len(new_text), cap,
            )
    except Exception as exc:  # noqa: BLE001 — cap is a defense in
        # depth, not a correctness invariant
        log.warning(
            "v2_renderer.cap_apply_failed file=%s err=%s",
            basename, exc,
        )

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
