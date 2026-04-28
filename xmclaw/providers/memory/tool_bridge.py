"""MemoryToolBridge — adapts ``MemoryManager`` into a ``ToolProvider``.

B-27. Lets memory providers expose their own LLM-callable tools
(``recall_memory``, ``synthesize`` etc.) without each provider needing
to know about the agent's tool registry. The bridge:

  • Walks ``manager.get_tool_schemas()`` once at construction
  • Builds an XMclaw ``ToolSpec`` per OpenAI-format schema
  • On ``invoke``, routes to the right provider's ``handle_tool_call``

Today no built-in provider exposes tools — BuiltinFileMemoryProvider
and SqliteVecMemory both return ``[]``. Future plugins (hindsight,
supermemory) will. The bridge is the wiring; it's a no-op until
something fills it.

Composition pattern: factory wraps (BuiltinTools, MemoryToolBridge)
in CompositeToolProvider, hands the composite to AgentLoop. Tool
name collisions surface at composite construction.
"""
from __future__ import annotations

import json
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class MemoryToolBridge(ToolProvider):
    """Adapt a :class:`MemoryManager` to the ``ToolProvider`` shape.

    OpenAI-format schemas from ``manager.get_tool_schemas()`` are
    converted to XMclaw ``ToolSpec`` instances. Each tool is routed
    back to whichever provider returned it via the manager's
    aggregated ``handle_tool_call`` dispatcher.
    """

    def __init__(self, memory_manager: Any) -> None:
        """``memory_manager`` is duck-typed — must expose
        ``get_tool_schemas()``, ``providers`` (list), and per-provider
        ``handle_tool_call(name, args, **kwargs)``."""
        self._mgr = memory_manager
        # Cache: tool name → provider that owns it. Built once at
        # construction so invoke() is O(1).
        self._owner: dict[str, Any] = {}
        self._specs: list[ToolSpec] = []
        self._refresh()

    def _refresh(self) -> None:
        self._owner.clear()
        specs: list[ToolSpec] = []
        if self._mgr is None:
            self._specs = specs
            return
        for p in getattr(self._mgr, "providers", []):
            try:
                schemas = p.get_tool_schemas() if hasattr(p, "get_tool_schemas") else []
            except Exception:  # noqa: BLE001
                continue
            for s in schemas:
                if not isinstance(s, dict):
                    continue
                # OpenAI-format: {"name", "description", "parameters"}.
                name = s.get("name") or ""
                if not name:
                    continue
                if name in self._owner:
                    # First provider wins on collision; warn-style log
                    # (no logger here to keep the module lean).
                    continue
                self._owner[name] = p
                specs.append(ToolSpec(
                    name=name,
                    description=str(s.get("description") or ""),
                    parameters_schema=dict(s.get("parameters") or {"type": "object"}),
                ))
        self._specs = specs

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        owner = self._owner.get(call.name)
        if owner is None:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown memory tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        handler = getattr(owner, "handle_tool_call", None)
        if handler is None:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"provider {getattr(owner, 'name', '?')!r} declared "
                      f"tool {call.name!r} but has no handle_tool_call",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            import inspect
            if inspect.iscoroutinefunction(handler):
                raw = await handler(call.name, call.args)
            else:
                raw = handler(call.name, call.args)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        # Provider returned either a string (Hermes contract) or a
        # dict / structured value. Normalise to string for ToolResult
        # content; clients can still parse JSON if needed.
        if isinstance(raw, str):
            content: Any = raw
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False)
            except (TypeError, ValueError):
                content = str(raw)
        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def refresh(self) -> None:
        """Re-scan provider tool schemas. Called when a provider is
        added / removed at runtime."""
        self._refresh()
