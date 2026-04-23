"""On-disk layout for backups.

Backups live at ``~/.xmclaw/backups/<name>/`` — each backup is a
directory containing ``archive.tar.gz`` + ``manifest.json``. Keeping
them as sibling files in a per-backup dir makes ``xmclaw backup list``
trivial (one directory = one backup) and ``xmclaw backup restore
<name>`` a clean index lookup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from xmclaw.backup.manifest import MANIFEST_NAME, Manifest

ARCHIVE_NAME = "archive.tar.gz"
"""Filename of the tarball inside each backup directory."""


def default_backups_dir() -> Path:
    """Root directory for all backups. Defaults to ``~/.xmclaw/backups``.

    Honors ``XMC_BACKUPS_DIR`` so tests can redirect to a tmp path
    without touching the real workspace.
    """
    override = os.environ.get("XMC_BACKUPS_DIR")
    if override:
        return Path(override)
    from xmclaw.utils.paths import data_dir

    return data_dir() / "backups"


@dataclass(frozen=True, slots=True)
class BackupEntry:
    """One backup on disk — its directory + loaded manifest."""

    name: str
    dir: Path
    manifest: Manifest

    @property
    def archive_path(self) -> Path:
        return self.dir / ARCHIVE_NAME

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME


def list_backups(backups_dir: Path | None = None) -> list[BackupEntry]:
    """Return every well-formed backup under ``backups_dir``.

    A well-formed backup is a subdirectory that contains *both*
    ``archive.tar.gz`` and a parseable ``manifest.json``. Malformed
    directories (missing one file, or json parse error) are silently
    skipped — they likely represent an aborted or hand-edited backup
    and surfacing them as errors would make ``backup list`` fragile.
    Results are sorted by ``created_ts`` ascending (oldest first).
    """
    root = backups_dir or default_backups_dir()
    if not root.exists():
        return []
    entries: list[BackupEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        archive = child / ARCHIVE_NAME
        manifest_path = child / MANIFEST_NAME
        if not (archive.is_file() and manifest_path.is_file()):
            continue
        try:
            manifest = Manifest.load(manifest_path)
        except (ValueError, OSError):
            # Corrupt manifest — don't blow up list(), just skip.
            continue
        entries.append(BackupEntry(name=child.name, dir=child, manifest=manifest))
    entries.sort(key=lambda e: e.manifest.created_ts)
    return entries


class BackupNotFoundError(RuntimeError):
    """Requested backup name does not resolve to a directory on disk."""


def get_backup(name: str, *, backups_dir: Path | None = None) -> BackupEntry:
    """Return the :class:`BackupEntry` for ``name`` or raise.

    Thin wrapper over :func:`list_backups` for the single-backup case —
    CLI verbs like ``backup info`` and ``backup verify`` need to look
    up by name and want a clear error when it's missing. Keeping the
    name-validation + lookup + manifest-load logic here means the CLI
    layer stays dumb.

    Args:
        name: Backup subdirectory name. Must be a plain directory
            name — no separators, no traversal — same rules as the
            create/delete codepaths.
        backups_dir: Override for the backups root.

    Raises:
        ValueError: ``name`` is structurally unsafe.
        BackupNotFoundError: No well-formed backup dir of that name
            exists (missing dir, missing tarball, unreadable manifest).
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"invalid backup name: {name!r}")
    root = backups_dir or default_backups_dir()
    target = root / name
    try:
        target.resolve(strict=True).relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        raise BackupNotFoundError(
            f"backup not found or outside backups dir: {name}"
        ) from exc
    archive = target / ARCHIVE_NAME
    manifest_path = target / MANIFEST_NAME
    if not (target.is_dir() and archive.is_file() and manifest_path.is_file()):
        raise BackupNotFoundError(f"backup not found or incomplete: {target}")
    try:
        manifest = Manifest.load(manifest_path)
    except (ValueError, OSError) as exc:
        raise BackupNotFoundError(
            f"backup manifest unreadable: {manifest_path}"
        ) from exc
    return BackupEntry(name=name, dir=target, manifest=manifest)


def delete_backup(name: str, *, backups_dir: Path | None = None) -> Path:
    """Remove the backup directory ``<backups_dir>/<name>`` in full.

    Args:
        name: Backup subdirectory name. Same validation as
            :func:`xmclaw.backup.create.create_backup` — must be a plain
            directory name, no separators.
        backups_dir: Override for the backups root.

    Returns:
        The deleted path (for the CLI to echo).

    Raises:
        BackupNotFoundError: The name doesn't resolve to an existing
            directory. We don't auto-create a "not found" success —
            callers should see the name they typed was wrong.
        ValueError: The name is structurally unsafe (path separator,
            traversal).  Protects against ``delete_backup("../etc")``.
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"invalid backup name: {name!r}")
    root = backups_dir or default_backups_dir()
    target = root / name
    # Refuse to follow any symlink that points outside ``root`` —
    # defense in depth against a ~/.xmclaw/backups/evil symlink pointing
    # at /. The normal happy path (plain subdirectory) passes this.
    try:
        target.resolve(strict=True).relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        raise BackupNotFoundError(
            f"backup not found or outside backups dir: {name}"
        ) from exc
    if not target.is_dir():
        raise BackupNotFoundError(f"backup not found: {target}")
    import shutil

    shutil.rmtree(target)
    return target


def prune_backups(
    *,
    backups_dir: Path | None = None,
    keep: int,
) -> list[str]:
    """Drop the oldest backups, keeping only the ``keep`` most recent.

    Args:
        backups_dir: Override for the backups root.
        keep: How many of the newest backups to retain. Must be ``>= 0``.
            ``keep=0`` deletes everything well-formed under the root;
            ``keep=1`` keeps the newest alone, etc.

    Returns:
        Names of the backups that were deleted, oldest first. Empty
        when nothing needed pruning.

    Raises:
        ValueError: ``keep`` is negative.

    Malformed backup directories (no manifest, corrupt json) are NOT
    touched — :func:`list_backups` never sees them, so neither does
    prune. That matches the "leave what you don't understand" rule in
    AGENTS.md hard no's.
    """
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    entries = list_backups(backups_dir)
    if len(entries) <= keep:
        return []
    # list_backups returns oldest-first, so the slice-from-start is the
    # batch to remove.
    to_remove = entries[: len(entries) - keep]
    removed: list[str] = []
    for entry in to_remove:
        import shutil

        shutil.rmtree(entry.dir)
        removed.append(entry.name)
    return removed
