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

from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class CompositeToolProvider(ToolProvider):
    def __init__(
        self, *children: ToolProvider, automation_observe_required: bool = True,
    ) -> None:
        self._children: list[ToolProvider] = list(children)
        self._automation_observed_at: dict[tuple[str, str], float] = {}
        self._automation_observe_required = bool(automation_observe_required)
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

    def invalidate_router(self) -> None:
        """Rebuild the name → child router from fresh child scans.

        Called when a child (e.g. SkillToolProvider) advertises tools
        that change after construction — new skill registration, promotion,
        rollback, or uninstall. After invalidation ``invoke()`` routes
        via the rebuilt table without falling back to a live re-scan.
        """
        self._router.clear()
        for child in self._children:
            for spec in child.list_tools():
                if spec.name in self._router:
                    # Collision policy: first child wins (same as __init__).
                    # In practice this shouldn't happen post-invalidation
                    # unless two children advertise the same name.
                    continue
                self._router[spec.name] = child

    async def invoke(self, call: ToolCall) -> ToolResult:
        child = self._router.get(call.name)
        if child is None:
            t0 = time.perf_counter()
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        pre_observation = await self._automation_preflight(child, call)
        result = await child.invoke(call)
        if pre_observation is not None:
            return self._attach_pre_observation(result, pre_observation)
        return result

    async def _automation_preflight(
        self, child: ToolProvider, call: ToolCall,
    ) -> ToolResult | None:
        surface_action = self._automation_surface_action(call)
        if surface_action is None:
            return None
        if not self._automation_observe_required:
            return None
        surface, action = surface_action
        if action in {"observe", "trace"}:
            key = (call.session_id or "default", surface)
            self._automation_observed_at[key] = time.time()
            return None

        key = (call.session_id or "default", surface)
        observed_at = self._automation_observed_at.get(key, 0.0)
        if time.time() - observed_at < 5.0:
            return None
        observe_name = "browser" if surface == "browser" else "computer_use"
        observe_call = ToolCall(
            name=observe_name,
            args={"action": "observe", "include_action_log": False},
            provenance=getattr(call, "provenance", "synthetic"),
            session_id=getattr(call, "session_id", None),
        )
        observed = await child.invoke(observe_call)
        self._automation_observed_at[key] = time.time()
        return observed

    def _automation_surface_action(self, call: ToolCall) -> tuple[str, str] | None:
        args = call.args or {}
        name = call.name
        if name == "browser":
            return "browser", str(args.get("action", "")).strip().lower()
        if name.startswith("browser_"):
            legacy_action = {
                "browser_open": "navigate",
                "browser_click": "click",
                "browser_press": "press",
                "browser_fill": "fill",
                "browser_hover": "hover",
                "browser_scroll": "scroll",
                "browser_select_option": "select_option",
                "browser_upload": "upload",
                "browser_back": "back",
                "browser_forward": "forward",
                "browser_reload": "reload",
                "browser_tab_switch": "tab_switch",
                "browser_tab_close": "tab_close",
                "browser_click_ref": "click_ref",
                "browser_type_ref": "type_ref",
                "browser_dialog": "dialog",
            }.get(name, "")
            if legacy_action:
                return "browser", legacy_action
        if name == "computer_use":
            return "computer", str(args.get("action", "")).strip().lower()
        return None

    def _attach_pre_observation(
        self, result: ToolResult, observation: ToolResult,
    ) -> ToolResult:
        content: Any = result.content
        if isinstance(content, dict):
            merged_content = dict(content)
        elif isinstance(content, str):
            merged_content = {"text": content}
        else:
            merged_content = {"value": content}
        merged_content["pre_observation"] = {
            "ok": bool(observation.ok),
            "content": observation.content,
            "error": observation.error,
        }
        metadata = dict(result.metadata or {})
        metadata["pre_observation"] = merged_content["pre_observation"]
        return ToolResult(
            call_id=result.call_id,
            ok=result.ok,
            content=merged_content,
            error=result.error,
            latency_ms=result.latency_ms,
            side_effects=result.side_effects,
            schema_version=result.schema_version,
            metadata=metadata,
        )

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
