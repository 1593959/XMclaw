"""B-390 (Sprint 2): Skill marketplace HTTP API.

Mounted at ``/api/v2/skills/marketplace`` (and a few siblings) so the web
UI Marketplace page can do the same things the ``xmclaw skill *`` CLI
does — browse the catalog, install / remove, list installed.

Endpoints:

* ``GET  /api/v2/skills/marketplace``        — return the parsed index.
  ``?refresh=1`` busts the cache.
* ``GET  /api/v2/skills/installed``          — list marketplace-installed
  skills.
* ``POST /api/v2/skills/install``            — body ``{"id": "..."}``;
  runs the same install flow as the CLI.
* ``DELETE /api/v2/skills/installed/{id}``   — uninstall.

Auth: pairing-token gate via :class:`xmclaw.daemon.middleware.PairingAuthMiddleware`,
same as every other ``/api/v2/*`` route. Nothing on this surface is
publicly readable.

Note on routing: the existing ``/api/v2/skills`` router (registry +
promote/rollback) is at ``xmclaw/daemon/routers/skills.py``. This module
mounts at ``/api/v2/skills`` too but with **distinct sub-paths**
(``/marketplace``, ``/installed``, ``/install``) so there's no collision
with the existing ``GET /api/v2/skills``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse


router = APIRouter(prefix="/api/v2/skills", tags=["skills", "marketplace"])


def _err(message: str, *, code: str = "marketplace_error", status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": message, "error_code": code},
        status_code=status,
    )


@router.get("/marketplace")
async def get_marketplace(request: Request) -> JSONResponse:
    """Return the curated marketplace index. Cached for 1h server-side;
    pass ``?refresh=1`` to force a re-fetch."""
    # Lazy import inside the handler so a stale ``import xmclaw.skills.marketplace``
    # at module-import time can't 500 the daemon if the marketplace
    # module itself has a parse error during a deploy.
    from xmclaw.skills.marketplace import MarketplaceError, fetch_index

    refresh_q = (request.query_params.get("refresh") or "").lower()
    refresh = refresh_q in ("1", "true", "yes")
    try:
        idx = fetch_index(refresh=refresh)
    except MarketplaceError as exc:
        return _err(str(exc), code=getattr(exc, "error_code", "index_fetch_failed"))
    return JSONResponse({
        "ok": True,
        "index": idx.to_dict(),
    })


@router.get("/installed")
async def list_installed_skills(request: Request) -> JSONResponse:
    """List skills installed via the marketplace.

    Distinct from ``GET /api/v2/skills`` (which lists every registered
    skill, including built-ins + manually-dropped user skills) — this
    endpoint reads ``~/.xmclaw/skills_user/.marketplace.json`` so the UI
    can distinguish "I installed this from the marketplace" from
    "this came with the install" or "I clone'd it by hand".
    """
    from xmclaw.skills.marketplace import list_installed

    rows = list_installed()
    return JSONResponse({
        "ok": True,
        "skills": [r.to_dict() for r in rows],
    })


@router.post("/install")
async def install_skill(request: Request) -> JSONResponse:
    """Install a skill by id. Body: ``{"id": "<skill_id>"}``.

    Returns 200 + the install report on success, 4xx on validation /
    scan / not-in-index. Same flow the ``xmclaw skill install`` CLI runs.
    """
    from xmclaw.skills.marketplace import (
        InstallScanFailed,
        InstallValidationError,
        MarketplaceError,
        SkillNotInIndexError,
        install,
    )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _err("invalid json", code="invalid_json")
    if not isinstance(body, dict):
        return _err("body must be a JSON object", code="invalid_body")
    skill_id = str(body.get("id") or "").strip()
    if not skill_id:
        return _err("'id' (string) required in body", code="missing_id")

    try:
        result = install(skill_id)
    except SkillNotInIndexError as exc:
        return _err(str(exc), code=exc.error_code, status=404)
    except InstallScanFailed as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "error_code": exc.error_code,
            "findings": exc.findings,
        }
        return JSONResponse(payload, status_code=400)
    except InstallValidationError as exc:
        return _err(str(exc), code=exc.error_code, status=400)
    except MarketplaceError as exc:
        return _err(str(exc), code=exc.error_code, status=400)

    return JSONResponse({
        "ok": True,
        "skill_id": result.skill_id,
        "version": result.version,
        "source": result.source,
        "install_path": str(result.install_path),
        "findings": result.findings,
    })


@router.delete("/installed/{skill_id}")
async def remove_skill(skill_id: str, request: Request) -> JSONResponse:
    """Uninstall a marketplace skill by id."""
    from xmclaw.skills.marketplace import remove

    removed = remove(skill_id)
    if not removed:
        return _err(
            f"{skill_id} was not installed",
            code="skill_not_installed",
            status=404,
        )
    return JSONResponse({"ok": True, "skill_id": skill_id, "removed": True})
