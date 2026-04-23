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
