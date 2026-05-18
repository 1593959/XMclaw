"""Output-styles REST surface — Wave-32+ (2026-05-18).

Mounted at ``/api/v2/output_styles``. Lets the frontend Style chip
discover what's available + report what's active for a session.

  * ``GET /`` → ``{"styles": [{name, description, source}, ...]}``
  * ``GET /session/{session_id}`` → ``{session_id, style: {name, ...}}``

Setting the style is done via the WS user-frame ``output_style`` field
(handled in app.py) or via the LLM-facing ``set_output_style`` tool —
no PUT endpoint to keep the surface simple.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from starlette.responses import JSONResponse

from xmclaw.core.output_styles import list_styles, session_style

router = APIRouter(prefix="/api/v2/output_styles", tags=["output_styles"])


@router.get("")
async def list_output_styles() -> JSONResponse:
    rows = []
    for style in list_styles():
        d = asdict(style)
        # Hide the prompt body — UI just needs name + description +
        # source. Saves a few KB on every list call and keeps the
        # bigger prompts that might contain sensitive operator
        # customizations out of casual HTTP responses.
        d.pop("prompt", None)
        rows.append(d)
    return JSONResponse({"styles": rows})


@router.get("/session/{session_id}")
async def get_session_style(session_id: str) -> JSONResponse:
    style = session_style(session_id)
    return JSONResponse({
        "session_id": session_id,
        "style": {
            "name": style.name,
            "description": style.description,
            "source": style.source,
        },
    })


__all__ = ["router"]
