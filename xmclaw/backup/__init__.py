"""Backup + restore for the ``~/.xmclaw/`` workspace (Epic #20).

Public surface:

* :func:`create_backup` — tar.gz the workspace minus logs + transient
  caches. Writes a manifest alongside so restore can verify integrity.
* :func:`list_backups` — enumerate backups on disk with their manifests.
* :func:`restore_backup` — validate checksum, extract into a staging
  directory, swap it into place. Non-atomic on Windows by design (see
  :mod:`xmclaw.backup.restore` for the swap semantics).

The CLI (``xmclaw backup ...``) lives in ``xmclaw.cli.backup`` and is
a thin shell over these entry points.
"""
from __future__ import annotations

from xmclaw.backup.create import create_backup
from xmclaw.backup.manifest import Manifest, MANIFEST_NAME, MANIFEST_SCHEMA_VERSION
from xmclaw.backup.restore import RestoreError, restore_backup, verify_backup
from xmclaw.backup.store import (
    BackupNotFoundError,
    default_backups_dir,
    delete_backup,
    get_backup,
    list_backups,
    prune_backups,
)

__all__ = [
    "BackupNotFoundError",
    "Manifest",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "RestoreError",
    "create_backup",
    "default_backups_dir",
    "delete_backup",
    "get_backup",
    "list_backups",
    "prune_backups",
    "restore_backup",
    "verify_backup",
]
