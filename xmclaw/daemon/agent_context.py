"""Per-task "who am I?" for multi-agent turns — Epic #17 Phase 4.

Tools invoked mid-turn (especially the agent-to-agent tools that land in
Phase 5 — ``list_agents``, ``chat_with_agent``, ``submit_to_agent``,
``check_agent_task``) need to know which agent is currently running so
they can stamp outgoing requests with the caller's id. Threading
``agent_id`` through every call site would mean touching ``ToolProvider``,
every concrete tool, the scheduler, the grader, and a few hundred tests;
using a :class:`ContextVar` is cheaper and still async-safe (each
asyncio Task gets its own copy).

Two surfaces:

* :func:`use_current_agent_id` — scoped context manager, used by the WS
  handler to mark "this block is the 'worker' agent's turn". Works under
  ``asyncio.gather`` / ``TaskGroup`` because contextvars propagate into
  child tasks at task-creation time.
* :class:`AgentContextMiddleware` — pure-ASGI middleware that reads
  ``X-Agent-Id`` (header) or ``agent_id`` (query param) off incoming
  HTTP/WS requests and pre-seeds the var before the route handler
  runs. Cheap ergonomic for plugins that issue HTTP calls between
  agents — they don't have to wire the var by hand.

Design notes:

* Starlette's :class:`BaseHTTPMiddleware` spawns the endpoint on a
  separate anyio task, which does NOT inherit contextvar mutations made
  inside the middleware (known pitfall). That's why we go pure-ASGI —
  the context we set persists for the downstream ``self.app(...)`` call
  without any extra plumbing.
* The middleware uses the RAW requested id, not the resolved one.
  "main" and "" both resolve to the primary at the WS-handler layer;
  the middleware sees the client's wording. This keeps tool-emitted
  events ("sent from agent=main") aligned with what the client asked
  for, not what the daemon chose — the daemon's resolution is an
  internal detail, the client's intent isn't.
* We intentionally do NOT default to ``"main"`` when the header is
  absent. ``None`` means "not inside a scoped turn" — tools that care
  can fall back to their own default rather than being misled into
  thinking every stray request is from the primary agent.
"""
from __future__ import annotations

from urllib.parse import parse_qs

# Phase 6: the ContextVar + accessors moved to ``xmclaw/core/`` so tool
# providers (which may not import ``xmclaw.daemon.*`` per
# ``xmclaw/providers/tool/AGENTS.md`` §2) can read the ambient agent
# id directly. Re-exported here so existing imports from
# ``xmclaw.daemon.agent_context`` keep working.
from xmclaw.core.agent_context import (
    _current_agent_id,
    get_current_agent_id,
    use_current_agent_id,
)

__all__ = [
    "AgentContextMiddleware",
    "_current_agent_id",
    "get_current_agent_id",
    "use_current_agent_id",
]


def _extract_agent_id_from_scope(scope: dict) -> str | None:
    """Pull ``X-Agent-Id`` header or ``agent_id`` query param off an ASGI scope.

    Header wins over query param — headers are the canonical form and
    query params are a browser-ergonomics fallback (same precedence
    already used by the WS handler for the pairing token).
    """
    # ASGI headers are a list of (bytes, bytes) tuples; header names are
    # lowercase per the spec.
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name == b"x-agent-id":
            try:
                value = raw_value.decode("latin-1").strip()
            except Exception:  # noqa: BLE001
                continue
            if value:
                return value
    # Query-string fallback. ``scope["query_string"]`` is bytes per ASGI.
    qs = scope.get("query_string") or b""
    if qs:
        try:
            parsed = parse_qs(qs.decode("latin-1"))
        except Exception:  # noqa: BLE001
            return None
        values = parsed.get("agent_id") or []
        if values:
            v = values[0].strip()
            if v:
                return v
    return None


class AgentContextMiddleware:
    """ASGI middleware that pre-seeds :data:`_current_agent_id`.

    Mount with ``app.add_middleware(AgentContextMiddleware)``. Covers
    both ``http`` and ``websocket`` scopes; ``lifespan`` and any other
    scope type pass through untouched.

    Route handlers can still override with :func:`use_current_agent_id`
    — the WS dispatch does exactly this after resolving "main" → the
    primary agent loop. The middleware's contribution is ambient
    defaults for cases where the handler doesn't set it explicitly.
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        agent_id = _extract_agent_id_from_scope(scope)
        token = _current_agent_id.set(agent_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_agent_id.reset(token)
