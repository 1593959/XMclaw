"""WSChannelAdapter — WebSocket transport for CLI / Web UI / Desktop clients.

Anti-req #7 compliance: this adapter is exercised by the shared
``tests/conformance/channel_test_suite.py`` matrix — every scenario
that applies to every channel must pass here before release.

Anti-req #8 preview: the ``auth_check`` callback is where device-bound
authentication will plug in. Phase 2.3 ships with a pass-through default
so we can demo locally; Phase 2.x replaces it with ed25519 device
pairing. Until then, do NOT bind to non-loopback addresses in prod.

Wire protocol (newline-delimited JSON):

    client → server:  {"type": "user", "content": "..."}
    server → client:  {"type": "assistant_chunk", "content": "..."} |
                      {"type": "tool_call", ...} | {"type": "error", ...}

Clients carry an opaque ``ref`` (connection id) for targeted sends.
``ChannelTarget(channel="ws", ref="*")`` broadcasts to all.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)


class WSChannelAdapter(ChannelAdapter):
    """WebSocket channel with pluggable auth.

    Parameters
    ----------
    host : str
        Bind address. Default ``127.0.0.1`` — do not change to 0.0.0.0
        until device-bound auth (anti-req #8) lands.
    port : int
        Port to bind. Pass ``0`` for an OS-assigned ephemeral port; read
        the actual port back from ``self.port`` after ``start()``.
    auth_check : callable | None
        Optional async ``(headers: dict) -> bool``. Connections that
        fail are closed immediately. Default accepts all (loopback-only).
    """

    name: ClassVar[str] = "ws"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        auth_check: Callable[[dict[str, str]], Awaitable[bool]] | None = None,
    ) -> None:
        self.host = host
        self._requested_port = port
        self.port: int | None = None  # set by start()
        self._auth_check = auth_check
        self._server: Any = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # conn_id -> websocket
        self._conns: dict[str, Any] = {}
        self._stopped = asyncio.Event()

    # ── lifecycle ──

    async def start(self) -> None:
        import websockets
        self._stopped.clear()
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self._requested_port,
        )
        # Resolve the actual port (handles port=0 case)
        # websockets server exposes sockets on ``.sockets`` (may be list of
        # wrappers); handle both raw and wrapped socket shapes.
        socks = list(getattr(self._server, "sockets", []) or [])
        if socks:
            raw = socks[0]
            getsock = getattr(raw, "socket", raw)
            self.port = getsock.getsockname()[1]
        else:
            self.port = self._requested_port

    async def stop(self) -> None:
        self._stopped.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Close any still-open connections.
        for ws in list(self._conns.values()):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass
        self._conns.clear()

    # ── send / subscribe ──

    async def send(self, target: ChannelTarget, payload: OutboundMessage) -> str:
        """Deliver ``payload`` to the connection identified by ``target.ref``.

        ``target.ref == "*"`` broadcasts to all connected clients. Returns
        an internal message id (UUID) — WS has no native msg id concept.
        """
        if target.channel != self.name:
            raise ValueError(
                f"WSChannelAdapter cannot send to channel {target.channel!r}"
            )
        wire = json.dumps({
            "type": "assistant",
            "content": payload.content,
            "reply_to": payload.reply_to,
            "attachments": list(payload.attachments),
        })
        targets: list[Any]
        if target.ref == "*":
            targets = list(self._conns.values())
        else:
            ws = self._conns.get(target.ref)
            if ws is None:
                raise LookupError(f"no open connection with ref={target.ref!r}")
            targets = [ws]
        msg_id = uuid.uuid4().hex
        # Fire in parallel; log (not raise) on per-conn send failure so one
        # dead conn does not abort the whole broadcast.
        results = await asyncio.gather(
            *[self._safe_send(ws, wire) for ws in targets],
            return_exceptions=True,
        )
        # If every send failed, propagate the first exception so the caller
        # knows something is wrong.
        if all(isinstance(r, Exception) for r in results) and results:
            first = next(r for r in results if isinstance(r, Exception))
            raise first
        return msg_id

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    # ── internals ──

    async def _safe_send(self, ws: Any, wire: str) -> None:
        await ws.send(wire)

    async def _handle_connection(self, ws: Any) -> None:
        """Per-connection handler. Runs until the client closes or we stop."""
        # Auth gate (anti-req #8 hook point). Default is accept-all.
        if self._auth_check is not None:
            headers = dict(getattr(ws, "request_headers", {}) or {})
            try:
                ok = await self._auth_check(headers)
            except Exception:  # noqa: BLE001 — auth must never crash the server
                ok = False
            if not ok:
                await ws.close(code=4401, reason="unauthorized")
                return

        conn_id = uuid.uuid4().hex
        self._conns[conn_id] = ws
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    # Malformed frames are dropped silently with a warning.
                    # (We might surface a structured "error" frame in Phase 2.4
                    # — for now, a drop is safer than crashing the conn.)
                    continue
                if not isinstance(frame, dict):
                    continue
                msg = InboundMessage(
                    target=ChannelTarget(channel=self.name, ref=conn_id),
                    user_ref=conn_id,
                    content=str(frame.get("content", "")),
                    raw=frame,
                )
                for h in self._handlers:
                    # Handlers run sequentially per frame; if one fails, the
                    # others still run and the connection stays open.
                    try:
                        await h(msg)
                    except Exception:  # noqa: BLE001 — isolate subscriber failures
                        pass
        except Exception:  # noqa: BLE001 — connection-level errors end the loop
            pass
        finally:
            self._conns.pop(conn_id, None)
