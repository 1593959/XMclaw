"""ASGI middleware — convention #1: ``X-Agent-Id`` header → ContextVar.

Direct port of ``qwenpaw/src/qwenpaw/app/routers/agent_scoped.py``
``AgentContextMiddleware``. Reads the ``X-Agent-Id`` header (or
``agent_id`` query param) on every HTTP request, sets the
:func:`xmclaw.core.multi_agent.set_current_agent_id` ContextVar for the
duration of the request, then resets on response.

Default value when no header is present is ``"main"`` — matches
QwenPaw's "default agent" semantics so existing single-agent flows
keep working.
"""
from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from xmclaw.core.multi_agent import (
    reset_current_agent_id,
    set_current_agent_id,
)
from xmclaw.core.multi_agent.context import _current_agent_id


class AgentScopeMiddleware(BaseHTTPMiddleware):
    """Wrap each HTTP request with the matching ``current_agent_id`` ctx."""

    async def dispatch(self, request: Request, call_next):
        # Header takes precedence over query string. Default "main".
        agent_id = (
            request.headers.get("X-Agent-Id")
            or request.query_params.get("agent_id")
            or "main"
        ).strip() or "main"

        token = set_current_agent_id(agent_id)
        try:
            response: Response = await call_next(request)
        finally:
            reset_current_agent_id(token)

        # Echo back the resolved id so curl users can see what stuck —
        # QwenPaw does the same for debuggability.
        response.headers.setdefault("X-Agent-Id", agent_id)
        return response
