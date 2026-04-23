"""Create a backup archive of the ``~/.xmclaw/`` workspace.

Design choices:

* **tar.gz, not zip** — preserves POSIX permissions for `pairing_token.txt`
  (0600). Windows doesn't enforce the bits on restore but they round-trip
  cleanly back to a POSIX host.

* **Exclude defaults** — the three classes of data that shouldn't land in
  a portable backup: ``logs/`` (grows unbounded, not load-bearing), the
  PID/meta files (machine-specific process state), and Python bytecode
  caches (trivially regenerated). Callers may pass extra ``excluded``
  patterns to extend the default list.

* **One-pass sha256** — computed while writing the tarball so we don't
  have to re-read the finished archive. Caller gets a verified manifest
  as part of the return value.

* **Atomic publish** — we write the tarball + manifest under a
  ``<name>.tmp`` directory, then rename to the final name. A crash
  mid-write leaves a ``.tmp`` the caller can clean up without risking
  a half-written backup masquerading as a valid one.
"""
from __future__ import annotations

import fnmatch
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

DEFAULT_EXCLUDED: tuple[str, ...] = (
    "logs",
    "logs/*",
    "*/__pycache__",
    "*/__pycache__/*",
    "daemon.pid",
    "daemon.meta",
    "daemon.log",
    "*/daemon.pid",
    "*/daemon.meta",
    "*/daemon.log",
    "*.pid",
    "*.tmp",
)
"""Glob patterns (fnmatch) applied relative to ``source_dir``. If any
pattern matches a path, it is skipped during archive creation."""


class BackupError(RuntimeError):
    """Failed to create a backup (source missing, name collision, …)."""


def _is_excluded(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """True if ``rel_path`` (posix-style, relative to source) matches any
    exclude pattern. We normalize to forward slashes so the same pattern
    list works on Windows and POSIX hosts without per-platform rewrites.
    """
    rel = rel_path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        # Also treat a bare dirname pattern as matching everything under it.
        if "/" not in pat and rel.startswith(pat + "/"):
            return True
    return False


def _xmclaw_version() -> str:
    """Current installed version; used in manifest. Falls back to "unknown"
    when package metadata is missing (editable install without setup)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("xmclaw")
    except PackageNotFoundError:  # pragma: no cover — editable install edge
        return "unknown"
    except Exception:  # pragma: no cover — defensive
        return "unknown"


def create_backup(
    source_dir: Path,
    name: str,
    *,
    backups_dir: Path | None = None,
    excluded: tuple[str, ...] = DEFAULT_EXCLUDED,
    overwrite: bool = False,
) -> Manifest:
    """Create ``<backups_dir>/<name>/{archive.tar.gz, manifest.json}``.

    Args:
        source_dir: The workspace to archive (typically ``~/.xmclaw``).
        name: Destination subdirectory name. Must not contain path
            separators — plain directory name.
        backups_dir: Where the backup lives. Defaults to
            :func:`default_backups_dir`.
        excluded: Glob patterns (relative to ``source_dir``) to skip.
            Pass ``()`` for "archive everything".
        overwrite: If False (default) and ``name`` already exists, raise
            :class:`BackupError` rather than clobber. True is "I know
            what I'm doing, replace it".

    Returns:
        The :class:`Manifest` that was written. Tests use this to assert
        on counts and checksums without re-parsing the JSON.

    Raises:
        BackupError: Source missing, name invalid, or destination exists
            with ``overwrite=False``.
    """
    if not source_dir.exists():
        raise BackupError(f"source directory does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise BackupError(f"source is not a directory: {source_dir}")
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise BackupError(f"invalid backup name: {name!r}")

    root = backups_dir or default_backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    final_dir = root / name
    if final_dir.exists():
        if not overwrite:
            raise BackupError(
                f"backup {name!r} already exists at {final_dir}; "
                "pass overwrite=True to replace"
            )
        # Caller accepted overwrite — nuke it before re-creating.
        _remove_tree(final_dir)

    staging = root / f"{name}.tmp"
    if staging.exists():
        _remove_tree(staging)
    staging.mkdir(parents=True)

    archive_path = staging / ARCHIVE_NAME
    manifest_path = staging / MANIFEST_NAME

    hasher = hashlib.sha256()
    entry_count = 0

    # tarfile doesn't expose a write-and-hash pipe natively; we write to
    # disk, then hash in one pass. A genuine workspace is tens of MiB —
    # one extra read is cheap and keeps the code straightforward.
    with tarfile.open(archive_path, mode="w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            rel = path.relative_to(source_dir)
            rel_posix = rel.as_posix()
            if _is_excluded(rel_posix, excluded):
                continue
            # tarfile's filter recurses but we want the sorted iteration
            # to produce a reproducible archive; arcname must be posix.
            tar.add(path, arcname=rel_posix, recursive=False)
            if path.is_file():
                entry_count += 1

    # Second pass: compute checksum.
    with archive_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)

    archive_bytes = archive_path.stat().st_size
    manifest = Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        name=name,
        created_ts=time.time(),
        xmclaw_version=_xmclaw_version(),
        archive_sha256=hasher.hexdigest(),
        archive_bytes=archive_bytes,
        source_dir=str(source_dir.resolve()),
        excluded=tuple(excluded),
        entries=entry_count,
    )
    manifest.write(manifest_path)

    # Atomic publish — rename staging to the final name. Path.rename on
    # Windows refuses to clobber; we already removed final_dir above.
    staging.rename(final_dir)
    return manifest


def _remove_tree(path: Path) -> None:
    """Recursive delete. ``shutil.rmtree`` isn't imported at module
    scope to keep startup cheap for callers who only list backups."""
    import shutil

    shutil.rmtree(path)
