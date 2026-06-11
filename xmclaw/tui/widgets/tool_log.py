"""Tool execution log — real-time tool call feed in the TUI."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from textual.widgets import Static


class ToolLog(Static):
    """Scrollable feed of recent tool calls with status icons and timing."""

    DEFAULT_CSS = """
    ToolLog {
        height: auto;
        max-height: 12;
        border: solid $secondary;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    STATUS_ICONS = {
        "pending": "○",
        "running": "◉",
        "done":    "✓",
        "error":   "✗",
    }

    def __init__(self) -> None:
        super().__init__("")
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_visible = 20  # Keep last N entries

    def add_entry(self, call_id: str, tool_name: str, args: dict[str, Any] | None = None) -> None:
        self._entries[call_id] = {
            "tool_name": tool_name,
            "args": args or {},
            "status": "pending",
            "duration_ms": None,
            "error": None,
        }
        self._prune()
        self._render()

    def update_status(self, call_id: str, status: str, duration_ms: float | None = None, error: str | None = None) -> None:
        if call_id not in self._entries:
            return
        self._entries[call_id]["status"] = status
        if duration_ms is not None:
            self._entries[call_id]["duration_ms"] = duration_ms
        if error is not None:
            self._entries[call_id]["error"] = error
        self._render()

    def _prune(self) -> None:
        while len(self._entries) > self._max_visible:
            self._entries.popitem(last=False)

    def _render(self) -> None:
        lines: list[str] = []
        running = sum(1 for e in self._entries.values() if e["status"] == "running")
        done = sum(1 for e in self._entries.values() if e["status"] == "done")
        errors = sum(1 for e in self._entries.values() if e["status"] == "error")
        header = f"🔧 Tools  "
        if running:
            header += f"[bold cyan]{running} running[/bold cyan]  "
        header += f"[green]{done} done[/green]  "
        if errors:
            header += f"[red]{errors} failed[/red]  "
        lines.append(header)

        for entry in list(self._entries.values())[-10:]:  # show last 10
            icon = self.STATUS_ICONS.get(entry["status"], "?")
            name = entry["tool_name"]
            dur = f" {self._fmt_ms(entry['duration_ms'])}" if entry.get("duration_ms") else ""
            colour = {
                "running": "cyan", "done": "green", "error": "red",
            }.get(entry["status"], "")
            line = f"  [{colour}]{icon}[/{colour}] {name}{dur}"
            if entry.get("error"):
                line += f" [red]{entry['error'][:60]}[/red]"
            lines.append(line)

        self.update("\n".join(lines))

    @staticmethod
    def _fmt_ms(ms: float | None) -> str:
        if ms is None:
            return ""
        if ms < 1000:
            return f"{int(ms)}ms"
        return f"{ms / 1000:.1f}s"


class ThinkingView(Static):
    """Collapsible thinking/reasoning text panel."""

    DEFAULT_CSS = """
    ThinkingView {
        height: auto;
        max-height: 10;
        border: solid $warning;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._segments: list[str] = []
        self._visible = True

    def append(self, text: str) -> None:
        self._segments.append(text)
        if self._visible:
            self._render()

    def toggle(self) -> None:
        self._visible = not self._visible
        self._render()

    def clear(self) -> None:
        self._segments.clear()
        self._visible = True

    def _render(self) -> None:
        if not self._segments:
            self.update("")
            return
        total = sum(len(s) for s in self._segments)
        header = f"💡 Thinking  ({len(self._segments)} segments, {total} chars)  [dim][T] toggle[/dim]"
        if not self._visible:
            self.update(f"{header}  [dim][collapsed][/dim]")
            return
        body = "\n".join(self._segments[-5:])  # show last 5 segments
        self.update(f"{header}\n{body}")


class PlanView(Static):
    """Multi-step plan display with checkmarks."""

    DEFAULT_CSS = """
    PlanView {
        height: auto;
        max-height: 10;
        border: solid $primary;
        padding: 0 1;
    }
    """

    STATUS_ICONS = {
        "pending": "[dim]☐[/dim]",
        "running": "[bold cyan]⏳[/bold cyan]",
        "done":    "[green]✅[/green]",
        "error":   "[red]❌[/red]",
    }

    def __init__(self) -> None:
        super().__init__("")
        self._steps: list[dict[str, Any]] = []

    def set_steps(self, steps: list[dict[str, Any]]) -> None:
        self._steps = steps
        self._render()

    def update_step(self, step_id: str, status: str, duration_ms: float | None = None) -> None:
        for s in self._steps:
            if s.get("id") == step_id:
                s["status"] = status
                if duration_ms is not None:
                    s["duration_ms"] = duration_ms
                break
        self._render()

    def clear(self) -> None:
        self._steps.clear()
        self.update("")

    def _render(self) -> None:
        if not self._steps:
            self.update("")
            return
        done = sum(1 for s in self._steps if s.get("status") == "done")
        lines = [f"📋 Plan ({done}/{len(self._steps)} steps)"]
        for s in self._steps:
            icon = self.STATUS_ICONS.get(s.get("status", "pending"), "?")
            desc = s.get("description", s.get("id", ""))
            dur = ""
            if s.get("duration_ms"):
                dur = " [dim]" + ToolLog._fmt_ms(s["duration_ms"]) + "[/dim]"
            lines.append("  " + icon + " " + desc + dur)
        self.update("\n".join(lines))


class CompactChatLog(Static):
    """Compact message transcript — shows only the last N exchanges."""

    DEFAULT_CSS = """
    CompactChatLog {
        height: auto;
        max-height: 8;
        border: solid $panel;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def __init__(self, agent_name: str = "XM") -> None:
        super().__init__("")
        self._agent_name = agent_name
        self._messages: list[dict[str, str]] = []
        self._max_visible = 5

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "text": text})
        self._prune()
        self._render()

    def add_agent(self, text: str) -> None:
        self._messages.append({"role": "agent", "text": text})
        self._prune()
        self._render()

    def add_system(self, text: str) -> None:
        self._messages.append({"role": "system", "text": text})
        self._prune()
        self._render()

    def _prune(self) -> None:
        while len(self._messages) > self._max_visible:
            self._messages.pop(0)

    def clear(self) -> None:
        self._messages.clear()
        self.update("")

    def _render(self) -> None:
        lines: list[str] = []
        for m in self._messages:
            role = m["role"]
            text = m["text"]
            if role == "user":
                lines.append(f"[bold cyan]You:[/bold cyan] {text[:120]}")
            elif role == "agent":
                lines.append(f"[bold green]{self._agent_name}:[/bold green] {text[:120]}")
            else:
                lines.append(f"[dim]{text[:120]}[/dim]")
        self.update("\n".join(lines))
