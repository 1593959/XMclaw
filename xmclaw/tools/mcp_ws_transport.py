"""WebSocket transport for MCP (Model Context Protocol).

Wraps a websockets connection as an async reader/writer compatible
with the mcp.ClientSession interface.
"""
from __future__ import annotations
import asyncio
import json
from typing import Any
from websockets import WebSocketClientProtocol


class WebSocketTransport:
    """WebSocket-based transport for MCP client sessions.

    Implements the async reader/writer interface that mcp.ClientSession expects,
    converting WebSocket messages to/from the JSON-RPC format used by MCP.
    """

    def __init__(self, ws: WebSocketClientProtocol):
        self._ws = ws
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False
        # Start background reader
        asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read messages from WebSocket and enqueue them."""
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                try:
                    msg = raw if isinstance(raw, str) else raw.decode("utf-8")
                    self._read_queue.put_nowait(msg)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._closed = True
            self._read_queue.put_nowait("")

    async def readline(self) -> str:
        """Read a single line (JSON message) from the queue."""
        msg = await self._read_queue.get()
        if not msg:
            raise EOFError("WebSocket closed")
        return msg

    async def read(self, n: int | None = None) -> str:
        """Read exactly one message from the queue."""
        return await self.readline()

    async def write(self, data: str | bytes) -> None:
        """Send a message over WebSocket."""
        if self._closed:
            return
        try:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            await self._ws.send(data)
        except Exception:
            self._closed = True

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass
