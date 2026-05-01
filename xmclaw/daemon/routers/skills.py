"""Skills API — enumerate registered skills + manual head-version control.

Mounted at ``/api/v2/skills``. Backs the web-UI Skills page so it can
show "built-in vs user-installed" tagging (the categorization the
peers — Hermes especially — use to gate trust + display).

Reads :class:`xmclaw.skills.registry.SkillRegistry` via the orchestrator
on ``app.state.orchestrator``. Returns one row per ``(skill_id, version)``
plus the active HEAD version flag.

When the orchestrator is disabled (``evolution.enabled=false``) or no
skills have been registered yet, the response is ``{"skills": []}`` —
that's a valid first-install state, not an error.

B-114 added manual control endpoints:
  POST /api/v2/skills/{skill_id}/promote   {to_version, evidence: [...]}
  POST /api/v2/skills/{skill_id}/rollback  {to_version, reason}
  GET  /api/v2/skills/{skill_id}/history   list of PromotionRecord rows
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/skills", tags=["skills"])


def _classify_source(skill: Any) -> str:
    """Tag a skill as 'built-in' / 'user' / 'unknown' from its module path.

    A skill class shipped inside the ``xmclaw.skills.*`` package is a
    built-in (lives in the wheel). Anything else is user-installed
    (registered at runtime from the user's environment).
    """
    cls = type(skill)
    mod = getattr(cls, "__module__", "") or ""
    if mod.startswith("xmclaw.skills."):
        return "built-in"
    if mod:
        return "user"
    return "unknown"


@router.get("")
async def list_skills(request: Request) -> JSONResponse:
    """Return registered skills grouped by ``skill_id``.

    Response shape::

        {
          "skills": [
            {
              "id": "summarize",
              "head_version": 3,
              "source": "built-in",
              "versions": [
                {"version": 1, "is_head": false, "manifest": {...}},
                {"version": 3, "is_head": true,  "manifest": {...}}
              ]
            }
          ]
        }
    """
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return JSONResponse({"skills": [], "evolution_enabled": False})

    registry = orch.registry
    rows: list[dict[str, Any]] = []
    for sid in sorted(registry.list_skill_ids()):
        head = registry.active_version(sid)
        versions: list[dict[str, Any]] = []
        source = "unknown"
        for v in registry.list_versions(sid):
            try:
                ref = registry.ref(sid, v)
                skill_obj = registry.get(sid, v)
            except Exception:  # noqa: BLE001 — never let one bad row 500 the listing
                continue
            if source == "unknown":
                source = _classify_source(skill_obj)
            manifest = ref.manifest
            manifest_dict: dict[str, Any]
            if hasattr(manifest, "to_dict"):
                manifest_dict = manifest.to_dict()
            elif hasattr(manifest, "__dict__"):
                manifest_dict = {k: v for k, v in manifest.__dict__.items() if not k.startswith("_")}
            else:
                manifest_dict = {}
            versions.append({
                "version": v,
                "is_head": v == head,
                "manifest": manifest_dict,
            })
        rows.append({
            "id": sid,
            "head_version": head,
            "source": source,
            "versions": versions,
        })
    return JSONResponse({"skills": rows, "evolution_enabled": True})


def _record_to_dict(r: Any) -> dict[str, Any]:
    """Render a PromotionRecord as JSON. Tolerates both dataclass and
    plain-object shapes so a future refactor doesn't 500 the listing."""
    base = {
        "kind": getattr(r, "kind", ""),
        "skill_id": getattr(r, "skill_id", ""),
        "from_version": getattr(r, "from_version", 0),
        "to_version": getattr(r, "to_version", 0),
        "ts": getattr(r, "ts", 0),
        "source": getattr(r, "source", "manual"),  # B-121
    }
    if getattr(r, "evidence", None):
        base["evidence"] = list(r.evidence)
    if getattr(r, "reason", None):
        base["reason"] = r.reason
    return base


@router.post("/{skill_id}/promote")
async def promote(skill_id: str, request: Request) -> JSONResponse:
    """B-114: manually promote a skill to a specific version.

    Anti-req #12 enforced by SkillRegistry.promote() — non-empty
    ``evidence`` list is REQUIRED. The UI must surface a confirmation
    flow that captures why the user is making the call (typically a
    bench result snippet or grader verdict).
    """
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return JSONResponse(
            {"ok": False, "error": "evolution disabled — no orchestrator wired"},
            status_code=400,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    try:
        to_version = int(body.get("to_version"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "to_version (int) required"},
            status_code=400,
        )
    raw_evidence = body.get("evidence") or []
    evidence = [
        str(e).strip() for e in raw_evidence if str(e).strip()
    ] if isinstance(raw_evidence, list) else []
    if not evidence:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "anti-req #12: evidence (non-empty list[str]) is required. "
                    "Manual promotions must justify themselves."
                ),
            },
            status_code=400,
        )
    try:
        record = orch.registry.promote(
            skill_id, to_version, evidence=evidence,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )
    return JSONResponse({
        "ok": True,
        "record": _record_to_dict(record),
        "head_version": orch.registry.active_version(skill_id),
    })


@router.post("/{skill_id}/rollback")
async def rollback(skill_id: str, request: Request) -> JSONResponse:
    """B-114: manually roll a skill back to an earlier version.

    SkillRegistry.rollback() requires a non-empty ``reason``. Same
    motivation as anti-req #12 promotion-evidence rule — auditable
    history of who rolled back why."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return JSONResponse(
            {"ok": False, "error": "evolution disabled — no orchestrator wired"},
            status_code=400,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    try:
        to_version = int(body.get("to_version"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "to_version (int) required"},
            status_code=400,
        )
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return JSONResponse(
            {
                "ok": False,
                "error": "reason (non-empty string) is required for rollback.",
            },
            status_code=400,
        )
    try:
        record = orch.registry.rollback(
            skill_id, to_version, reason=reason,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )
    return JSONResponse({
        "ok": True,
        "record": _record_to_dict(record),
        "head_version": orch.registry.active_version(skill_id),
    })


@router.get("/{skill_id}/history")
async def history(skill_id: str, request: Request) -> JSONResponse:
    """B-114: return the promote/rollback history for a skill_id."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        return JSONResponse({"records": [], "evolution_enabled": False})
    try:
        records = orch.registry.history(skill_id)
    except Exception:  # noqa: BLE001
        records = []
    return JSONResponse({
        "records": [_record_to_dict(r) for r in records],
        "skill_id": skill_id,
    })
