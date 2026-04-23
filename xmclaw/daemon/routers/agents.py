"""Agents API — CRUD for the running :class:`MultiAgentManager` registry.

Epic #17 Phase 3. Mounted at ``/api/v2/agents``. Unlike Epic #18's
``/api/v2/workspaces`` router (which edits abstract user presets),
this one drives live daemon state: each POST instantiates a fresh
``Workspace`` + AgentLoop and registers it in
``app.state.agents``. The primary agent — the one built from the
top-level daemon config and hanging off ``app.state.agent`` — is
NOT exposed through this surface; it has id ``"main"`` reserved and
is routed to by WS clients that omit the ``agent_id`` query param.

Why a distinct surface from ``/workspaces``? Presets are
"configurations the user might launch"; agents are "configurations
currently running". The lifecycle, sensitivity, and wipe semantics
differ (see docstrings on :func:`agents_registry_dir` /
:func:`workspaces_dir`). Two routers keep those concerns from
contaminating each other.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.multi_agent_manager import (
    AgentIdError,
    MultiAgentManager,
    _sanitize_id,
)

router = APIRouter(prefix="/api/v2/agents", tags=["agents"])


_RESERVED_IDS = frozenset({"main"})


def _manager(request: Request) -> MultiAgentManager | None:
    """Pull the manager off ``app.state``.

    Returns None in echo-mode apps (``create_app()`` with no config)
    where no manager was wired in. Routes branch on that so tests
    that don't care about multi-agent don't need a manager fixture.
    """
    return getattr(request.app.state, "agents", None)


def _workspace_summary(agent_id: str, ws, *, is_primary: bool) -> dict:
    return {
        "agent_id": agent_id,
        "ready": ws.is_ready() if ws is not None else True,
        "primary": is_primary,
    }


@router.get("")
async def list_agents(request: Request) -> JSONResponse:
    """Return every registered agent, with the primary flagged.

    The primary — the config-built ``app.state.agent`` — is emitted
    synthetically so UIs can show it alongside user-launched agents
    without a special-case branch on the client side.
    """
    manager = _manager(request)
    items: list[dict] = []

    # Synthetic entry for the primary agent, if one exists.
    primary = getattr(request.app.state, "agent", None)
    if primary is not None:
        items.append({"agent_id": "main", "ready": True, "primary": True})

    if manager is not None:
        for agent_id in manager.list_ids():
            if agent_id in _RESERVED_IDS:
                # Shouldn't happen (create rejects reserved ids), but
                # if it somehow did, don't double-list.
                continue
            ws = manager.get(agent_id)
            items.append(_workspace_summary(agent_id, ws, is_primary=False))

    return JSONResponse({"agents": items})


@router.post("")
async def create_agent(request: Request) -> JSONResponse:
    """Launch a new agent. Body: ``{"agent_id": "...", "config": {...}}``.

    Upsert-via-DELETE semantics are intentional — POST to an existing
    id returns 409 rather than silently replacing, because replacing
    would drop the AgentLoop's in-memory session history without
    warning. The caller must DELETE first if they really want to
    rebuild.
    """
    manager = _manager(request)
    if manager is None:
        return JSONResponse(
            {"ok": False, "error": "multi-agent registry not configured"},
            status_code=503,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    raw_id = body.get("agent_id") or body.get("id") or ""
    if not isinstance(raw_id, str) or not raw_id.strip():
        return JSONResponse(
            {"ok": False, "error": "agent_id required"}, status_code=400
        )
    agent_id = raw_id.strip()
    if agent_id in _RESERVED_IDS:
        return JSONResponse(
            {"ok": False, "error": f"agent_id {agent_id!r} is reserved"},
            status_code=400,
        )
    if agent_id != _sanitize_id(agent_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "agent_id may only contain [A-Za-z0-9_-]",
            },
            status_code=400,
        )
    if agent_id in manager:
        return JSONResponse(
            {"ok": False, "error": "already exists"}, status_code=409
        )

    config = body.get("config") or {}
    if not isinstance(config, dict):
        return JSONResponse(
            {"ok": False, "error": "config must be an object"}, status_code=400
        )

    try:
        ws = await manager.create(agent_id, config)
    except AgentIdError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface as HTTP 500 instead of stalling
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {"ok": True, "agent_id": agent_id, "ready": ws.is_ready()},
        status_code=201,
    )


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request) -> JSONResponse:
    """Tear down a running agent.

    404 on a missing id is deliberate — idempotent DELETE that hides
    "already gone" races the tabs. The primary ``main`` agent is not
    deletable through this surface; stop the whole daemon instead.
    """
    manager = _manager(request)
    if manager is None:
        return JSONResponse(
            {"ok": False, "error": "multi-agent registry not configured"},
            status_code=503,
        )
    if agent_id in _RESERVED_IDS:
        return JSONResponse(
            {"ok": False, "error": f"cannot delete reserved agent {agent_id!r}"},
            status_code=400,
        )
    removed = await manager.remove(agent_id)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> JSONResponse:
    """Inspect one agent's readiness + primary flag."""
    if agent_id == "main":
        primary = getattr(request.app.state, "agent", None)
        if primary is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return JSONResponse({"agent_id": "main", "ready": True, "primary": True})
    manager = _manager(request)
    if manager is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ws = manager.get(agent_id)
    if ws is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse(_workspace_summary(agent_id, ws, is_primary=False))
