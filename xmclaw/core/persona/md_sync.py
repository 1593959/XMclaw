"""Synchronize persona markdown edits into v2 memory.

Facts remain the source of truth for auto-rendered sections. This module
only ingests the human/manual part of persona markdown files and stores it as
a structured ``persona_manual`` fact, then asks the renderer to project the
merged view back to disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xmclaw.core.persona.v2_renderer import FILE_TO_BUCKETS, render_persona_file


_AUTO_BLOCK_RE = re.compile(
    r"<!--\s*XMC-AUTO-EXTRACTED:[^>]*:BEGIN.*?-->\s*.*?"
    r"<!--\s*XMC-AUTO-EXTRACTED:[^>]*:END\s*-->",
    re.DOTALL,
)
_FID_LINE_RE = re.compile(r"<!--\s*fid:[^>]+-->")


@dataclass(frozen=True, slots=True)
class MdSyncCandidate:
    basename: str
    manual_text: str
    source_path: str
    skipped_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MdSyncReport:
    scanned: int
    written: int
    rendered: tuple[str, ...]
    candidates: tuple[MdSyncCandidate, ...]
    mode: str = "manual_to_facts"


@dataclass(frozen=True, slots=True)
class MdSyncPolicy:
    """Policy for persona markdown versus structured memory.

    ``manual_to_facts`` keeps the intended ownership model: manual MD prose is
    ingested as ``persona_manual``; structured facts stay in the fact store.
    ``facts_to_view`` only renders files from existing structured/manual rows.
    ``bidirectional_review`` currently behaves like manual ingestion plus
    render, but remains explicit so future review queues do not invent a
    second authority source.
    """

    enabled: bool = True
    direction: str = "manual_to_facts"
    authority: str = "structured_facts"


def parse_md_sync_policy(config: dict[str, Any] | None = None) -> MdSyncPolicy:
    cfg = config if isinstance(config, dict) else {}
    mem_cfg = cfg.get("cognition", {}).get("memory_v2", {}) if isinstance(cfg.get("cognition"), dict) else {}
    raw = mem_cfg.get("md_sync", {}) if isinstance(mem_cfg, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    direction = str(raw.get("direction", "manual_to_facts") or "manual_to_facts")
    if direction not in {"manual_to_facts", "facts_to_view", "bidirectional_review"}:
        direction = "manual_to_facts"
    authority = str(raw.get("authority", "structured_facts") or "structured_facts")
    if authority not in {"structured_facts", "manual_md"}:
        authority = "structured_facts"
    return MdSyncPolicy(
        enabled=bool(raw.get("enabled", True)),
        direction=direction,
        authority=authority,
    )


def extract_manual_md_memories(profile_dir: Path | str) -> list[MdSyncCandidate]:
    """Extract manual persona-file content while ignoring rendered facts."""
    root = Path(profile_dir)
    candidates: list[MdSyncCandidate] = []
    for basename in sorted(FILE_TO_BUCKETS):
        path = root / basename
        reasons: list[str] = []
        if not path.exists():
            reasons.append("file_missing")
            candidates.append(MdSyncCandidate(
                basename=basename,
                manual_text="",
                source_path=str(path),
                skipped_reasons=tuple(reasons),
            ))
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            candidates.append(MdSyncCandidate(
                basename=basename,
                manual_text="",
                source_path=str(path),
                skipped_reasons=("read_failed",),
            ))
            continue
        manual = _strip_auto_content(raw).strip()
        if not manual:
            reasons.append("manual_empty")
        candidates.append(MdSyncCandidate(
            basename=basename,
            manual_text=(manual + "\n") if manual else "",
            source_path=str(path),
            skipped_reasons=tuple(reasons),
        ))
    return candidates


async def sync_manual_md_to_memory(
    memory_service: Any,
    profile_dir: Path | str,
    *,
    render: bool = True,
    policy: MdSyncPolicy | None = None,
) -> MdSyncReport:
    """Persist manual persona markdown sections into v2 memory.

    Empty or header-only manual sections are NOT written: instead, any
    existing manual row is deleted so the bundled template survives the next
    render. This avoids a regression where the watcher would capture the
    template's section header as "manual" prose and then clobber the file on
    the next daemon boot.

    Wave-27 fix-LAT16: when the only remaining "manual" content is an auto-
    extracted section header (e.g. the file was reset to its bundled template
    or a previous render wrote only the placeholder section), do NOT upsert
    that header as the manual row. Upserting it would make the v2 renderer
    drop the bundled template on the next boot. Instead, delete any existing
    manual row so the renderer falls back to the on-disk file content.
    """
    root = Path(profile_dir)
    policy = policy or MdSyncPolicy()
    candidates = extract_manual_md_memories(root)
    if not policy.enabled:
        return MdSyncReport(
            scanned=len(candidates),
            written=0,
            rendered=(),
            candidates=tuple(candidates),
            mode="disabled",
        )
    if policy.direction == "facts_to_view":
        rendered: list[str] = []
        if render:
            for basename in sorted(FILE_TO_BUCKETS):
                changed = await render_persona_file(
                    memory_service,
                    root,
                    basename,
                    include_auto_sections=False,
                )
                if changed:
                    rendered.append(basename)
        return MdSyncReport(
            scanned=len(candidates),
            written=0,
            rendered=tuple(rendered),
            candidates=tuple(candidates),
            mode=policy.direction,
        )
    written = 0
    rendered: list[str] = []
    for candidate in candidates:
        if "file_missing" in candidate.skipped_reasons or "read_failed" in candidate.skipped_reasons:
            continue
        if _manual_is_substantial(candidate.manual_text):
            await memory_service.upsert_persona_manual(
                candidate.basename,
                candidate.manual_text,
            )
        else:
            # File has no real user-curated content (template-only or
            # section-header-only). Clear any stale manual row so the
            # bundled template survives the next render.
            await memory_service.delete_persona_manual(candidate.basename)
        written += 1
        if render:
            changed = await render_persona_file(
                memory_service,
                root,
                candidate.basename,
                include_auto_sections=False,
            )
            if changed:
                rendered.append(candidate.basename)
    return MdSyncReport(
        scanned=len(candidates),
        written=written,
        rendered=tuple(rendered),
        candidates=tuple(candidates),
        mode=policy.direction,
    )


def _manual_is_substantial(manual_text: str) -> bool:
    """Return True when ``manual_text`` contains prose beyond headers."""
    for line in (manual_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            return True
    return False


def _strip_auto_content(text: str) -> str:
    without_blocks = _AUTO_BLOCK_RE.sub("", text or "")
    kept: list[str] = []
    for line in without_blocks.splitlines():
        if _FID_LINE_RE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


__all__ = [
    "MdSyncCandidate",
    "MdSyncPolicy",
    "MdSyncReport",
    "extract_manual_md_memories",
    "parse_md_sync_policy",
    "sync_manual_md_to_memory",
]
