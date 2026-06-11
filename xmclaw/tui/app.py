"""JarvisTUI — textual app entry point.

Connects to the XMclaw daemon via WebSocket and renders the chat
stream in a terminal-native interface.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from typing import TYPE_CHECKING

from textual.app import App
from textual.widgets import Footer, Header

if TYPE_CHECKING:
    from textual.app import ComposeResult
else:
    ComposeResult = object

from xmclaw.tui.screens.chat import ChatScreen
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class JarvisTUI(App[None]):  # type: ignore[misc]
    """Textual application for XMclaw."""

    CSS = """
    """


    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+n", "new_session", "新会话"),
    ]

    def __init__(
        self,
        *,
        daemon_ws_url: str = "ws://127.0.0.1:8766/agent/v2/default",
        session_id: str | None = None,
        agent_name: str = "Jarvis",
    ) -> None:
        super().__init__()
        self._ws_url = daemon_ws_url
        self._session_id = session_id or f"tui_{asyncio.get_event_loop().time():.0f}"
        self._agent_name = agent_name
        self._ws: Any | None = None
        self._ws_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatScreen(
            session_id=self._session_id,
            on_send=self._on_user_send,
            agent_name=self._agent_name,
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "XMclaw Chat"
        self.sub_title = self._session_id
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def on_unmount(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()

    # ── websocket ──

    async def _ws_loop(self) -> None:
        """Maintain a WebSocket connection to the daemon and forward
        events to the ChatScreen."""
        try:
            import websockets
        except ImportError:
            _log.error("tui.websockets_missing: pip install websockets")
            self._push_system("[red]未安装 websockets — 运行: pip install websockets[/red]")
            return

        chat = self.query_one(ChatScreen)
        while True:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    self._push_system("[green]已连接 daemon[/green]")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await chat.on_daemon_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("tui.ws_error: %s", exc)
                self._push_system(f"[yellow]连接断开 — 3秒后重试 ({exc})[/yellow]")
                await asyncio.sleep(3.0)

    async def _on_user_send(self, text: str) -> None:
        if self._ws is None:
            self._push_system("[red]未连接 daemon[/red]")
            return
        try:
            await self._ws.send(json.dumps({
                "type": "user",
                "content": text,
            }))
        except Exception as exc:  # noqa: BLE001
            _log.warning("tui.send_failed: %s", exc)
            self._push_system(f"[red]发送失败: {exc}[/red]")

    def _push_system(self, text: str) -> None:
        chat = self.query_one(ChatScreen)
        asyncio.create_task(chat.append_system(text))

    def action_new_session(self) -> None:
        self._session_id = f"tui_{asyncio.get_event_loop().time():.0f}"
        chat = self.query_one(ChatScreen)
        chat.clear()
        self.sub_title = self._session_id
        self._push_system(f"[dim]新会话: {self._session_id}[/dim]")
