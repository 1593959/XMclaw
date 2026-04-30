"""B-103: Backup router — Web UI parity with the ``xmclaw backup`` CLI.

Endpoints:
  GET    /api/v2/backup                         list all backups
  POST   /api/v2/backup                         create a new backup
  GET    /api/v2/backup/{name}                  manifest + integrity status
  DELETE /api/v2/backup/{name}                  delete one
  POST   /api/v2/backup/prune                   drop oldest beyond keep
  POST   /api/v2/backup/{name}/verify           sha256 integrity check
  POST   /api/v2/backup/{name}/restore          atomic swap with rollback

The backup module (xmclaw/backup/) is full-featured but had no router —
the CLI was the only way to trigger one. New users had no idea the
feature existed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.backup.create import BackupError, create_backup
from xmclaw.backup.restore import RestoreError, restore_backup, verify_backup
from xmclaw.backup.store import (
    BackupNotFoundError,
    delete_backup,
    get_backup,
    list_backups,
    prune_backups,
)
from xmclaw.utils.paths import data_dir

router = APIRouter(prefix="/api/v2/backup", tags=["backup"])


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """Render a BackupEntry as the JSON shape the UI expects."""
    m = entry.manifest
    return {
        "name": entry.name,
        "path": str(entry.path),
        "archive_path": str(entry.archive_path),
        "manifest_path": str(entry.manifest_path),
        "name_meta": getattr(m, "name", entry.name),
        "created_at": getattr(m, "created_at", None),
        "xmclaw_version": getattr(m, "xmclaw_version", None),
        "files_count": getattr(m, "files_count", 0),
        "total_bytes": getattr(m, "total_bytes", 0),
        "archive_sha256": getattr(m, "archive_sha256", None),
        "source_dir": getattr(m, "source_dir", None),
    }


@router.get("")
async def list_all() -> JSONResponse:
    """Return every backup under the configured backups dir, newest first."""
    try:
        backups = list_backups()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"backups": [], "error": str(exc)})
    return JSONResponse({
        "backups": [_entry_to_dict(b) for b in backups],
    })


@router.post("")
async def create(request: Request) -> JSONResponse:
    """Create a new backup. Body: ``{name?, overwrite?}``.

    When ``name`` is empty, a timestamp like ``auto-2026-04-30-153000``
    is generated. ``source_dir`` always uses :func:`data_dir` (the
    daemon's runtime workspace).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = str(body.get("name", "") or "").strip()
    overwrite = bool(body.get("overwrite", False))
    if not name:
        import time as _t
        name = _t.strftime("auto-%Y-%m-%d-%H%M%S", _t.localtime())
    try:
        manifest = create_backup(
            source_dir=data_dir(),
            name=name,
            overwrite=overwrite,
        )
    except BackupError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "name": manifest.name,
        "files_count": manifest.files_count,
        "total_bytes": manifest.total_bytes,
        "archive_sha256": manifest.archive_sha256,
    })


@router.get("/{name}")
async def info(name: str) -> JSONResponse:
    try:
        entry = get_backup(name)
    except BackupNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "backup not found"}, status_code=404,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse(_entry_to_dict(entry))


@router.delete("/{name}")
async def delete(name: str) -> JSONResponse:
    try:
        delete_backup(name)
    except BackupNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "backup not found"}, status_code=404,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({"ok": True})


@router.post("/prune")
async def prune(request: Request) -> JSONResponse:
    """Drop oldest beyond ``keep`` (default: from config ``backup.keep``).

    Body: ``{keep?: int, name_prefix?: str}``. Mirrors the CLI
    ``xmclaw backup prune --keep N`` behaviour.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = getattr(request.app.state, "config", None) or {}
    backup_section = cfg.get("backup") or {}
    try:
        keep = int(body.get("keep", backup_section.get("keep", 7)))
    except (TypeError, ValueError):
        keep = 7
    name_prefix = str(body.get("name_prefix", backup_section.get("name_prefix", "auto-")))
    try:
        removed = prune_backups(keep=keep, name_prefix=name_prefix)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "removed_count": len(removed),
        "removed": [str(p) for p in removed],
        "keep": keep,
        "name_prefix": name_prefix,
    })


@router.post("/{name}/verify")
async def verify(name: str) -> JSONResponse:
    """Verify a backup's archive sha256 against its manifest."""
    try:
        ok = verify_backup(name)
    except BackupNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "backup not found"}, status_code=404,
        )
    except RestoreError as exc:
        return JSONResponse({"ok": False, "verified": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({"ok": True, "verified": bool(ok)})


@router.post("/{name}/restore")
async def restore(name: str) -> JSONResponse:
    """Atomic-swap restore. The current ``data_dir`` is renamed to
    ``data_dir.prev-<ts>`` before extraction; on failure the previous
    state is rolled back. Daemon restart is needed for the restored
    state to fully apply (events.db, sessions.db etc are all loaded
    in lifespan)."""
    try:
        restore_backup(name=name, target_dir=data_dir())
    except BackupNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "backup not found"}, status_code=404,
        )
    except RestoreError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "restart_required": True,
        "note": "重启 daemon 以加载恢复后的 events.db / sessions.db",
    })
