"""Session recap endpoint — Wave-32+ (2026-05-18).

Mounted at :http:get:`/api/v2/session/{session_id}/recap`. Returns a
1-3 sentence "while you were away" summary of the session's recent
exchanges, generated on demand by :func:`xmclaw.cognition.
away_summary.generate_away_summary`.

Frontend usage: when the chat panel re-opens after a gap (tab closed
overnight, daemon restarted, etc.), the UI hits this endpoint and
displays the result as a banner / card so the user doesn't have to
re-read the transcript to remember what they were doing.

The endpoint is read-only and produces no side effects on the
session. It does make an LLM call — callers should treat it like any
other LLM-backed read (latency budget, error handling).

Pairing-token middleware guards this like every other ``/api/v2/*``
route — no special config needed.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.cognition.away_summary import generate_away_summary

router = APIRouter(prefix="/api/v2/session", tags=["session"])


@router.get("/{session_id}/recap")
async def session_recap(session_id: str, request: Request) -> JSONResponse:
    """Generate a recap of the given session.

    Returns
    -------
    JSON object with shape ``{"session_id": str, "recap": str | None,
    "messages_considered": int}``.

      * ``recap == null`` means the history was empty or the LLM
        call failed — frontend should hide the card.
      * ``messages_considered`` reflects the truncated tail (max 30)
        sent to the LLM. Useful for debugging stale-recap reports.
    """
    state = getattr(request.app, "state", None)
    agent: Any | None = getattr(state, "agent", None) if state else None
    if agent is None:
        return JSONResponse(
            {"error": "no agent wired"}, status_code=503,
        )

    histories: dict[str, list[Any]] | None = getattr(agent, "_histories", None)
    if histories is None:
        return JSONResponse(
            {"error": "agent has no history store"}, status_code=503,
        )

    history = list(histories.get(session_id) or ())
    if not history:
        return JSONResponse({
            "session_id": session_id,
            "recap": None,
            "messages_considered": 0,
        })

    llm = getattr(agent, "_llm", None)
    if llm is None:
        return JSONResponse(
            {"error": "agent has no LLM provider"}, status_code=503,
        )

    # Use the standard 30-message tail. Configurable via query if
    # operator needs a different window — keep it simple for v1.
    try:
        window = int(request.query_params.get("window") or 30)
    except (TypeError, ValueError):
        window = 30
    window = max(1, min(200, window))

    recap = await generate_away_summary(history, llm, max_messages=window)
    # Compute the actual count of non-system messages considered
    # (matches what the function did internally) so the caller can
    # tell whether window or content was the limiter.
    nonsystem = [m for m in history if getattr(m, "role", None) != "system"]
    considered = min(len(nonsystem), window)
    return JSONResponse({
        "session_id": session_id,
        "recap": recap,
        "messages_considered": considered,
    })


__all__ = ["router"]
