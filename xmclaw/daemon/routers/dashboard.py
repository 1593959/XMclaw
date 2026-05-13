"""Dashboard aggregator API (Sprint 2 Wave 6).

Single-shot overview for the Dashboard page. Mounted at
``/api/v2/dashboard``. The page makes ONE GET per refresh tick instead
of 5+ per-domain calls because:

  * Refresh is high-cadence (~10s) and N round-trips would feel laggy
    on mobile / flaky nets.
  * Each per-domain read is a tiny SQLite query — the round-trip count
    is what costs, not the data.
  * The page is a "summary card" view, not a deep dive. Counts + 3-5
    most-recent rows is enough; drill-down lives on the per-domain
    pages (/cognition, /memory, /sessions, /tasks, etc).

Endpoint:

  * ``GET /api/v2/dashboard/overview`` — heterogeneous JSON with one
    sub-object per surfaced subsystem. Each sub-object is best-effort:
    if its source isn't wired, that key is ``None`` (UI shows
    "未启用" badge instead of breaking the whole page).

Implementation notes:

  * Every read is wrapped in try/except so a single failing subsystem
    doesn't 500 the whole dashboard. The UI displays whatever did
    return.
  * No new state stored. We read what's already on ``app.state`` from
    lifespan and shape it for display.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.log import get_logger

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])
_log = get_logger(__name__)


# ── helpers ───────────────────────────────────────────────────────


def _state(request: Request) -> Any:
    return getattr(request.app, "state", None)


def _uptime_block(st: Any) -> dict[str, Any]:
    """Daemon process uptime + boot duration."""
    out: dict[str, Any] = {
        "boot_ts": None,
        "uptime_s": None,
        "startup_duration_s": None,
        "version": None,
    }
    try:
        boot_ts = getattr(st, "boot_ts", None)
        if boot_ts is not None:
            out["boot_ts"] = float(boot_ts)
            out["uptime_s"] = round(time.time() - float(boot_ts), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        d = getattr(st, "lifespan_startup_duration_s", None)
        if d is not None:
            out["startup_duration_s"] = float(d)
    except Exception:  # noqa: BLE001
        pass
    try:
        from xmclaw import __version__ as _v
        out["version"] = str(_v)
    except Exception:  # noqa: BLE001
        pass
    return out


def _proactive_block(st: Any) -> dict[str, Any] | None:
    """Snapshot of ProactiveAgent state."""
    pa = getattr(st, "proactive_agent", None)
    if pa is None:
        return None
    try:
        last_proposal_ts = getattr(pa, "_last_proposal_ts", 0.0) or 0.0
        last_user_ts = getattr(pa, "_last_user_message_ts", None)
        last_agent_ts = getattr(pa, "_last_agent_message_ts", None)
        triggers = []
        for t in getattr(pa, "_triggers", []):
            triggers.append({
                "name": getattr(t, "name", "?"),
                "cooldown_s": float(getattr(t, "cooldown_s", 0.0)),
            })
        try:
            quiet = bool(pa._is_quiet_hours_active())
        except Exception:  # noqa: BLE001
            quiet = False
        return {
            "triggers": triggers,
            "last_proposal_ts": float(last_proposal_ts) or None,
            "last_user_ts": (
                float(last_user_ts) if last_user_ts is not None else None
            ),
            "last_agent_ts": (
                float(last_agent_ts) if last_agent_ts is not None else None
            ),
            "tick_interval_s": float(
                getattr(pa, "_tick_interval_s", 30.0),
            ),
            "quiet_hours_active": quiet,
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("dashboard.proactive_block_failed err=%s", exc)
        return {"error": str(exc)}


def _autobio_block(st: Any) -> dict[str, Any] | None:
    """Snapshot of the AutobiographicalMemory."""
    mem = getattr(st, "autobio_memory", None)
    if mem is None:
        return None
    try:
        people = mem.people(limit=5)
        projects = mem.projects(limit=5)
        return {
            "people_count": len(mem.people(limit=200)),
            "project_count": len(mem.projects(limit=200)),
            "recent_people": [
                {
                    "name": p.name,
                    "relationship": p.relationship,
                    "importance": round(p.importance, 2),
                }
                for p in people
            ],
            "recent_projects": [
                {
                    "name": pr.name,
                    "status": pr.status,
                    "current_focus": pr.current_focus,
                    "last_touch_ts": pr.last_touch_ts,
                }
                for pr in projects
            ],
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("dashboard.autobio_block_failed err=%s", exc)
        return {"error": str(exc)}


def _cognition_block(st: Any) -> dict[str, Any] | None:
    """Snapshot of the live CognitiveState."""
    cs = getattr(st, "cognitive_state", None)
    if cs is None:
        agent = getattr(st, "agent", None)
        cs = getattr(agent, "_cognitive_state", None) if agent else None
    if cs is None:
        return None
    try:
        goals = getattr(cs, "current_goals", [])
        focus = getattr(cs, "attention_focus", [])
        return {
            "goal_count": len(goals),
            "active_goals": [
                {
                    "id": g.id,
                    "description": g.description,
                    "priority": g.priority,
                    "status": g.status,
                }
                for g in list(goals)[:5]
            ],
            "attention_count": len(focus),
            "salience_threshold": getattr(cs, "salience_threshold", None),
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("dashboard.cognition_block_failed err=%s", exc)
        return {"error": str(exc)}


def _suggestions_block(st: Any) -> dict[str, Any] | None:
    """Pending suggestion inbox (R5)."""
    inbox = getattr(st, "suggestion_inbox", None)
    if inbox is None:
        return None
    try:
        # SuggestionInbox exposes ``recent`` / ``pending`` / ``list``
        # depending on version; probe in order.
        pending = None
        for attr in ("pending", "list_pending", "recent"):
            fn = getattr(inbox, attr, None)
            if callable(fn):
                pending = fn() if attr != "recent" else fn(status="pending")
                break
        if pending is None:
            return {"pending_count": 0, "recent": []}
        items = list(pending)[:5]
        return {
            "pending_count": len(list(pending)),
            "recent": [
                {
                    "id": getattr(s, "id", None),
                    "text": (getattr(s, "text", "") or "")[:120],
                    "urgency": getattr(s, "urgency", "normal"),
                    "created_ts": getattr(s, "created_ts", None),
                }
                for s in items
            ],
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("dashboard.suggestions_block_failed err=%s", exc)
        return {"error": str(exc)}


def _tasks_block(st: Any) -> dict[str, Any] | None:
    """Task scheduler queue counts (R2)."""
    sched = getattr(st, "task_scheduler", None)
    if sched is None:
        return None
    try:
        # TaskScheduler usually exposes ``list_tasks`` or similar.
        listing = None
        for attr in ("list_tasks", "tasks", "all_tasks"):
            fn = getattr(sched, attr, None)
            if callable(fn):
                listing = fn()
                break
            if attr == "tasks" and isinstance(fn, list):
                listing = fn
                break
        if listing is None:
            return {"total": 0, "by_status": {}}
        items = list(listing)
        by_status: dict[str, int] = {}
        for t in items:
            s = (getattr(t, "status", None) or "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total": len(items),
            "by_status": by_status,
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("dashboard.tasks_block_failed err=%s", exc)
        return {"error": str(exc)}


def _storage_block(st: Any) -> dict[str, Any]:
    """File sizes for the three SQLite stores that matter."""
    from xmclaw.utils.paths import data_dir
    base = data_dir() / "v2"
    out: dict[str, Any] = {}

    def _maybe_size(p: Path) -> int | None:
        try:
            return p.stat().st_size if p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    out["events_db_bytes"] = _maybe_size(base / "events.db")
    out["memory_db_bytes"] = _maybe_size(base / "memory.db")
    out["autobio_db_bytes"] = _maybe_size(base / "autobiographical.db")
    out["data_dir"] = str(base)
    return out


# ── endpoint ──────────────────────────────────────────────────────


@router.get("/overview")
async def overview(request: Request) -> JSONResponse:
    """Return the dashboard snapshot.

    Every sub-block is best-effort: a single missing subsystem returns
    ``None`` for that key, the rest still render. The shape:

        {
          "now":          float (server epoch),
          "uptime":       {boot_ts, uptime_s, startup_duration_s, version},
          "proactive":    {triggers, last_proposal_ts, ...} | null,
          "autobio":      {people_count, project_count, ...} | null,
          "cognition":    {goal_count, active_goals, ...} | null,
          "suggestions":  {pending_count, recent} | null,
          "tasks":        {total, by_status} | null,
          "storage":      {events_db_bytes, ...}
        }
    """
    st = _state(request)
    if st is None:
        return JSONResponse({"error": "no_app_state"}, status_code=503)
    payload: dict[str, Any] = {
        "now": time.time(),
        "uptime": _uptime_block(st),
        "proactive": _proactive_block(st),
        "autobio": _autobio_block(st),
        "cognition": _cognition_block(st),
        "suggestions": _suggestions_block(st),
        "tasks": _tasks_block(st),
        "storage": _storage_block(st),
    }
    return JSONResponse(payload)
