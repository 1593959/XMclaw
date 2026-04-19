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

    async def send_ask_user_answer(self, answer: str) -> None:
        """Send an answer in response to an ask_user tool call."""
        if self.ws:
            await self.ws.send(json.dumps({"type": "ask_user_answer", "answer": answer}))

    async def receive_stream(self) -> AsyncIterator[str]:
        """Yield raw WebSocket messages until the connection closes.

        NOTE: this method no longer breaks on 'done' because the server may
        continue sending messages after an ask_user answer is sent (the
        asend() resumes the paused generator).  Callers must handle message
        types and terminate their own loop on 'done'/'error'.
        """
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        async for raw in self.ws:
            yield raw

    async def disconnect(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None
