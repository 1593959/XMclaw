"""ASGI middleware — pairing-token auth on HTTP API routes.

B-73 closes a real gap. The WebSocket handler at ``/agent/v2/{sid}``
already enforces ``auth_check``, but the HTTP routers (sessions,
config, memory, profiles, agents, …) don't. With the default daemon
binding (127.0.0.1:8765) any process on the user's machine could:

  curl 127.0.0.1:8765/api/v2/sessions/<id>   # read full chat history
  curl -X DELETE 127.0.0.1:8765/api/v2/sessions/<id>   # wipe session
  curl -X PUT 127.0.0.1:8765/api/v2/config -d '...'    # rewrite config

The Web UI already attaches the pairing token as ``?token=...`` on every
request (``static/lib/api.js`` ``withToken``) and the daemon ignored it.
This middleware enforces it.

Allowlisted paths (no token needed):
  * ``/health``                — liveness probe
  * ``/api/v2/pair``           — bootstrap; the UI calls this BEFORE it
                                 has a token, then uses the token for
                                 every subsequent call
  * Anything outside ``/api/v2/`` — ``/`` redirect, ``/ui/*`` static
                                 assets. Static files don't expose any
                                 of the agent's state.

Skipped when ``auth_check is None`` (the ``--no-auth`` daemon mode);
the constructor takes a callable so wiring matches the WS handler's
signature exactly.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths that bypass the token check. Compared with ``startswith`` so
# the auto-generated pair-with-trailing-slash variant works too.
_ALLOWLIST_PREFIXES = (
    "/health",
    "/api/v2/pair",
)


class PairingAuthMiddleware(BaseHTTPMiddleware):
    """Reject HTTP requests to ``/api/v2/*`` without a valid pairing token."""

    def __init__(
        self, app, *,
        auth_check: Callable[[str | None], Awaitable[bool]],
    ) -> None:
        super().__init__(app)
        self._auth_check = auth_check

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path or ""
        if not path.startswith("/api/v2/"):
            return await call_next(request)
        for allowed in _ALLOWLIST_PREFIXES:
            if path.startswith(allowed):
                return await call_next(request)

        # Token from query string or Authorization: Bearer header. Same
        # extraction order as the WS handler so clients have one mental
        # model regardless of transport.
        token: str | None = request.query_params.get("token")
        if not token:
            auth = request.headers.get("authorization", "") or ""
            if auth.lower().startswith("bearer "):
                token = auth[len("bearer "):].strip() or None

        ok = False
        try:
            ok = await self._auth_check(token)
        except Exception:  # noqa: BLE001 — auth must never crash the daemon
            ok = False
        if not ok:
            return JSONResponse(
                {"error": "unauthorized", "detail": "missing or invalid pairing token"},
                status_code=401,
            )
        return await call_next(request)
