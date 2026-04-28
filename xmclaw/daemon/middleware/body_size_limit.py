"""ASGI middleware — cap request body size on ``/api/v2/*``.

B-75. ``request.json()`` (and any other ``request.body()`` consumer)
loads the entire request body into memory before parsing. Without a
content-length gate, a 1 GB POST to ``/api/v2/memory/<filename>`` or
``/api/v2/profiles/<canonical>`` would happily allocate a gigabyte of
RAM in the daemon process and quite possibly OOM-kill it.

This middleware rejects oversized requests at 413 BEFORE the body is
read. Default cap is 10 MB — comfortably more than any persona file,
journal entry, note, or workspace manifest needs (the curated MEMORY.md
cap is 2.2 KB; a user-authored note rarely runs past a few hundred
KB), but small enough that an accidentally-pasted 100 MB log file
or a malicious script trying to OOM the daemon gets stopped.

Two enforcement paths:
  1. Content-Length header present → compare against cap, reject early.
  2. No Content-Length (chunked transfer) → wrap the receive callable
     and tally bytes as the body streams in; trip 413 when the running
     total crosses the cap.

Path-scoped to ``/api/v2/*`` so file-upload endpoints (currently
``/api/v2/files/*`` uses tmp+replace and could theoretically need a
larger cap; revisit if a real upload use case shows up) can override
later via a wider middleware. ``/health``, ``/ui/*`` static, ``/`` etc
pass through untouched.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Default cap. 10 MB covers every legitimate XMclaw POST/PUT body —
# the largest user-authored payload is the memory editor's full-file
# replace for SOUL.md / MEMORY.md, typically a few KB. The /api/v2/files
# router (file editor) is a different surface but goes through a tmp
# write, not request.json() — its size budget is separate.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class BodySizeLimitMiddleware:
    """Reject ``/api/v2/*`` requests whose body exceeds ``max_bytes``.

    Runs at the raw ASGI layer (not BaseHTTPMiddleware) so we can
    intercept the body BEFORE Starlette buffers it — rejecting a 100 MB
    POST after the framework already allocated 100 MB defeats the
    purpose.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self._app = app
        self._max = int(max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        if not path.startswith("/api/v2/"):
            await self._app(scope, receive, send)
            return

        # Cheap path: Content-Length present → check upfront.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if declared > self._max:
                    await _send_413(send)
                    return
                break

        # Streamed path: wrap receive() to count bytes as they arrive.
        seen = 0
        max_bytes = self._max

        async def _bounded_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body") or b""
                seen += len(body)
                if seen > max_bytes:
                    # Drain remaining body in the background-ish way
                    # by signalling no more body; the framework will
                    # pick up our 413 response above. Simpler: just
                    # mark the message body empty so json() raises
                    # cleanly. We'd already have sent 413 if we knew
                    # the size — for chunked-overflow we send it
                    # NOW and zero the body so downstream sees
                    # nothing further.
                    raise _BodyTooLarge()
            return msg

        try:
            await self._app(scope, _bounded_receive, send)
        except _BodyTooLarge:
            await _send_413(send)


class _BodyTooLarge(Exception):
    """Internal sentinel — body exceeded cap mid-stream."""


async def _send_413(send: Send) -> None:
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"error":"request body too large","detail":"max 10MB on /api/v2/*"}',
    })
