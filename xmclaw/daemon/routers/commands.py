"""Markdown commands REST surface — Wave-32+ (2026-05-18).

Mounted at ``/api/v2/commands``. Lets the frontend slash-popover
discover what's available + lets external tools render commands
ad-hoc without going through WS.

  * ``GET  /``                  → list all discovered commands
  * ``GET  /{name}``            → single command's metadata + body
  * ``POST /{name}/render``     → run shell escapes + ``$ARGUMENTS``
                                  substitution, return rendered text
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from starlette.responses import JSONResponse

from xmclaw.cognition.markdown_commands import (
    discover_commands,
    find_command,
    render_command,
)

router = APIRouter(prefix="/api/v2/commands", tags=["commands"])


@router.get("")
async def list_commands() -> JSONResponse:
    rows: list[dict[str, Any]] = []
    for c in discover_commands():
        d = asdict(c)
        # Hide the prompt body in list output — keep payload small.
        # ``GET /{name}`` returns the full record.
        d.pop("prompt_body", None)
        rows.append(d)
    return JSONResponse({"commands": rows})


@router.get("/{name}")
async def get_command(name: str) -> JSONResponse:
    c = find_command(name)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown command: {name!r}")
    return JSONResponse(asdict(c))


@router.post("/{name}/render")
async def render(
    name: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> JSONResponse:
    c = find_command(name)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown command: {name!r}")
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, str):
        args = ""
    # Workspace-trust check — gated through the same trust marker
    # the hook engine uses (see core/hooks/trust.py). Untrusted
    # workspaces get a degraded render (shell escapes substituted
    # with placeholders).
    try:
        from xmclaw.core.hooks.trust import workspace_trust_level
        trust = workspace_trust_level()
    except Exception:  # noqa: BLE001
        trust = "trusted"
    result = await render_command(c, args, workspace_trust=trust)
    return JSONResponse({
        "name": name,
        "ok": result.ok,
        "rendered": result.rendered,
        "failures": result.failures,
    })


__all__ = ["router"]
