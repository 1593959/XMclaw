"""Feature-flag REST API. Wave-32+ (2026-05-18).

Endpoints (all under ``/api/v2/features``):

  * ``GET    /``           — snapshot of every registered flag with
                             value + resolved layer + description.
  * ``GET    /{name}``     — single-flag detail.
  * ``PUT    /{name}``     — set a value (body: {"value": ..., "persist":
                             true|false}). persist=true → memory + disk.
  * ``DELETE /{name}``     — clear local overrides (memory + disk).
                             Remote / default value returns afterwards.
  * ``POST   /refresh``    — pull remote provider for every registered
                             flag. Body: {"names": [...]} (optional).

Pairing-token gated by the standard middleware. Read-only for now —
the operator UI uses GET / for the flag dashboard; writes are still
typically via env var for production safety, but the PUT route lets
you flip a flag from the Web UI without restarting the daemon.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from starlette.responses import JSONResponse

from xmclaw.core.feature_flags import default_engine

router = APIRouter(prefix="/api/v2/features", tags=["features"])


@router.get("")
async def list_features(request: Request) -> JSONResponse:
    engine = getattr(
        getattr(request.app, "state", None),
        "feature_engine",
        None,
    ) or default_engine()
    return JSONResponse({"flags": list(engine.snapshot().values())})


@router.get("/{name}")
async def get_feature(name: str, request: Request) -> JSONResponse:
    engine = getattr(
        getattr(request.app, "state", None),
        "feature_engine",
        None,
    ) or default_engine()
    snap = engine.snapshot()
    if name in snap:
        return JSONResponse(snap[name])
    # Unregistered flag — still return the current resolved value
    # (env / memory / disk / remote may know about it) so callers
    # can inspect ad-hoc overrides too.
    return JSONResponse({
        "name": name,
        "value": engine.variant(name),
        "layer": "unknown",
        "description": "",
    })


@router.put("/{name}")
async def set_feature(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
) -> JSONResponse:
    if "value" not in body:
        return JSONResponse(
            {"error": "body must include 'value'"}, status_code=400,
        )
    persist = bool(body.get("persist", True))
    engine = getattr(
        getattr(request.app, "state", None),
        "feature_engine",
        None,
    ) or default_engine()
    engine.set(name, body["value"], persist=persist)
    return JSONResponse({
        "ok": True,
        "name": name,
        "value": engine.variant(name),
        "persisted": persist,
    })


@router.delete("/{name}")
async def clear_feature(name: str, request: Request) -> JSONResponse:
    engine = getattr(
        getattr(request.app, "state", None),
        "feature_engine",
        None,
    ) or default_engine()
    engine.clear(name)
    return JSONResponse({"ok": True, "name": name})


@router.post("/refresh")
async def refresh_features(
    request: Request,
    body: dict[str, Any] | None = Body(None),
) -> JSONResponse:
    engine = getattr(
        getattr(request.app, "state", None),
        "feature_engine",
        None,
    ) or default_engine()
    names = None
    if isinstance(body, dict):
        raw = body.get("names")
        if isinstance(raw, list):
            names = [str(n) for n in raw if isinstance(n, str)]
    count = engine.refresh(names)
    return JSONResponse({"ok": True, "refreshed": count})


__all__ = ["router"]
