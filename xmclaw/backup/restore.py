"""Restore a previously-created backup.

Restore semantics (Phase 1 — daemon-agnostic):

1. Verify the manifest's schema is one we can read.
2. Re-hash the tarball; reject if it drifted from ``manifest.archive_sha256``.
3. Extract into a staging directory (``<target>.restore-staging``).
4. Swap staging into place. Before the swap, if a target exists it is
   moved aside to ``<target>.prev-<ts>`` — **not** deleted. Roll-forward
   requires keeping the old copy for emergency rollback.

Phase 2 (Epic #20 step 5) will add ``daemon/reloader.py`` to stop the
daemon before the swap and restart it after. For now, the caller is
responsible for that — :func:`restore_backup` operates on filesystem
only. The CLI prints a reminder.
"""
from __future__ import annotations

import hashlib
import tarfile
import time
from pathlib import Path

from xmclaw.backup.manifest import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
)
from xmclaw.backup.store import ARCHIVE_NAME, default_backups_dir


class RestoreError(RuntimeError):
    """Restore failed (checksum mismatch, unknown schema, tar corrupt, …)."""


def restore_backup(
    name: str,
    target_dir: Path,
    *,
    backups_dir: Path | None = None,
    keep_previous: bool = True,
) -> Path:
    """Restore backup ``name`` into ``target_dir``.

    Args:
        name: Backup subdirectory name under ``backups_dir``.
        target_dir: Where to materialize the restored workspace. If it
            exists, it's moved aside to ``<target>.prev-<ts>`` when
            ``keep_previous=True`` (default) or deleted when False.
        backups_dir: Override for the backups root.
        keep_previous: When the target exists, preserve it under a
            timestamped sibling so a bad restore can be rolled back.

    Returns:
        The final ``target_dir`` that now contains the restored tree.
        (Same as the input; returned for chaining.)

    Raises:
        RestoreError: Backup missing, schema too new, checksum mismatch,
            tar corruption, or FS failure during extract/swap.
    """
    root = backups_dir or default_backups_dir()
    backup_dir = root / name
    archive_path = backup_dir / ARCHIVE_NAME
    manifest_path = backup_dir / MANIFEST_NAME

    if not backup_dir.is_dir():
        raise RestoreError(f"backup not found: {backup_dir}")
    if not archive_path.is_file():
        raise RestoreError(f"archive missing: {archive_path}")
    if not manifest_path.is_file():
        raise RestoreError(f"manifest missing: {manifest_path}")

    try:
        manifest = Manifest.load(manifest_path)
    except (ValueError, OSError) as exc:
        raise RestoreError(f"manifest parse failed: {exc}") from exc

    if manifest.schema_version > MANIFEST_SCHEMA_VERSION:
        raise RestoreError(
            f"backup manifest schema v{manifest.schema_version} is newer than "
            f"supported v{MANIFEST_SCHEMA_VERSION}; upgrade xmclaw to restore"
        )

    _verify_checksum(archive_path, expected=manifest.archive_sha256)

    staging = target_dir.with_name(target_dir.name + ".restore-staging")
    if staging.exists():
        _remove_tree(staging)
    staging.mkdir(parents=True)

    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            _safe_extract(tar, staging)
    except (tarfile.TarError, OSError) as exc:
        _remove_tree(staging)
        raise RestoreError(f"extract failed: {exc}") from exc

    # Atomic-ish swap: move target aside, then rename staging into place.
    # POSIX rename is atomic for same-filesystem moves; Windows permits
    # rename only to a non-existent name, which is why we move aside
    # rather than try to overwrite.
    prev_dir: Path | None = None
    if target_dir.exists():
        if keep_previous:
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            prev_dir = target_dir.with_name(f"{target_dir.name}.prev-{ts}")
            target_dir.rename(prev_dir)
        else:
            _remove_tree(target_dir)

    try:
        staging.rename(target_dir)
    except OSError as exc:
        # Rename failed after we already moved the old one aside — try
        # to put it back before erroring so the user isn't left with
        # nothing.
        if prev_dir is not None and prev_dir.exists():
            try:
                prev_dir.rename(target_dir)
            except OSError:
                pass
        raise RestoreError(f"final rename failed: {exc}") from exc

    return target_dir


def verify_backup(
    name: str,
    *,
    backups_dir: Path | None = None,
) -> Manifest:
    """Check that backup ``name``'s tarball hashes to ``manifest.archive_sha256``.

    Same integrity gate :func:`restore_backup` runs, exposed as a
    read-only operation so users can sanity-check a backup before
    moving storage tiers, or catch bit-rot on long-lived archives.

    Args:
        name: Backup subdirectory name under ``backups_dir``.
        backups_dir: Override for the backups root.

    Returns:
        The parsed :class:`Manifest` on success. Callers can inspect
        it (entry count, creation time, etc) without re-parsing.

    Raises:
        RestoreError: Manifest missing / unparseable, archive missing,
            schema newer than code can read, or sha256 drift.
    """
    root = backups_dir or default_backups_dir()
    backup_dir = root / name
    archive_path = backup_dir / ARCHIVE_NAME
    manifest_path = backup_dir / MANIFEST_NAME

    if not backup_dir.is_dir():
        raise RestoreError(f"backup not found: {backup_dir}")
    if not archive_path.is_file():
        raise RestoreError(f"archive missing: {archive_path}")
    if not manifest_path.is_file():
        raise RestoreError(f"manifest missing: {manifest_path}")
    try:
        manifest = Manifest.load(manifest_path)
    except (ValueError, OSError) as exc:
        raise RestoreError(f"manifest parse failed: {exc}") from exc
    if manifest.schema_version > MANIFEST_SCHEMA_VERSION:
        raise RestoreError(
            f"backup manifest schema v{manifest.schema_version} is newer "
            f"than supported v{MANIFEST_SCHEMA_VERSION}; upgrade xmclaw"
        )
    _verify_checksum(archive_path, expected=manifest.archive_sha256)
    return manifest


def _verify_checksum(archive: Path, *, expected: str) -> None:
    hasher = hashlib.sha256()
    with archive.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != expected:
        raise RestoreError(
            f"archive checksum mismatch: expected {expected}, got {actual}"
        )


def _safe_extract(tar: tarfile.TarFile, target: Path) -> None:
    """Extract ``tar`` into ``target`` after validating every member path
    stays inside the target — defense against tar-slip even though we
    control the archives. ``tarfile.extractall`` got ``filter="data"``
    support in 3.12; we keep 3.10+ compat with a manual check.
    """
    target_resolved = target.resolve()
    for member in tar.getmembers():
        dest = (target / member.name).resolve()
        try:
            dest.relative_to(target_resolved)
        except ValueError as exc:
            raise RestoreError(
                f"archive member escapes target: {member.name}"
            ) from exc
    tar.extractall(target)


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)
