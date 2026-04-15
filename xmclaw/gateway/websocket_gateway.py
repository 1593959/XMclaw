"""WebSocket gateway implementation."""
import json
import websockets
from typing import AsyncIterator
from xmclaw.gateway.base import Gateway


class WebSocketGateway(Gateway):
    def __init__(self, uri: str):
        self.uri = uri
        self.ws = None

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.uri)

    async def send(self, message: str) -> None:
        if self.ws:
            await self.ws.send(json.dumps({"role": "user", "content": message}))

    async def receive_stream(self) -> AsyncIterator[str]:
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        async for raw in self.ws:
            data = json.loads(raw)
            if data.get("type") == "done":
                break
            if data.get("type") == "error":
                yield f"[Error: {data.get('content')}]"
                break
            yield data.get("content", "")

    async def disconnect(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None
