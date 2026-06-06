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
        "Create a visual artifact that renders inline in the chat transcript. "
        "This is your primary tool for ANY situation where visual structure "
        "improves understanding — not just when the user explicitly asks for a diagram.\n\n"
        "**When to use (proactive, not reactive):**\n"
        "  • Explaining architecture, workflows, or state machines — draw them.\n"
        "  • Comparing options (A vs B vs C) — use a table or chart.\n"
        "  • Walking through a multi-step process — sequence diagram or flowchart.\n"
        "  • Showing relationships (dependencies, inheritance, data flow) — graph.\n"
        "  • Presenting time-based events — timeline or Gantt chart.\n"
        "  • Breaking down a complex decision — decision tree.\n"
        "  • Summarizing categorical data — bar/pie chart.\n"
        "  • ANY time you think 'this would be clearer with a picture'.\n\n"
        "**When NOT to use:**\n"
        "  • The user just wants a quick text answer (no visual benefit).\n"
        "  • You're in the middle of a rapid debug loop — text is faster.\n"
        "  • The data is a single scalar or trivial pair (overkill).\n"
        "  • The frontend has no canvas renderer (fallback text will show, but it's not the intended experience).\n\n"
        "**Supported kinds:**\n"
        "  • mermaid — Universal diagram language.\n"
        "      Best for: flowcharts, sequence diagrams, class diagrams, "
        "      ER diagrams, state diagrams, Gantt charts, Git graphs, "
        "      mind maps, timelines, C4 architecture diagrams.\n"
        "  • html    — Rich HTML snippets.\n"
        "      Best for: styled cards, collapsible sections, "
        "      color-coded diffs, embedded iframes, dashboards.\n"
        "  • svg     — Raw vector graphics.\n"
        "      Best for: custom shapes, badges, icons, geometric art, "
        "      precise diagrams that Mermaid cannot express.\n"
        "  • chart   — Chart.js JSON configuration.\n"
        "      Best for: bar, line, area, scatter, bubble, pie, doughnut, "
        "      radar, polar area charts. Include labels, colors, datasets.\n"
        "  • table   — Structured data grid.\n"
        "      Best for: comparing entities, listing configurations, "
        "      showing API parameters, matrix comparisons.\n\n"
        "Returns an artifact_id. Use ``canvas_update`` to mutate it live "
        "(e.g., streaming partial results, animating progress), "
        "and ``canvas_close`` when the visual is no longer needed."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["mermaid", "html", "svg", "chart", "table"],
                "description": (
                    "Visual format. Choose the ONE best fit:\n"
                    "  mermaid = diagrams (most common, start here)\n"
                    "  html    = styled snippets / interactive widgets\n"
                    "  svg     = custom vector art\n"
                    "  chart   = data-driven Chart.js visualizations\n"
                    "  table   = structured row/column data"
                ),
            },
            "title": {
                "type": "string",
                "description": "Short descriptive title shown above the artifact",
            },
            "content": {
                "type": "string",
                "description": (
                    "The artifact payload. Kind-specific format:\n"
                    "  mermaid → Mermaid diagram definition (e.g. 'graph TD; A-->B')\n"
                    "  html    → HTML fragment WITHOUT <html>/<body> wrappers\n"
                    "  svg     → Raw <svg>...</svg> markup string\n"
                    "  chart   → Chart.js config as JSON string\n"
                    "  table   → JSON string: {headers:['col1','col2'], rows:[['a','b'],...]}"
                ),
            },
        },
        "required": ["kind", "title", "content"],
    },
)

_CANVAS_UPDATE_SPEC = ToolSpec(
    name="canvas_update",
    description=(
        "Update an existing canvas artifact in-place. "
        "The kind and title remain unchanged; only the payload is replaced.\n\n"
        "**Use this for:**\n"
        "  • Streaming incremental results (update a chart as data arrives).\n"
        "  • Progressive diagram building (add nodes/edges step by step).\n"
        "  • Live status updates (refresh a dashboard or progress bar).\n"
        "  • Correcting mistakes in a previously created artifact.\n"
        "  • Animation frames (flip through states of a state machine).\n\n"
        "If the artifact_id does not exist in this session, return an error."
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
        "Close (remove) a canvas artifact from the chat. "
        "The frontend collapses or deletes the visual.\n\n"
        "**Use this when:**\n"
        "  • The visual has served its purpose and now clutters the conversation.\n"
        "  • You are about to create a newer, better version of the same concept.\n"
        "  • The user explicitly asks to hide or remove a diagram.\n"
        "  • The session is transitioning to a completely unrelated topic.\n\n"
        "Closing is polite housekeeping — don't leave stale artifacts hanging around."
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
                        # 2026-06-06: carry the real session so the daemon's
                        # per-socket forwarder routes this to the originating
                        # chat. Pre-fix the listener stamped "_system" and
                        # _is_relevant() filtered it out → diagram never
                        # reached the UI (only the tool-result text showed).
                        "session_id": sid,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

        # B-Vis-Fallback: keep a short preview in the tool result so
        # the user has something readable if the frontend Canvas renderer
        # fails (CDN timeout, network partition, etc.).  Preview is
        # intentionally brief — the full artifact is rendered above.
        preview = content[:200]
        if len(content) > 200:
            preview += " …"
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=(
                f"[{title}] ({kind}) 已创建 · ID: {artifact_id}"
                f" — 预览: {preview}"
            ),
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
                        "session_id": sid,
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
                    {"artifact_id": artifact_id, "session_id": sid},
                )
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id,
            ok=True,
            content=f"Closed canvas artifact {artifact_id}",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
