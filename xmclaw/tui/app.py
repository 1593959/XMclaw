"""JarvisTUI — textual app entry point with multi-panel agent layout.

Connects to the XMclaw daemon via WebSocket and renders the agent
interface in a terminal-native multi-panel Grid layout:
  StatusBar · PlanView · ToolLog · ThinkingView · CompactChatLog
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

from xmclaw.tui.screens.chat import AgentScreen
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class JarvisTUI(App[None]):  # type: ignore[misc]
    """Textual application for XMclaw — multi-panel agent interface."""

    CSS = """
    #panels {
        padding: 0 1;
    }
    #panels > * {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+n", "new_session", "New Session"),
        ("t", "toggle_thinking", "Toggle Thinking"),
    ]

    def __init__(
        self,
        *,
        daemon_ws_url: str = "ws://127.0.0.1:8766/agent/v2/default",
        session_id: str | None = None,
        agent_name: str = "XM",
    ) -> None:
        super().__init__()
        self._ws_url = daemon_ws_url
        self._session_id = session_id or f"tui_{asyncio.get_event_loop().time():.0f}"
        self._agent_name = agent_name
        self._ws: Any | None = None
        self._ws_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield AgentScreen(
            session_id=self._session_id,
            on_send=self._on_user_send,
            agent_name=self._agent_name,
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "XMclaw Agent"
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

    # ── websocket ────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Maintain WebSocket connection and forward events to AgentScreen."""
        try:
            import websockets
        except ImportError:
            _log.error("tui.websockets_missing: pip install websockets")
            self._push_system("[red]websockets not installed — run: pip install websockets[/red]")
            return

        screen = self.query_one(AgentScreen)
        while True:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    screen.status_bar.update_connection("connected")
                    screen.status_bar.refresh_display()
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await screen.on_daemon_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("tui.ws_error: %s", exc)
                screen.status_bar.update_connection("reconnecting")
                screen.status_bar.refresh_display()
                await asyncio.sleep(3.0)

    async def _on_user_send(self, text: str) -> None:
        if self._ws is None:
            self._push_system("[red]Not connected to daemon[/red]")
            return
        try:
            await self._ws.send(json.dumps({
                "type": "user",
                "content": text,
            }))
        except Exception as exc:  # noqa: BLE001
            _log.warning("tui.send_failed: %s", exc)
            self._push_system(f"[red]Send failed: {exc}[/red]")

    def _push_system(self, text: str) -> None:
        screen = self.query_one(AgentScreen)
        screen.chat_log.add_system(text)

    def action_new_session(self) -> None:
        self._session_id = f"tui_{asyncio.get_event_loop().time():.0f}"
        screen = self.query_one(AgentScreen)
        screen.clear()
        self.sub_title = self._session_id

    def action_toggle_thinking(self) -> None:
        screen = self.query_one(AgentScreen)
        screen.thinking_view.toggle()
