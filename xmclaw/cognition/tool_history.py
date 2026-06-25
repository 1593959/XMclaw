"""Tool history normalization for graph-state traces.

This is not the prompt-history pruner. `xmclaw.context.tool_result_prune`
rewrites old LLM messages to save context. This module produces compact,
checkpoint-friendly tool history entries for GraphState so planning and
reflection can reason over tool use without carrying full stdout/files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolHistoryEntry:
    step_id: str
    step_index: int
    tool_name: str
    ok: bool
    content_preview: str
    content_chars: int
    truncated: bool
    error: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "content_preview": self.content_preview,
            "content_chars": self.content_chars,
            "truncated": self.truncated,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


class ToolHistoryProcessor:
    """Compact tool outputs into stable graph-state entries."""

    def __init__(self, *, max_preview_chars: int = 2000) -> None:
        self.max_preview_chars = max(200, int(max_preview_chars))

    def from_output(
        self,
        *,
        step_id: str,
        step_index: int,
        tool_name: str,
        ok: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        latency_ms: float = 0.0,
    ) -> ToolHistoryEntry:
        text = _stringify_tool_output(output or {})
        truncated = len(text) > self.max_preview_chars
        preview = text[:self.max_preview_chars]
        if truncated:
            preview += "...[truncated]"
        return ToolHistoryEntry(
            step_id=step_id,
            step_index=step_index,
            tool_name=tool_name,
            ok=bool(ok),
            content_preview=preview,
            content_chars=len(text),
            truncated=truncated,
            error=error,
            latency_ms=float(latency_ms),
        )


def _stringify_tool_output(output: dict[str, Any]) -> str:
    for key in ("content", "stdout", "stderr", "text", "result"):
        val = output.get(key)
        if isinstance(val, str):
            return val
    try:
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(output)


__all__ = ["ToolHistoryEntry", "ToolHistoryProcessor"]
