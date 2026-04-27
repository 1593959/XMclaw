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


@router.post("/{job_id}/pause")
async def pause_job(job_id: str) -> JSONResponse:
    """Mark a cron job paused (CronJob.enabled=False).

    The CronTickTask filters by enabled, so a paused job is silently
    skipped on every tick until resumed. Idempotent: pausing an already-
    paused job is a no-op.
    """
    s = _store()
    job = s.get(job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not job.enabled:
        return JSONResponse({"ok": True, "state": "paused", "noop": True})
    s._jobs[job_id] = job.with_updates(enabled=False)
    s._dirty = True
    s._save()
    return JSONResponse({"ok": True, "state": "paused"})


@router.post("/{job_id}/resume")
async def resume_job(job_id: str) -> JSONResponse:
    """Re-enable a paused cron job.

    Re-computes ``next_run_at`` from now so a job that was paused for
    days doesn't immediately fire on resume — same posture as cron
    daemons that don't backfill missed slots while disabled.
    """
    import time as _t
    from xmclaw.core.scheduler.cron import parse_schedule
    s = _store()
    job = s.get(job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job.enabled:
        return JSONResponse({"ok": True, "state": "running", "noop": True})
    try:
        next_at = parse_schedule(job.schedule, now=_t.time())
    except ValueError:
        next_at = _t.time() + 3600
    s._jobs[job_id] = job.with_updates(enabled=True, next_run_at=next_at)
    s._dirty = True
    s._save()
    return JSONResponse({"ok": True, "state": "running"})


@router.post("/{job_id}/trigger")
async def trigger_job(job_id: str) -> JSONResponse:
    """Force a cron job to run on the next tick.

    Sets ``next_run_at`` to ``now`` so the next CronTickTask scan picks
    it up. Doesn't run synchronously — the tick is the canonical
    execution path; mirroring it inline would require hooking the bus
    and toolset wiring twice. Returns ``next_run_at`` so the UI can
    show a 'firing in <X>s' indicator if it wants.
    """
    import time as _t
    s = _store()
    job = s.get(job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    s._jobs[job_id] = job.with_updates(next_run_at=_t.time(), enabled=True)
    s._dirty = True
    s._save()
    return JSONResponse({"ok": True, "triggered_at": _t.time()})


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex
