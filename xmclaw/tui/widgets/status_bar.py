"""Agent status bar — model, hop, tokens, time, tool count."""
from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """Fixed- height metrics strip at the top of the agent layout."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._model: str = ""
        self._hop: int = 0
        self._max_hops: int = 40
        self._tokens: int = 0
        self._cost: float = 0.0
        self._elapsed: float = 0.0
        self._tool_count: int = 0
        self._status: str = "disconnected"

    def update_model(self, model: str) -> None:
        self._model = model or self._model

    def update_hop(self, hop: int, max_hops: int = 40) -> None:
        self._hop = hop
        self._max_hops = max_hops

    def update_tokens(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self._tokens += prompt_tokens + completion_tokens

    def update_cost(self, usd: float) -> None:
        self._cost = usd

    def update_elapsed(self, sec: float) -> None:
        self._elapsed = sec

    def update_tool_count(self, count: int) -> None:
        self._tool_count = count

    def update_connection(self, status: str) -> None:
        self._status = status

    def refresh_display(self) -> None:
        parts: list[str] = []
        if self._model:
            parts.append(f"🧠 [bold]{self._model}[/bold]")
        parts.append(f"⚡ hop {self._hop}/{self._max_hops}")
        if self._tokens:
            parts.append(self._fmt_tokens())
        if self._cost:
            parts.append(f"💰 ${self._cost:.2f}")
        if self._elapsed:
            parts.append(f"⏱ {self._fmt_duration(self._elapsed)}")
        parts.append(f"🔧 {self._tool_count} calls")
        dot = "●" if self._status == "connected" else "○"
        colour = "green" if self._status == "connected" else "yellow"
        parts.append(f"[{colour}]{dot} {self._status}[/{colour}]")
        self.update(" · ".join(parts))

    def _fmt_tokens(self) -> str:
        n = self._tokens
        if n >= 1_000_000:
            return f"📊 {n / 1_000_000:.1f}M tokens"
        if n >= 1_000:
            return f"📊 {n / 1_000:.1f}k tokens"
        return f"📊 {n} tokens"

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        if sec < 60:
            return f"{int(sec)}s"
        if sec < 3600:
            return f"{int(sec // 60)}m{int(sec % 60)}s"
        return f"{int(sec // 3600)}h{int((sec % 3600) // 60)}m"
