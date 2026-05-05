"""PersonaStore — B-198 / Phase 3: DB-as-truth for persona files.

Architecture switch (see ``docs/MEMORY_ARCHITECTURE.md`` §3):

    ❌ before — markdown files on disk are truth, DB is parallel index
    ✅ after  — DB rows are truth, markdown is rendered cache for humans

Two row kinds back every persona file:

* ``kind=persona_manual`` — one row per file (USER.md, MEMORY.md,
  SOUL.md, …). ``text`` is the user-curated prose / decisions / pinned
  content. Auto-sections are NOT included here — they live as their
  own rows below and get appended at render time. ``metadata.file``
  identifies which canonical persona file this row backs.

* extracted facts (``kind=preference``, ``kind=lesson``,
  ``kind=procedure``, …) — each bullet is a row. When a persona file
  has an auto-extracted section, the renderer fetches the matching
  rows and emits them as bullets under that section.

Render behaviour per file (see :mod:`xmclaw.core.persona.render`):

* SOUL.md / IDENTITY.md / LEARNING.md / BOOTSTRAP.md → manual only.
  No auto append. The store is just a single-row container.
* USER.md → manual + ``## Auto-extracted preferences`` filled with
  ``kind=preference`` rows.
* MEMORY.md → manual + ``## Failure Modes`` from
  ``kind=lesson`` where ``bucket=failure_modes`` (or unscoped).
* AGENTS.md → manual + ``## Auto-extracted`` from
  ``kind=lesson`` where ``bucket=workflow``.
* TOOLS.md → manual + ``## Auto-extracted`` from
  ``kind=lesson`` where ``bucket=tool_quirks``.

Write paths route here:

* ``set_manual(filename, text)`` — user edited the file via Web UI /
  ``update_persona`` tool. We strip the auto-section (if any) before
  writing so the manual row stays clean — auto rows are derived,
  never user-edited.
* ``add_fact(kind, text, metadata)`` — extractor produced a fact.
  Goes through the underlying provider's ``upsert_fact`` (B-197 Phase
  2 strengthening). Render output reflects on next ``get_text`` call.
* ``render_to_disk(filename)`` — refresh the on-disk cached file.
  Daemon calls this after store mutations so external readers (the
  user, ``rg``, file diff tools) see fresh content.

Migration from existing markdown:

* ``migrate_from_disk()`` is the one-shot bootstrap. It reads every
  canonical persona file currently on disk, splits each into
  manual-prose + auto-extracted bullets, and seeds the store. Idempotent
  — re-runs after the first do nothing.

Failure isolation: every method swallows DB errors with a log warning;
the markdown cache file remains valid after a partial failure so the
agent can keep operating.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.utils.fs_locks import atomic_write_text, get_lock

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Auto-section anchors per persona file
# ─────────────────────────────────────────────────────────────────────

# Each entry: filename → (section_header, fact_kind, bucket_filter|None).
# bucket_filter=None means "any fact of this kind"; a string filters
# metadata.bucket. None entries (e.g. SOUL.md) mean "manual only — no
# auto append".
AUTO_SECTIONS: dict[str, tuple[str, str, str | None] | None] = {
    "SOUL.md":      None,
    "IDENTITY.md":  None,
    "LEARNING.md":  None,
    "BOOTSTRAP.md": None,
    "USER.md":      ("## Auto-extracted preferences", "preference", None),
    "MEMORY.md":    ("## Failure Modes",              "lesson",     "failure_modes"),
    "AGENTS.md":    ("## Auto-extracted",             "lesson",     "workflow"),
    "TOOLS.md":     ("## Auto-extracted",             "lesson",     "tool_quirks"),
}

# Marker used on persona_manual rows so the migration / lookup queries
# can find them deterministically.
_MANUAL_KIND = "persona_manual"


# ─────────────────────────────────────────────────────────────────────
# Markdown split helpers
# ─────────────────────────────────────────────────────────────────────


def split_manual_and_auto(text: str, *, auto_header: str) -> tuple[str, str]:
    """Split a persona markdown file at the auto-section heading.

    Returns ``(manual_part, auto_part)``. If the auto header doesn't
    appear, the whole file is treated as manual and ``auto_part`` is
    empty. The auto section is everything from the header line to EOF
    (or the next ``## ``-level heading at the same depth — but we
    treat the auto section as terminal because that's what the existing
    write paths do).
    """
    if not text:
        return "", ""

    # Anchor matches the header at the start of a line. Be tolerant of
    # trailing whitespace / OS-specific line endings.
    pattern = re.compile(
        r"^" + re.escape(auto_header) + r"\s*$",
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return text.rstrip("\n") + "\n" if text.endswith("\n") else text, ""

    manual = text[: m.start()].rstrip()
    auto = text[m.start():].rstrip("\n") + "\n"
    return manual + ("\n" if manual else ""), auto


def parse_auto_bullets(auto_text: str) -> list[str]:
    """Pull individual bullets out of the auto section. Used by the
    migration step to seed each existing markdown bullet as a fact row.
    """
    if not auto_text.strip():
        return []
    out: list[str] = []
    for line in auto_text.split("\n"):
        # Top-level bullets only — start with "- " at column 0.
        # Indented continuations / nested bullets are intentionally
        # ignored; the migration importer treats every top-level
        # bullet as one fact.
        if not line.startswith("- "):
            continue
        out.append(line[2:].strip())
    return out


# ─────────────────────────────────────────────────────────────────────
# PersonaStore
# ─────────────────────────────────────────────────────────────────────


class PersonaStore:
    """DB-backed source-of-truth for persona content.

    Parameters
    ----------
    memory_provider : SqliteVecMemory-like
        Underlying store. Must expose ``put`` / ``query`` /
        ``upsert_fact``. We intentionally type this loosely so this
        module stays free of imports from ``providers/`` (import
        direction rule).
    profile_dir : pathlib.Path
        Where rendered cache files land. The daemon writes them so
        external tools / Web UI keep working.
    item_factory : optional callable
        Returns a MemoryItem-shaped object given keyword args
        ``(id, layer, text, metadata, embedding, ts)``. Provided by
        the daemon side so this ``core/`` module stays free of
        ``providers/`` imports per the layering rule.
    """

    def __init__(
        self,
        memory_provider: Any,
        profile_dir: Path,
        *,
        item_factory: Any = None,
    ) -> None:
        self._mem = memory_provider
        self._profile_dir = Path(profile_dir)
        self._item_factory = item_factory
        # Module-level write lock per file so parallel set_manual /
        # add_fact don't trample render_to_disk mid-flight.
        self._render_locks: dict[str, Any] = {}

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    # ── reads ──

    async def get_text(self, basename: str) -> str:
        """Render ``basename`` from the DB. Manual prose + (optional)
        auto-extracted bullets.

        Returns empty string when the manual row is missing AND no
        facts of the configured kind exist — that's a fresh-install
        signal the persona assembler / Web UI use to decide whether
        to plant a default template.
        """
        manual = await self._read_manual(basename)
        auto = await self._render_auto(basename)
        if manual and auto:
            return manual.rstrip("\n") + "\n\n" + auto
        return manual or auto or ""

    async def read_manual(self, basename: str) -> str:
        """Public accessor: just the manual portion of ``basename``,
        without the auto-extracted block. Useful for tools that need
        to read-modify-write the user-curated content (e.g.
        ``update_persona append_section`` mode)."""
        return await self._read_manual(basename)

    async def list_files(self) -> list[str]:
        """Return the canonical persona files this store knows about."""
        return list(AUTO_SECTIONS.keys())

    # ── writes ──

    async def set_manual(self, basename: str, text: str) -> None:
        """Write the manual portion of a persona file.

        ``text`` may include the auto section (if the user roundtripped
        a render through the Web UI without changes); we strip it
        before persisting so the manual row stays clean. Auto-section
        edits are silently ignored — they're derived from fact rows.
        """
        if basename not in AUTO_SECTIONS:
            raise ValueError(f"unknown persona file: {basename!r}")
        config = AUTO_SECTIONS[basename]
        manual_only = text
        if config is not None:
            header, _, _ = config
            manual_only, _ = split_manual_and_auto(text, auto_header=header)
        manual_only = manual_only.rstrip() + "\n" if manual_only.strip() else ""
        await self._write_manual(basename, manual_only)
        # Refresh the cached file on disk so external readers / Web UI
        # see the new content immediately.
        await self.render_to_disk(basename)

    async def add_fact(
        self,
        *,
        kind: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        layer: str = "working",
    ) -> str:
        """Add a fact row via the underlying store's upsert_fact path
        so duplicates merge into evidence_count rather than stacking.

        Returns the row id. On failure (DB unreachable, no upsert
        method) the call logs and returns an empty string — the caller
        should not rely on the return value for anything but tests.
        """
        md = dict(metadata or {})
        md.setdefault("kind", kind)
        upsert = getattr(self._mem, "upsert_fact", None)
        try:
            if upsert is not None:
                row_id, _strengthened = await upsert(
                    text=text,
                    embedding=embedding,
                    layer=layer,
                    metadata=md,
                )
                return row_id
            # Fallback: legacy put for providers without upsert.
            if self._item_factory is None:
                _log.warning(
                    "persona_store.add_fact_no_factory kind=%s — "
                    "provider has no upsert_fact and no item_factory "
                    "wired; row not persisted", kind,
                )
                return ""
            item = self._item_factory(
                id=uuid.uuid4().hex,
                layer=layer,
                text=text,
                metadata=md,
                embedding=tuple(embedding) if embedding else None,
                ts=time.time(),
            )
            await self._mem.put(layer, item)
            return getattr(item, "id", "")
        except Exception as exc:  # noqa: BLE001
            _log.warning("persona_store.add_fact_failed kind=%s err=%s", kind, exc)
            return ""

    async def render_to_disk(self, basename: str | None = None) -> None:
        """Re-render one (or all) persona file(s) from DB to the cache
        on disk. Called after every mutation so external readers see
        fresh content.

        Atomic write via ``atomic_write_text`` — daemon crash mid-flush
        leaves either old or new file, never a half-truncated one.
        """
        targets = [basename] if basename else list(AUTO_SECTIONS.keys())
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        for name in targets:
            target = self._profile_dir / name
            text = await self.get_text(name)
            # Skip writes when the rendered text is empty AND the file
            # already doesn't exist — prevents creating bogus empty
            # files for SOUL/IDENTITY etc. that haven't been seeded.
            if not text and not target.is_file():
                continue
            try:
                async with get_lock(target):
                    atomic_write_text(target, text)
            except OSError as exc:
                _log.warning(
                    "persona_store.render_failed file=%s err=%s",
                    name, exc,
                )

    # ── migration ──

    async def migrate_from_disk(self) -> dict[str, int]:
        """One-shot bootstrap. Read every canonical persona file from
        disk, split into manual + bullet rows, write to DB.

        Idempotent — re-running after the manual row already exists in
        DB skips that file. Useful after first install with existing
        markdown content, or after a DB wipe + restore-from-files.

        Returns a dict ``{file: bullet_count}`` for diagnostics.
        """
        report: dict[str, int] = {}
        for basename, config in AUTO_SECTIONS.items():
            target = self._profile_dir / basename
            if not target.is_file():
                continue
            try:
                disk_text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            existing_manual = await self._read_manual(basename)
            if existing_manual:
                # Already migrated — skip.
                report[basename] = 0
                continue

            if config is None:
                # Pure-manual file: store the whole text as manual.
                await self._write_manual(basename, disk_text)
                report[basename] = 0
                continue

            header, fact_kind, bucket = config
            manual_part, auto_part = split_manual_and_auto(
                disk_text, auto_header=header,
            )
            await self._write_manual(basename, manual_part)
            bullets = parse_auto_bullets(auto_part)
            for bullet in bullets:
                md: dict[str, Any] = {
                    "kind": fact_kind,
                    "evidence_count": 1,
                    "migrated_from": basename,
                    "ts": time.time(),
                }
                if bucket is not None:
                    md["bucket"] = bucket
                await self.add_fact(
                    kind=fact_kind,
                    text=bullet,
                    metadata=md,
                    layer="long",  # migrated content is durable
                )
            report[basename] = len(bullets)
        return report

    # ── internal ──

    async def _read_manual(self, basename: str) -> str:
        """Fetch the manual row for a persona file. Empty when absent."""
        try:
            hits = await self._mem.query(
                "long",
                text=None,
                k=1,
                filters={"kind": _MANUAL_KIND, "file": basename},
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "persona_store.read_manual_failed file=%s err=%s",
                basename, exc,
            )
            return ""
        if not hits:
            return ""
        return getattr(hits[0], "text", "") or ""

    async def _write_manual(self, basename: str, text: str) -> None:
        """Write or replace the manual row. Deterministic id keyed by
        filename so writes idempotently overwrite."""
        if self._item_factory is None:
            _log.warning(
                "persona_store.write_manual_no_factory file=%s",
                basename,
            )
            return
        item_id = f"persona_manual:{basename}"
        try:
            item = self._item_factory(
                id=item_id,
                layer="long",
                text=text,
                metadata={
                    "kind": _MANUAL_KIND,
                    "file": basename,
                    "ts": time.time(),
                },
                embedding=None,
                ts=time.time(),
            )
            await self._mem.put("long", item)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "persona_store.write_manual_failed file=%s err=%s",
                basename, exc,
            )

    async def _render_auto(self, basename: str) -> str:
        """Render the auto-extracted section for ``basename`` (if any).
        Empty when the file has no auto config or no facts."""
        config = AUTO_SECTIONS.get(basename)
        if config is None:
            return ""
        header, fact_kind, bucket = config
        try:
            filters: dict[str, Any] = {"kind": fact_kind}
            if bucket is not None:
                filters["bucket"] = bucket
            # Pull working + long; rendered output is what the human +
            # agent see at injection time.
            hits_long = await self._mem.query(
                "long", text=None, k=200, filters=filters,
            )
            hits_working = await self._mem.query(
                "working", text=None, k=200, filters=filters,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "persona_store.render_auto_failed file=%s err=%s",
                basename, exc,
            )
            return ""

        # Merge + dedup by id. Sort by (evidence_count desc, ts desc)
        # so the most-supported facts surface first.
        seen: set[str] = set()
        rows: list[Any] = []
        for h in list(hits_long) + list(hits_working):
            rid = getattr(h, "id", None)
            if not rid or rid in seen:
                continue
            seen.add(rid)
            md = getattr(h, "metadata", {}) or {}
            if md.get("superseded_by"):
                continue
            rows.append(h)
        rows.sort(
            key=lambda r: (
                -((r.metadata or {}).get("evidence_count", 1)),
                -getattr(r, "ts", 0.0),
            )
        )
        if not rows:
            return ""

        lines: list[str] = [header, ""]
        for r in rows:
            text = (r.text or "").strip()
            if not text:
                continue
            # Compact metadata footer for transparency: source +
            # evidence_count when > 1.
            md = r.metadata or {}
            ev = int(md.get("evidence_count", 1) or 1)
            sid = md.get("source_session_id") or md.get("session_id") or ""
            tag_parts: list[str] = []
            if ev > 1:
                tag_parts.append(f"×{ev}")
            if sid and ev <= 1:
                # Only show session for fresh single-evidence rows;
                # mature rows don't need the breadcrumb.
                tag_parts.append(f"src={sid}")
            tag = (" ".join(tag_parts)).strip()
            if tag:
                lines.append(f"- [{tag}] {text}")
            else:
                lines.append(f"- {text}")
        return "\n".join(lines).rstrip() + "\n"
