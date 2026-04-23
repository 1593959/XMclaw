"""Backup manifest — the metadata sidecar :mod:`xmclaw.backup.create`
writes next to each ``.tar.gz`` and :mod:`xmclaw.backup.restore` reads.

Schema is deliberately frozen with a ``schema_version``. A backup made
by an older xmclaw must still be readable, so we write strictly this
shape and read permissively (extra fields are allowed and ignored).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
"""Filename used for the sidecar. Lives next to the tarball."""

MANIFEST_SCHEMA_VERSION = 1
"""Bump when the manifest shape changes. Restore rejects newer versions."""


@dataclass(frozen=True, slots=True)
class Manifest:
    """Backup metadata.

    Attributes:
        schema_version: Shape version (see :data:`MANIFEST_SCHEMA_VERSION`).
        name: User-facing label (e.g. ``auto-2026-04-23`` or a custom name).
        created_ts: Unix epoch seconds when the tarball finished writing.
        xmclaw_version: Version of the package that produced this backup.
        archive_sha256: Hex SHA-256 of the tarball. Restore verifies this.
        archive_bytes: Uncompressed byte size of the tarball on disk.
        source_dir: Absolute path of the workspace that was archived.
        excluded: Glob patterns that were skipped during archive creation.
        entries: Number of files included in the tarball (advisory, not
            a hard integrity check — the sha256 is the source of truth).
    """

    schema_version: int
    name: str
    created_ts: float
    xmclaw_version: str
    archive_sha256: str
    archive_bytes: int
    source_dir: str
    excluded: tuple[str, ...] = field(default_factory=tuple)
    entries: int = 0

    def to_json(self) -> str:
        payload = asdict(self)
        payload["excluded"] = list(payload["excluded"])
        return json.dumps(payload, indent=2, sort_keys=True)

    def write(self, dest: Path) -> None:
        dest.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Manifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        # Permissive read: only keep fields we know about, ignore extras.
        # This lets a newer manifest be read by an older xmclaw without
        # crashing — restore will still reject if schema_version is
        # actually higher than MANIFEST_SCHEMA_VERSION.
        known = {
            "schema_version", "name", "created_ts", "xmclaw_version",
            "archive_sha256", "archive_bytes", "source_dir",
            "excluded", "entries",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        if "excluded" in kwargs and isinstance(kwargs["excluded"], list):
            kwargs["excluded"] = tuple(kwargs["excluded"])
        return cls(**kwargs)
