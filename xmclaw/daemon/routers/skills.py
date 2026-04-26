"""Skills API — enumerate registered skills with source classification.

Mounted at ``/api/v2/skills``. Backs the web-UI Skills page so it can
show "built-in vs user-installed" tagging (the categorization the
peers — Hermes especially — use to gate trust + display).

Reads :class:`xmclaw.skills.registry.SkillRegistry` via the orchestrator
on ``app.state.orchestrator``. Returns one row per ``(skill_id, version)``
plus the active HEAD version flag.

When the orchestrator is disabled (``evolution.enabled=false``) or no
skills have been registered yet, the response is ``{"skills": []}`` —
that's a valid first-install state, not an error.
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
