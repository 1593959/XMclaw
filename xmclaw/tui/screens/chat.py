"""ChatScreen — main chat interface for the TUI."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from typing import TYPE_CHECKING

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
else:
    ComposeResult = object

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class ChatScreen(Vertical):  # type: ignore[misc]
    """Displays message history and an input bar."""

    CSS = """
    ChatScreen {
        layout: vertical;
        height: 100%;
    }
    #messages {
        height: 1fr;
        border: solid $primary;
        padding: 1;
        overflow-y: scroll;
    }
    #input-bar {
        height: auto;
        border: solid $primary-darken-2;
        padding: 1;
    }
    #msg-input {
        width: 1fr;
    }
    .user-msg {
        color: $text-accent;
        text-style: bold;
    }
    .agent-msg {
        color: $text;
    }
    .system-msg {
        color: $text-muted;
        text-style: italic;
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
        self._message_box = Static(id="messages")
        self._input = Input(placeholder="输入消息后回车发送…", id="msg-input")

    def compose(self) -> ComposeResult:
        yield self._message_box
        with Horizontal(id="input-bar"):
            yield self._input
            yield Button("发送", id="send-btn")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-btn":
            await self._submit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._submit()

    async def _submit(self) -> None:
        text = self._input.value.strip()
        if not text:
            return
        self._input.value = ""
        await self.append_user(text)
        await self._on_send(text)

    # ── public append API ──

    async def append_user(self, text: str) -> None:
        await self._append_line(f"[user-msg]你:[/user-msg] {text}")

    async def append_agent(self, text: str) -> None:
        await self._append_line(f"[agent-msg]{self._agent_name}:[/agent-msg] {text}")

    async def append_system(self, text: str) -> None:
        await self._append_line(f"[system-msg]{text}[/system-msg]")

    async def _append_line(self, line: str) -> None:
        current = self._message_box.renderable or ""
        if current:
            new_text = f"{current}\n{line}"
        else:
            new_text = line
        self._message_box.update(new_text)
        # Auto-scroll to bottom.
        self._message_box.scroll_end(animate=False)

    def clear(self) -> None:
        self._message_box.update("")

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
