"""CompositeToolProvider -- the fan-out wrapper used when more than
one ToolProvider is wired in (BuiltinTools + BrowserTools + LSPTools).
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.composite import CompositeToolProvider


class _Stub(ToolProvider):
    def __init__(self, name: str, reply: str = "ok") -> None:
        self._name = name; self._reply = reply
        self.session_closed: list[str] = []
        self.shut_called = False

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name=self._name, description="stub",
            parameters_schema={"type": "object", "properties": {}},
        )]

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, ok=(call.name == self._name),
            content=self._reply, side_effects=(),
            error=None if call.name == self._name else f"wrong provider for {call.name}",
        )

    async def close_session(self, session_id: str) -> None:
        self.session_closed.append(session_id)

    async def shutdown(self) -> None:
        self.shut_called = True


def test_list_tools_concatenates_in_order() -> None:
    a, b = _Stub("a"), _Stub("b")
    c = CompositeToolProvider(a, b)
    assert [s.name for s in c.list_tools()] == ["a", "b"]


def test_name_collision_raises_at_construction() -> None:
    a1, a2 = _Stub("same"), _Stub("same")
    with pytest.raises(ValueError, match="collision"):
        CompositeToolProvider(a1, a2)


@pytest.mark.asyncio
async def test_invoke_routes_by_name() -> None:
    a, b = _Stub("a", "from-a"), _Stub("b", "from-b")
    c = CompositeToolProvider(a, b)
    ra = await c.invoke(ToolCall(name="a", args={}, provenance="synthetic"))
    assert ra.ok is True and ra.content == "from-a"
    rb = await c.invoke(ToolCall(name="b", args={}, provenance="synthetic"))
    assert rb.ok is True and rb.content == "from-b"


@pytest.mark.asyncio
async def test_unknown_name_returns_structured_error() -> None:
    a = _Stub("a")
    c = CompositeToolProvider(a)
    r = await c.invoke(ToolCall(name="nope", args={}, provenance="synthetic"))
    assert r.ok is False
    assert "unknown tool" in r.error


@pytest.mark.asyncio
async def test_close_session_fans_out_to_all_children() -> None:
    a, b = _Stub("a"), _Stub("b")
    c = CompositeToolProvider(a, b)
    await c.close_session("sess-42")
    assert a.session_closed == ["sess-42"]
    assert b.session_closed == ["sess-42"]


@pytest.mark.asyncio
async def test_shutdown_fans_out_to_all_children() -> None:
    a, b = _Stub("a"), _Stub("b")
    c = CompositeToolProvider(a, b)
    await c.shutdown()
    assert a.shut_called is True
    assert b.shut_called is True


@pytest.mark.asyncio
async def test_child_that_doesnt_implement_close_session_is_skipped() -> None:
    """Composite must not crash if a child lacks optional lifecycle hooks."""

    class _NoLifecycle(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(
                name="bare", description="",
                parameters_schema={"type": "object", "properties": {}},
            )]
        async def invoke(self, call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content=None, side_effects=())

    c = CompositeToolProvider(_NoLifecycle(), _Stub("x"))
    await c.close_session("s1")   # must not raise
    await c.shutdown()            # must not raise
