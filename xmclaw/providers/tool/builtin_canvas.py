"""Canvas tools — agent-generated visual artifacts (A2UI / Live Canvas).

Phase 6+ delivery. Gives the agent the ability to create, update, and close
visual artifacts that render inline in the chat transcript:

  * mermaid  — flowcharts, sequence diagrams, class diagrams, etc.
  * html     — rich HTML snippets (tables, styled divs, embedded widgets)
  * svg      — inline vector graphics
  * chart    — Chart.js spec (bar, line, pie, radar, etc.)
  * table    — structured data table (rows + columns)

Each tool call emits a BehavioralEvent so the frontend reducer can surface
live mutations without polling.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from xmclaw.core.bus import EventType, make_event
from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool.base import ToolSpec


_VALID_KINDS = {"mermaid", "html", "svg", "chart", "table"}

_CANVAS_CREATE_SPEC = ToolSpec(
    name="canvas_create",
    description=(
        "Create a visual artifact that renders inline in the chat. "
        "Use this when the user asks for diagrams, data visualization, "
        "structured comparison tables, or any explanation that benefits "
        "from visual layout.\n\n"
        "Supported kinds:\n"
        "  • mermaid — flowcharts, sequence diagrams, class diagrams, "
        "Gantt charts (Mermaid syntax)\n"
        "  • html    — rich HTML snippets, styled containers, embedded widgets\n"
        "  • svg     — inline vector graphics (raw SVG markup)\n"
        "  • chart   — Chart.js JSON spec (bar, line, pie, radar, doughnut, polarArea)\n"
        "  • table   — structured data table (Markdown-like rows + columns)\n\n"
        "Returns an artifact_id. Use ``canvas_update`` to mutate it later, "
        "and ``canvas_close`` when you're done."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["mermaid", "html", "svg", "chart", "table"],
                "description": "Visual format of the artifact",
            },
            "title": {
                "type": "string",
                "description": "Short title shown above the artifact",
            },
            "content": {
                "type": "string",
                "description": (
                    "The artifact payload. Kind-specific:\n"
                    "  mermaid → Mermaid diagram definition\n"
                    "  html    → HTML fragment (no <html>/<body> wrappers)\n"
                    "  svg     → Raw <svg>...</svg> markup\n"
                    "  chart   → Chart.js config JSON string\n"
                    "  table   → JSON string: {headers:[], rows:[]})"
                ),
            },
        },
        "required": ["kind", "title", "content"],
    },
)

_CANVAS_UPDATE_SPEC = ToolSpec(
    name="canvas_update",
    description=(
        "Update an existing canvas artifact by replacing its content. "
        "The kind and title stay the same; only the payload changes. "
        "Use this for live data updates, progressive diagram building, "
        "or animation frames."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "The id returned by the original canvas_create call",
            },
            "content": {
                "type": "string",
                "description": "New payload (same format rules as canvas_create)",
            },
        },
        "required": ["artifact_id", "content"],
    },
)

_CANVAS_CLOSE_SPEC = ToolSpec(
    name="canvas_close",
    description=(
        "Close a canvas artifact. The frontend collapses or removes it. "
        "Use this when the visual is no longer relevant to the conversation."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "The id returned by canvas_create",
            },
        },
        "required": ["artifact_id"],
    },
)


def _fail(call: ToolCall, t0: float, msg: str) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        ok=False,
        content=msg,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


class BuiltinToolsCanvasMixin:
    """Canvas artifact tools: canvas_create, canvas_update, canvas_close."""

    # Optional callback fired on every canvas mutation so the agent loop /
    # daemon can emit CANVAS_ARTIFACT_* events to the bus.
    # Signature: ``def listener(event_type, payload) -> None``.
    _canvas_listener: Any | None = None

    # In-memory registry of active artifacts per session.
    # Key: session_id → {artifact_id: {kind, title, content}}
    _canvas_registry: dict[str, dict[str, dict[str, str]]] | None = None

    def _ensure_canvas_registry(self) -> dict[str, dict[str, dict[str, str]]]:
        if self._canvas_registry is None:
            self._canvas_registry = {}
        return self._canvas_registry

    async def _canvas_create(self, call: ToolCall, t0: float) -> ToolResult:
        kind = call.args.get("kind")
        title = call.args.get("title", "")
        content = call.args.get("content", "")
        if kind not in _VALID_KINDS:
            return _fail(
                call, t0,
                f"invalid kind {kind!r}; expected one of: {sorted(_VALID_KINDS)}",
            )
        if not isinstance(title, str) or not title.strip():
            return _fail(call, t0, "missing or empty 'title'")
        if not isinstance(content, str):
            return _fail(call, t0, "'content' must be a string")

        artifact_id = "art_" + uuid.uuid4().hex[:12]
        sid = call.session_id or "_default"
        reg = self._ensure_canvas_registry()
        reg.setdefault(sid, {})[artifact_id] = {
            "kind": kind,
            "title": title,
            "content": content,
        }

        if self._canvas_listener is not None:
            try:
                self._canvas_listener(
                    EventType.CANVAS_ARTIFACT_CREATED,
                    {
                        "artifact_id": artifact_id,
                        "kind": kind,
                        "title": title,
                        "content": content,
                        "turn_id": call.id or "",
                    },
                )
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id,
            ok=True,
            content=f"Created canvas artifact {artifact_id} ({kind}): {title}",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _canvas_update(self, call: ToolCall, t0: float) -> ToolResult:
        artifact_id = call.args.get("artifact_id")
        content = call.args.get("content", "")
        if not isinstance(artifact_id, str) or not artifact_id:
            return _fail(call, t0, "missing or empty 'artifact_id'")
        if not isinstance(content, str):
            return _fail(call, t0, "'content' must be a string")

        sid = call.session_id or "_default"
        reg = self._ensure_canvas_registry()
        session_arts = reg.get(sid, {})
        if artifact_id not in session_arts:
            return _fail(call, t0, f"artifact {artifact_id!r} not found in this session")

        session_arts[artifact_id]["content"] = content

        if self._canvas_listener is not None:
            try:
                self._canvas_listener(
                    EventType.CANVAS_ARTIFACT_UPDATED,
                    {
                        "artifact_id": artifact_id,
                        "content": content,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id,
            ok=True,
            content=f"Updated canvas artifact {artifact_id}",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _canvas_close(self, call: ToolCall, t0: float) -> ToolResult:
        artifact_id = call.args.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return _fail(call, t0, "missing or empty 'artifact_id'")

        sid = call.session_id or "_default"
        reg = self._ensure_canvas_registry()
        session_arts = reg.get(sid, {})
        if artifact_id not in session_arts:
            return _fail(call, t0, f"artifact {artifact_id!r} not found in this session")

        del session_arts[artifact_id]

        if self._canvas_listener is not None:
            try:
                self._canvas_listener(
                    EventType.CANVAS_ARTIFACT_CLOSED,
                    {"artifact_id": artifact_id},
                )
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id,
            ok=True,
            content=f"Closed canvas artifact {artifact_id}",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
