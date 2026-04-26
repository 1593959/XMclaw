"""Cron API — list / create / delete scheduled jobs.

Mounted at ``/api/v2/cron``. Backs the Hermes-style CronPage.
Persists via :class:`xmclaw.core.scheduler.cron.CronStore` (jobs.json
under ``~/.xmclaw/cron/``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from starlette.responses import JSONResponse

from xmclaw.core.scheduler.cron import CronJob, CronStore

router = APIRouter(prefix="/api/v2/cron", tags=["cron"])

_store_singleton: CronStore | None = None


def _store() -> CronStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = CronStore()
    return _store_singleton


@router.get("")
async def list_jobs() -> JSONResponse:
    rows = [j.to_dict() for j in _store().list_jobs()]
    return JSONResponse({"jobs": rows})


@router.post("")
async def create_job(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    schedule = str(payload.get("schedule") or "").strip()
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "")
    if not schedule:
        return JSONResponse({"error": "schedule required"}, status_code=400)
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    job = CronJob(
        id=str(payload.get("id") or "").strip() or _new_id(),
        name=name,
        schedule=schedule,
        prompt=prompt,
        agent_id=str(payload.get("agent_id") or "main"),
        enabled=bool(payload.get("enabled", True)),
        wake_agent=bool(payload.get("wake_agent", True)),
    )
    try:
        saved = _store().add(job)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "job": saved.to_dict()})


@router.delete("/{job_id}")
async def delete_job(job_id: str) -> JSONResponse:
    if _store().remove(job_id):
        return JSONResponse({"ok": True, "job_id": job_id})
    return JSONResponse({"error": "not found"}, status_code=404)


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex
