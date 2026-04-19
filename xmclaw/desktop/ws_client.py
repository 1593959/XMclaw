"""WebSocket client running in a QThread for non-blocking UI."""
import json
import asyncio
import websockets
from PySide6.QtCore import QThread, Signal


class WSClientThread(QThread):
    message_received = Signal(dict)
    chunk_received = Signal(str)
    state_changed = Signal(str, str)
    ask_user = Signal(str)
    tool_called = Signal(dict)
    connection_changed = Signal(bool)

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
                    async for raw in ws:
                        data = json.loads(raw)
                        self._handle(data)
                    send_task.cancel()
            except Exception:
                self.connection_changed.emit(False)
                await asyncio.sleep(2)

    def _handle(self, data: dict):
        msg_type = data.get("type", "")
        if msg_type == "chunk":
            self.chunk_received.emit(data.get("content", ""))
        elif msg_type == "state":
            self.state_changed.emit(data.get("state", ""), data.get("thought", ""))
        elif msg_type == "ask_user":
            self.ask_user.emit(data.get("question", ""))
        elif msg_type == "tool_result":
            self.tool_called.emit({"name": data.get("tool", ""), "result": data.get("result", "")})
        elif msg_type == "tool_call":
            self.tool_called.emit({"name": data.get("tool", ""), "arguments": data.get("arguments", {})})
        else:
            self.message_received.emit(data)

    async def _sender(self, ws):
        while self._running:
            text = await self._queue.get()
            await ws.send(json.dumps({"role": "user", "content": text}))

    def send_message(self, text: str, plan_mode: bool = False) -> None:
        try:
            payload = {"role": "user", "content": text, "plan_mode": plan_mode}
            self._queue.put_nowait(json.dumps(payload))
        except Exception:
            pass

    def send_answer(self, text: str) -> None:
        try:
            payload = {"role": "user", "content": text}
            self._queue.put_nowait(json.dumps(payload))
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
