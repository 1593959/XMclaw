"""LSPTools unit tests.

We don't launch a real pylsp here -- tests cover the protocol surface
and the missing-dep error path. The behavioral integration with pylsp
lives in a smoke test you run manually with the [lsp] extra installed.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.lsp import LSPTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


def test_list_tools_is_hover_and_definition() -> None:
    names = {s.name for s in LSPTools().list_tools()}
    assert names == {"lsp_hover", "lsp_definition"}


def test_specs_are_well_formed() -> None:
    for spec in LSPTools().list_tools():
        assert spec.parameters_schema["type"] == "object"
        required = set(spec.parameters_schema.get("required", []))
        assert required == {"path", "line", "column"}


@pytest.mark.asyncio
async def test_missing_pylsp_returns_structured_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pylsp isn't a hard dep. Tools must refuse gracefully when invoked
    on an otherwise-valid file (we provide one so the path-exists guard
    doesn't short-circuit before the pylsp check)."""
    src = tmp_path / "x.py"; src.write_text("def f(): pass\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "pylsp", None)  # force ImportError
    tools = LSPTools()
    r = await tools.invoke(_call("lsp_hover", {
        "path": str(src), "line": 0, "column": 0,
    }))
    assert r.ok is False
    assert "python-lsp-server" in r.error
    assert "install" in r.error.lower()


@pytest.mark.asyncio
async def test_invalid_args_fail_before_server_boot(tmp_path) -> None:
    """Input validation shouldn't care whether pylsp is installed."""
    tools = LSPTools()
    # Missing path.
    r = await tools.invoke(_call("lsp_hover", {"line": 0, "column": 0}))
    assert r.ok is False
    assert "path" in r.error
    # Missing line/column.
    r = await tools.invoke(_call("lsp_hover", {
        "path": str(tmp_path / "x.py"), "line": "oops", "column": 0,
    }))
    assert r.ok is False
    assert "integer" in r.error


@pytest.mark.asyncio
async def test_nonexistent_file_rejected(tmp_path) -> None:
    tools = LSPTools()
    r = await tools.invoke(_call("lsp_hover", {
        "path": str(tmp_path / "nope.py"), "line": 0, "column": 0,
    }))
    assert r.ok is False
    assert "does not exist" in r.error


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_structured_error() -> None:
    tools = LSPTools()
    r = await tools.invoke(_call("lsp_what", {}))
    assert r.ok is False
    assert "unknown tool" in r.error
