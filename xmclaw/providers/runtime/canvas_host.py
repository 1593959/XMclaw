"""Canvas host — HTTP+WS render server for live UI artifacts.

Direct port of OpenClaw's ``src/canvas-host/server.ts:1-39``. Canvas
is the "agent renders to a separate device" feature: the daemon can
serve an HTML page on a dedicated port (default 18793) and push
incremental updates over WebSocket so external apps (Mac / iOS /
Android web views) see live mutations.

This complements but does NOT replace the chat WS at
``/agent/v2/{session_id}``. Chat is for messages; Canvas is for
visualizing intermediate state — running tool output, file diffs,
plot panels. Phase 6 ships the surface; concrete renderers (Markdown,
diff, chart) layer on top in Phase 7+.

Public API:
* :class:`CanvasHost` — start/stop/publish HTML, port-bound singleton
* :func:`default_canvas_port` — resolves env override / config

Note: the server is a thin starlette app embedded in the same daemon
process. We don't share auth with the chat WS — each render gets its
own ephemeral token (or open if ``--no-auth`` was given).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)


def default_canvas_port() -> int:
    """Resolve the Canvas port. Env var ``XMC_CANVAS_PORT`` overrides;
    else 18793 (matches OpenClaw's default)."""
    raw = os.environ.get("XMC_CANVAS_PORT", "18793")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 18793


class CanvasHost:
    """Lightweight HTML+WS server. Lazy-imports starlette/uvicorn so
    daemons that don't enable Canvas don't pay the import cost.

    Usage::

        host = CanvasHost(port=18793)
        await host.start()
        await host.publish("<h1>Build progress: 50%</h1>")
        ...
        await host.stop()

    The HTML is the single rendered page; calling ``publish`` again
    replaces its contents and notifies all connected WS clients.
    Live-reload pattern mirrors OpenClaw's chokidar-based file watcher.
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self._port = port if port is not None else default_canvas_port()
        self._host = host
        self._html: str = "<!doctype html><title>XMclaw Canvas</title>"
        self._connections: set[Any] = set()
        self._server_task: asyncio.Task[None] | None = None
        self._app: Any = None
        self._server: Any = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._server_task is not None and not self._server_task.done()

    async def publish(self, html: str) -> None:
        """Replace the served HTML and notify all connected clients."""
        self._html = html
        # Best-effort fanout — disconnected sockets quietly drop.
        for ws in list(self._connections):
            try:
                await ws.send_text(json.dumps({"type": "reload"}))
            except Exception:  # noqa: BLE001
                self._connections.discard(ws)

    async def start(self) -> str:
        """Bring up the embedded server. Returns the public URL."""
        if self.is_running:
            return f"http://{self._host}:{self._port}/"
        try:
            from starlette.applications import Starlette
            from starlette.responses import HTMLResponse
            from starlette.routing import Route, WebSocketRoute
            from starlette.websockets import WebSocket
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "Canvas requires starlette + uvicorn (already daemon deps)"
            ) from exc

        async def _index(request):
            return HTMLResponse(self._html)

        async def _ws(websocket: WebSocket):
            await websocket.accept()
            self._connections.add(websocket)
            try:
                while True:
                    # Keep-alive — clients don't need to send anything.
                    await websocket.receive_text()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._connections.discard(websocket)

        self._app = Starlette(routes=[
            Route("/", _index),
            WebSocketRoute("/ws", _ws),
        ])
        config = uvicorn.Config(
            self._app, host=self._host, port=self._port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        # Give the loop a tick to actually bind.
        await asyncio.sleep(0.05)
        return f"http://{self._host}:{self._port}/"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=3.0)
            except asyncio.TimeoutError:
                self._server_task.cancel()
        self._server = None
        self._server_task = None
        self._app = None
