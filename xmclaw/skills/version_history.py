"""Epic #27 P2 G-07 (2026-05-19) — per-skill versioned edit history.

When the SkillsWatcher detects a SKILL.md / skill.py / manifest.json
content change, the previous-step snapshotter writes the new content
under ``<skill_dir>/.versions/<utc-iso>.<ext>`` so the user can:

  * inspect the diff between their current edit and the previous save
    (``skill_diff`` meta-tool, or by direct read of ``.versions/``);
  * roll back to the previous save (``skill_rollback`` meta-tool, or
    by manually copying a ``.versions/`` entry over the live file).

Storage model is intentionally dumb: one file per save, named by UTC
timestamp so chronological order falls out of an alphabetical sort.
No SQLite, no JSON index — failure of the snapshotter to write a
backup must NEVER block the actual content reload, so we keep the
write path free of any dependency that could be slow or locked.

Format examples:

::

    ~/.xmclaw/skills_user/my-skill/
        SKILL.md                            ← live content
        .versions/
            2026-05-19T08-14-32Z.md         ← previous save
            2026-05-19T08-23-08Z.md         ← save before that
            ...

Retention cap: we keep at most ``MAX_VERSIONS_PER_SKILL`` snapshots
per (skill_dir, extension) bucket, pruning the oldest on overflow.
Skill files are typically small (kB-scale markdown / Python) so 50
entries × 8 KB ≈ 400 KB worst-case per skill — cheap.

Anti-features (intentional):

  * No cross-skill global history; each skill's ``.versions/`` is its
    own.
  * No author / commit-message stamping; we don't know who edited the
    file (editor saves are anonymous from the watcher's POV).
  * No automatic rollback on bad content — the watcher's
    ``SkillsWatcher._maybe_hot_reload_or_announce`` already routes
    failed reloads through the load-failures path; G-07 just makes
    the operator's manual recovery cheaper.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import logging

log = logging.getLogger(__name__)

VERSIONS_DIR_NAME = ".versions"
MAX_VERSIONS_PER_SKILL = 50

# Timestamp format chosen so alphabetical sort = chronological sort
# AND the result is a valid filename on Windows (no ``:``).
_TS_FORMAT = "%Y-%m-%dT%H-%M-%S.%fZ"


@dataclass(frozen=True, slots=True)
class VersionEntry:
    """One snapshot. ``ts`` is UTC-naive (the file name already
    carries the Z tail), ``index`` is the position in the
    newest-first ordering returned by :func:`list_versions`."""

    path: Path
    ts: datetime
    index: int
    ext: str


def _versions_dir(skill_dir: Path) -> Path:
    return skill_dir / VERSIONS_DIR_NAME


def snapshot(skill_dir: Path, file: Path) -> Path | None:
    """Copy ``file`` into ``<skill_dir>/.versions/<ts>.<ext>``.

    Returns the snapshot path on success; ``None`` on failure (the
    snapshotter NEVER raises — losing a backup must not break the
    live reload). After writing, prunes the oldest entries above
    :data:`MAX_VERSIONS_PER_SKILL` for the matching extension.

    Idempotent for the same content within a single timestamp grain
    (microseconds): if the previous snapshot's content equals
    ``file``'s content, we skip writing a new one to avoid spamming
    the directory on repeated save-no-change editor flushes.
    """
    if not file.is_file():
        return None
    try:
        body = file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning(
            "version_history.read_failed file=%s err=%s", file, exc,
        )
        return None

    vdir = _versions_dir(skill_dir)
    try:
        vdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "version_history.mkdir_failed dir=%s err=%s", vdir, exc,
        )
        return None

    ext = file.suffix.lstrip(".") or "txt"

    # Idempotent-skip: if the newest existing snapshot already holds
    # this exact content, write nothing — editors that save on every
    # keystroke would otherwise pollute .versions/.
    existing = list_versions(skill_dir, ext=ext)
    if existing:
        try:
            prev_body = existing[0].path.read_text(
                encoding="utf-8", errors="replace",
            )
            if prev_body == body:
                return None
        except OSError:
            pass  # corrupted snapshot — fall through to write a fresh one.

    ts = datetime.now(timezone.utc).strftime(_TS_FORMAT)
    target = vdir / f"{ts}.{ext}"
    try:
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "version_history.write_failed file=%s err=%s", target, exc,
        )
        return None

    _prune(skill_dir, ext=ext)
    return target


def list_versions(
    skill_dir: Path, *, ext: str | None = None,
) -> list[VersionEntry]:
    """Return snapshots under ``<skill_dir>/.versions/`` newest-first.

    Filter to a single extension (e.g. ``"md"``, ``"py"``) via the
    ``ext`` argument — extensions are normalised lowercase, no leading
    dot. Returns empty when the directory doesn't exist (the typical
    state until the watcher first observes a change).
    """
    vdir = _versions_dir(skill_dir)
    if not vdir.is_dir():
        return []
    norm_ext = (ext or "").lower().lstrip(".") if ext else None
    out: list[VersionEntry] = []
    try:
        entries = list(vdir.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.is_file():
            continue
        if norm_ext and p.suffix.lstrip(".").lower() != norm_ext:
            continue
        # Filename shape: ``<ts>.<ext>`` — split at the LAST dot so
        # multi-dot extensions (e.g. ``foo.test.md``) don't poison the
        # parse. The ts string itself contains hyphens but no dots.
        stem, _, suffix = p.name.rpartition(".")
        ts = _parse_ts(stem)
        if ts is None:
            continue
        out.append(VersionEntry(
            path=p, ts=ts, index=0, ext=suffix.lower(),
        ))
    out.sort(key=lambda e: e.ts, reverse=True)
    # Stamp index after sort so caller can refer to "the i-th newest".
    return [
        VersionEntry(path=e.path, ts=e.ts, index=i, ext=e.ext)
        for i, e in enumerate(out)
    ]


def rollback(
    skill_dir: Path,
    live_file: Path,
    *,
    to_index: int = 0,
    snapshot_current: bool = True,
) -> Path | None:
    """Copy ``.versions/<ts>.<ext>`` into ``live_file``, restoring the
    snapshot.

    ``to_index`` selects the i-th newest entry (default 0, the most
    recent saved snapshot). ``snapshot_current=True`` (default) first
    captures the current live file content so the rollback itself is
    undoable.

    Returns the snapshot path that was restored, or ``None`` on any
    failure (no snapshot found at that index, IO error, etc.). NEVER
    raises — rollback is an emergency-recovery affordance and must
    not crash the caller.
    """
    if not live_file.is_file():
        log.warning(
            "version_history.rollback.no_live file=%s", live_file,
        )
        return None
    ext = live_file.suffix.lstrip(".") or "txt"
    versions = list_versions(skill_dir, ext=ext)
    if to_index < 0 or to_index >= len(versions):
        log.warning(
            "version_history.rollback.no_target dir=%s idx=%d total=%d",
            skill_dir, to_index, len(versions),
        )
        return None
    target = versions[to_index]
    try:
        body = target.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning(
            "version_history.rollback.read_failed file=%s err=%s",
            target.path, exc,
        )
        return None

    if snapshot_current:
        # Capture the current live content so the rollback itself is
        # undoable. Failure here is informational, not fatal.
        snapshot(skill_dir, live_file)

    try:
        live_file.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "version_history.rollback.write_failed file=%s err=%s",
            live_file, exc,
        )
        return None
    return target.path


def diff(
    skill_dir: Path,
    live_file: Path,
    *,
    against_index: int = 0,
    max_lines: int = 200,
) -> str | None:
    """Unified diff between live ``live_file`` content and the
    ``against_index``-th newest snapshot. Returns ``None`` when no
    snapshot exists at that index.

    Output is capped at ``max_lines`` to keep LLM context predictable;
    excess is replaced with ``[... diff truncated ...]``.
    """
    import difflib
    if not live_file.is_file():
        return None
    ext = live_file.suffix.lstrip(".") or "txt"
    versions = list_versions(skill_dir, ext=ext)
    if against_index < 0 or against_index >= len(versions):
        return None
    try:
        live_body = live_file.read_text(encoding="utf-8", errors="replace")
        prior_body = versions[against_index].path.read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    lines = list(difflib.unified_diff(
        prior_body.splitlines(keepends=False),
        live_body.splitlines(keepends=False),
        fromfile=f"snapshot:{versions[against_index].path.name}",
        tofile=f"live:{live_file.name}",
        n=3,
    ))
    if not lines:
        return ""  # identical content
    if len(lines) > max_lines:
        head = lines[: max_lines - 1]
        tail = (
            f"[... diff truncated, {len(lines) - len(head)} more "
            "lines; read the .versions/ file directly for the rest]"
        )
        return "\n".join([*head, tail])
    return "\n".join(lines)


def _prune(skill_dir: Path, *, ext: str) -> None:
    """Keep at most :data:`MAX_VERSIONS_PER_SKILL` snapshots per
    extension; drop the oldest above that. Logs the drops at debug
    level so operators can correlate "where did my v-old.md go?"."""
    versions = list_versions(skill_dir, ext=ext)
    if len(versions) <= MAX_VERSIONS_PER_SKILL:
        return
    to_drop = versions[MAX_VERSIONS_PER_SKILL:]
    for entry in to_drop:
        try:
            entry.path.unlink()
        except OSError:
            continue


def _parse_ts(stem: str) -> datetime | None:
    """Inverse of the ``_TS_FORMAT`` strftime."""
    try:
        return datetime.strptime(stem, _TS_FORMAT).replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


__all__ = [
    "VersionEntry",
    "VERSIONS_DIR_NAME",
    "MAX_VERSIONS_PER_SKILL",
    "snapshot",
    "list_versions",
    "rollback",
    "diff",
]
