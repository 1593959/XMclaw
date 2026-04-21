"""ToolCall IR — construction invariants."""
from __future__ import annotations

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec


def test_toolcall_is_frozen() -> None:
    call = ToolCall(name="read", args={"path": "/tmp/x"}, provenance="synthetic")
    assert call.name == "read"
    assert call.args == {"path": "/tmp/x"}
    # frozen dataclass — direct attribute assignment must fail
    try:
        call.name = "write"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ToolCall must be frozen")


def test_toolcall_ids_unique() -> None:
    a = ToolCall(name="x", args={}, provenance="synthetic")
    b = ToolCall(name="x", args={}, provenance="synthetic")
    assert a.id != b.id


def test_toolcallshape_values() -> None:
    # Test that all 4 shapes defined — anti-req #14 protocol coverage
    assert ToolCallShape.ANTHROPIC_NATIVE.value == "anthropic_native"
    assert ToolCallShape.OPENAI_TOOL.value == "openai_tool"
    assert ToolCallShape.OPENAI_JSONMODE.value == "openai_jsonmode"
    assert ToolCallShape.SYNTHETIC.value == "synthetic"


def test_toolresult_dataclass() -> None:
    res = ToolResult(call_id="x", ok=True, content={"data": 1})
    assert res.ok is True
    assert res.content == {"data": 1}


def test_toolspec_dataclass() -> None:
    spec = ToolSpec(name="read", description="read a file", parameters_schema={})
    assert spec.name == "read"
