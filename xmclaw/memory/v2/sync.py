"""Cross-instance memory sync — export / import / sync backends.

Wave-4 fix for M-6: enables multi-device continuity for self-hosted
XMclaw by providing a pluggable sync abstraction.

Backends:
  * FileExportSync — JSONL export/import to local filesystem
  * (Future) S3Sync — S3-compatible cloud backup
  * (Future) Mem0CloudSync — Mem0 Cloud API bridge

Usage:
    from xmclaw.memory.v2.sync import FileExportSync
    sync = FileExportSync("memory_backup.jsonl")
    await sync.push(facts, relations)
    facts, relations = await sync.pull(since=0)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class SyncBackend(Protocol):
    """Pluggable sync backend protocol."""

    async def push(
        self,
        facts: list[Any],
        relations: list[Any],
    ) -> bool:
        ...

    async def pull(
        self,
        since: float,
    ) -> tuple[list[Any], list[Any]]:
        ...


class FileExportSync:
    """Simplest sync: export/import facts + relations as JSONL.

    Each line is a JSON object with a ``_type`` discriminator:
      * ``{"_type": "fact", ...}`` — a Fact serialized via ``to_dict()``
      * ``{"_type": "relation", ...}`` — a Relation serialized via ``to_dict()``

    The file is append-only during ``push()`` and read sequentially
    during ``pull()``. No locking — intended for CLI-driven manual
    backup/restore, not concurrent daemon sync.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def push(
        self,
        facts: list[Any],
        relations: list[Any],
    ) -> bool:
        """Append facts and relations to the JSONL file."""
        try:
            with self._path.open("a", encoding="utf-8") as f:
                for fact in facts:
                    row = {"_type": "fact"}
                    row.update(fact.to_dict())
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                for rel in relations:
                    row = {"_type": "relation"}
                    row.update(rel.to_dict())
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).error(
                    "file_export_sync.push_failed path=%s err=%s",
                    self._path, exc,
                )
            except Exception:
                pass
            return False

    async def pull(
        self,
        since: float = 0.0,
    ) -> tuple[list[Any], list[Any]]:
        """Read facts and relations from the JSONL file.

        ``since`` filters by ``ts_last`` (facts) or ``ts`` (relations).
        Returns (facts, relations) as raw dicts — caller must reconstruct
        Fact / Relation objects.
        """
        facts: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        if not self._path.exists():
            return facts, relations

        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    row_type = row.pop("_type", None)
                    ts = row.get("ts_last", row.get("ts", 0.0))
                    if ts < since:
                        continue
                    if row_type == "fact":
                        facts.append(row)
                    elif row_type == "relation":
                        relations.append(row)
            return facts, relations
        except Exception as exc:  # noqa: BLE001
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).error(
                    "file_export_sync.pull_failed path=%s err=%s",
                    self._path, exc,
                )
            except Exception:
                pass
            return facts, relations


__all__ = [
    "SyncBackend",
    "FileExportSync",
]
