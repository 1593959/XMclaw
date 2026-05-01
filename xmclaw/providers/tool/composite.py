"""CompositeToolProvider — merge multiple ``ToolProvider`` instances.

The daemon builds BuiltinTools unconditionally; optional extras
(BrowserTools, LSPTools) get wrapped on top when their pyproject extra
is installed and the config enables them. ``CompositeToolProvider``
stitches them into a single provider so AgentLoop doesn't care about
which underlying module owns a given tool name.

Semantics:
  - ``list_tools`` concatenates each child's specs in the order they
    were passed. Name collisions raise at construction time -- we'd
    rather fail loud than silently shadow a tool.
  - ``invoke`` routes by name: the first child whose ``list_tools``
    contains a matching spec handles the call. Missing name -> the
    structured ``unknown tool`` error from the last child (or a
    synthesized error if there are no children).

The AgentLoop-maintained session_id on ToolCall is passed through
unchanged, so session-scoped state (Todo lists, browser contexts) Just
Works.
"""
from __future__ import annotations

import time

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class CompositeToolProvider(ToolProvider):
    def __init__(self, *children: ToolProvider) -> None:
        self._children: list[ToolProvider] = list(children)
        # Resolve tool name -> child index once at construction so
        # invoke() is O(1) per call instead of re-scanning every time.
        self._router: dict[str, ToolProvider] = {}
        for child in self._children:
            for spec in child.list_tools():
                if spec.name in self._router:
                    raise ValueError(
                        f"tool name collision: {spec.name!r} already "
                        f"provided by {type(self._router[spec.name]).__name__}"
                    )
                self._router[spec.name] = child

    def list_tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for child in self._children:
            out.extend(child.list_tools())
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        child = self._router.get(call.name)
        # B-124: fall back to a live re-scan when the static router
        # misses. The router is built once at construction, but some
        # children (e.g. SkillToolProvider) advertise tools that come
        # and go as the SkillRegistry's HEAD changes. ``list_tools``
        # already polls children fresh; ``invoke`` should match —
        # otherwise a freshly-registered skill would appear in the
        # LLM's tool spec but fail on invocation.
        if child is None:
            for c in self._children:
                if any(s.name == call.name for s in c.list_tools()):
                    self._router[call.name] = c
                    child = c
                    break
        if child is None:
            t0 = time.perf_counter()
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return await child.invoke(call)

    async def close_session(self, session_id: str) -> None:
        """Fan-out to children that implement session teardown."""
        for child in self._children:
            close = getattr(child, "close_session", None)
            if callable(close):
                try:
                    await close(session_id)
                except Exception:  # noqa: BLE001
                    pass

    async def shutdown(self) -> None:
        """Fan-out daemon-shutdown hook to children that support it."""
        for child in self._children:
            shut = getattr(child, "shutdown", None)
            if callable(shut):
                try:
                    await shut()
                except Exception:  # noqa: BLE001
                    pass

    # Passthrough accessors so tests can still peek at the first child
    # (BuiltinTools for today's single-provider assertions).
    @property
    def children(self) -> list[ToolProvider]:
        return list(self._children)
