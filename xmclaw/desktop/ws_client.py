"""WebSocket client running in a QThread for non-blocking UI."""
import json
import asyncio
import websockets
from PySide6.QtCore import QThread, Signal


class WSClientThread(QThread):
    message_received = Signal(str, str)  # type, content
    connection_changed = Signal(bool)    # connected

    def __init__(self, agent_id: str, parent=None):
        super().__init__(parent)
        self.agent_id = agent_id
        self.uri = f"ws://127.0.0.1:8765/agent/{agent_id}"
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = True

    def run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        while self._running:
            try:
                async with websockets.connect(self.uri) as ws:
                    self.connection_changed.emit(True)
                    send_task = asyncio.create_task(self._sender(ws))
                    async for message in ws:
                        data = json.loads(message)
                        self.message_received.emit(data.get("type", ""), data.get("content", ""))
                    send_task.cancel()
            except Exception:
                self.connection_changed.emit(False)
                await asyncio.sleep(2)

    async def _sender(self, ws):
        while self._running:
            text = await self._queue.get()
            await ws.send(json.dumps({"role": "user", "content": text}))

    def send(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
