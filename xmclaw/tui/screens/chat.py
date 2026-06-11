"""AgentScreen — multi-panel agent TUI replacing the old chat-only screen.

Panel layout (top → bottom):
  StatusBar  — model · hop · tokens · time · tool count · connection
  PlanView   — multi-step plan with checkmarks (hidden when empty)
  ToolLog    — real-time tool call feed
  ThinkingView — collapsible chain-of-thought (hidden when empty)
  CompactChatLog — last N messages (reference-only)
  Input bar  — single-line input + send button
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input

from xmclaw.tui.widgets.status_bar import StatusBar
from xmclaw.tui.widgets.tool_log import (
    CompactChatLog,
    PlanView,
    ThinkingView,
    ToolLog,
)
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class AgentScreen(Vertical):  # type: ignore[misc]
    """Multi-panel agent TUI screen."""

    DEFAULT_CSS = """
    AgentScreen {
        layout: vertical;
        width: 100%;
        height: 100%;
    }
    #status-bar {
        dock: top;
        height: 1;
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
    #panels {
        height: 1fr;
        overflow-y: auto;
    }
    """

    def __init__(
        self,
        *,
        session_id: str,
        on_send: Callable[[str], Awaitable[None]],
        agent_name: str = "XM",
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._on_send = on_send
        self._agent_name = agent_name
        self._submitting = False
        self.status_bar = StatusBar(id="status-bar")
        self.plan_view = PlanView()
        self.tool_log = ToolLog()
        self.thinking_view = ThinkingView()
        self.chat_log = CompactChatLog(agent_name)
        self._input = Input(placeholder="输入消息后回车发送…", id="msg-input")

    def compose(self) -> None:
        yield self.status_bar
        with Vertical(id="panels"):
            yield self.plan_view
            yield self.tool_log
            yield self.thinking_view
            yield self.chat_log
        with Horizontal(id="input-bar"):
            yield self._input
            yield Button("发送", id="send-btn")

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
            self.chat_log.add_user(text)
            await self._on_send(text)
        finally:
            self._submitting = False

    def clear(self) -> None:
        self.chat_log.clear()
        self.plan_view.clear()
        self.thinking_view.clear()

    # ── daemon message dispatch ──────────────────────────────────

    async def on_daemon_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type", "")
        payload = msg.get("payload", {})

        if t == "llm_request":
            if payload.get("model"):
                self.status_bar.update_model(payload["model"])
            if payload.get("hop") is not None:
                self.status_bar.update_hop(payload["hop"])
            self.status_bar.refresh_display()

        elif t == "llm_chunk":
            text = payload.get("content", "")
            if text:
                self.chat_log.add_agent(text)

        elif t == "llm_thinking_chunk":
            text = payload.get("content", "")
            if text:
                self.thinking_view.append(text)

        elif t == "llm_response":
            text = payload.get("content", "")
            if text:
                self.chat_log.add_agent(text)

        elif t == "tool_call_emitted":
            call_id = payload.get("call_id", payload.get("id", ""))
            name = payload.get("name", payload.get("tool_name", "tool"))
            args = payload.get("args", payload.get("arguments", {}))
            self.tool_log.add_entry(call_id, name, args)
            self.status_bar.update_tool_count(len(self.tool_log._entries))
            self.status_bar.refresh_display()

        elif t == "tool_invocation_started":
            call_id = payload.get("call_id", "")
            if call_id:
                self.tool_log.update_status(call_id, "running")

        elif t == "tool_invocation_finished":
            call_id = payload.get("call_id", "")
            ok = not payload.get("error")
            status = "done" if ok else "error"
            duration = payload.get("duration_ms")
            error = payload.get("error")
            if call_id:
                self.tool_log.update_status(call_id, status, duration, error)

        elif t == "cost_tick":
            pt = int(payload.get("prompt_tokens", 0))
            ct = int(payload.get("completion_tokens", 0))
            self.status_bar.update_tokens(pt, ct)
            if payload.get("spent_usd"):
                self.status_bar.update_cost(float(payload["spent_usd"]))
            self.status_bar.refresh_display()

        elif t == "proactive_proposal":
            text = payload.get("message", "")
            if text:
                self.chat_log.add_system(f"💡 {text}")

        elif t == "error":
            text = payload.get("message", "")
            self.chat_log.add_system(f"[red]Error: {text}[/red]")

    def on_key_t(self) -> None:
        """Toggle thinking panel visibility."""
        self.thinking_view.toggle()
