"""ChatScreen — main chat interface for the TUI."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from typing import TYPE_CHECKING

from rich.text import Text

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
else:
    ComposeResult = object

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class ChatScreen(Vertical):  # type: ignore[misc]
    """Displays message history and an input bar."""

    DEFAULT_CSS = """
    ChatScreen {
        layout: vertical;
        width: 100%;
        height: 100%;
    }
    #message-scroll {
        height: 1fr;
        border: solid $primary;
        padding: 1;
    }
    #messages {
        width: 100%;
        height: auto;
    }
    #input-bar {
        dock: bottom;
        height: 3;
        border: solid $primary-darken-2;
        padding: 0 1;
    }
    #msg-input {
        width: 1fr;
        height: 1;
        border: none;
    }
    #send-btn {
        height: 1;
        min-width: 8;
        border: none;
    }
    """


    def __init__(
        self,
        *,
        session_id: str,
        on_send: Callable[[str], Awaitable[None]],
        agent_name: str = "Jarvis",
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._on_send = on_send
        self._agent_name = agent_name
        self._lines: list[str] = []
        self._message_static = Static(id="messages")
        self._message_box = VerticalScroll(self._message_static, id="message-scroll")
        self._input = Input(placeholder="输入消息后回车发送…", id="msg-input")
        self._submitting = False

    def compose(self) -> ComposeResult:
        yield self._message_box
        with Horizontal(id="input-bar"):
            yield self._input
            yield Button("发送", id="send-btn")

    def on_mount(self) -> None:
        # Pin layout via instance styles — highest specificity, immune to
        # CSS cascade quirks. The message area flexes to fill, the input
        # bar stays a compact 3-row strip docked to the very bottom so it
        # never floats in the middle of an empty screen.
        try:
            self._message_box.styles.height = "1fr"
            bar = self.query_one("#input-bar")
            bar.styles.dock = "bottom"
            bar.styles.height = 3
            self._input.styles.height = 1
        except Exception:  # noqa: BLE001 — never let styling crash the TUI
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-btn":
            await self._submit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._submit()

    async def _submit(self) -> None:
        if self._submitting:
            return
        text = self._input.value.strip()
        if not text:
            return
        self._submitting = True
        self._input.value = ""
        try:
            await self.append_user(text)
            await self._on_send(text)
        finally:
            self._submitting = False

    # ── public append API ──

    async def append_user(self, text: str) -> None:
        self._lines.append(Text.assemble(("你: ", "bold cyan"), text).markup)
        self._refresh_messages()

    async def append_agent(self, text: str) -> None:
        self._lines.append(
            Text.assemble((f"{self._agent_name}: ", "bold green"), text).markup
        )
        self._refresh_messages()

    async def append_system(self, text: str) -> None:
        self._lines.append(text)
        self._refresh_messages()

    def _refresh_messages(self) -> None:
        self._message_static.update("\n".join(self._lines))
        self._message_box.scroll_end(animate=False)

    def clear(self) -> None:
        self._lines.clear()
        self._message_static.update("")

    # ── daemon message dispatch ──

    async def on_daemon_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type", "")
        payload = msg.get("payload", {})
        if t == "llm_chunk":
            text = payload.get("content", "")
            if text:
                await self.append_agent(text)
        elif t == "llm_response":
            text = payload.get("content", "")
            if text:
                await self.append_agent(text)
        elif t == "tool_invocation_started":
            name = payload.get("name", "tool")
            await self.append_system(f"▶ 正在运行 {name}…")
        elif t == "tool_invocation_finished":
            name = payload.get("name", "tool")
            ok = payload.get("ok", True)
            icon = "✓" if ok else "✗"
            await self.append_system(f"{icon} {name} 已完成")
        elif t == "proactive_proposal":
            text = payload.get("message", "")
            await self.append_system(f"💡 {text}")
        elif t == "error":
            text = payload.get("message", "")
            await self.append_system(f"[red]错误: {text}[/red]")
