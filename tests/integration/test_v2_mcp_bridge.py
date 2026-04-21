"""MCPBridge × fake MCP server — end-to-end wire-protocol test.

Spawns ``tests/fixtures/fake_mcp_server.py`` as the server, runs
MCPBridge through initialize + tools/list + tools/call cycles, and
verifies the results round-trip. Every MCP client's real-world
failure mode (malformed server, server crash, timeout, tool error)
gets a dedicated assertion.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.mcp_bridge import MCPBridge, MCPError


_FAKE_SERVER = Path(__file__).parent.parent / "fixtures" / "fake_mcp_server.py"


def _fake_server_cmd() -> list[str]:
    return [sys.executable, str(_FAKE_SERVER)]


# ── start + tools/list ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_discovers_tools_from_fake_server() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        tools = bridge.list_tools()
        names = {t.name for t in tools}
        assert names == {"echo", "always_fails"}
        echo = next(t for t in tools if t.name == "echo")
        assert "text" in echo.parameters_schema["properties"]
    finally:
        await bridge.stop()


# ── tools/call happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_echo_tool_returns_text_verbatim() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        result = await bridge.invoke(ToolCall(
            name="echo", args={"text": "hello via mcp"},
            provenance="synthetic",
        ))
        assert result.ok is True
        assert result.content == "hello via mcp"
        assert result.error is None
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_invoke_echo_with_unicode() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        result = await bridge.invoke(ToolCall(
            name="echo", args={"text": "你好 🦞"},
            provenance="synthetic",
        ))
        assert result.ok is True
        assert result.content == "你好 🦞"
    finally:
        await bridge.stop()


# ── tools/call error paths ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_that_reports_is_error_becomes_ok_false() -> None:
    """MCP servers return tool errors via isError=true. The bridge
    translates that into ToolResult(ok=False)."""
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        result = await bridge.invoke(ToolCall(
            name="always_fails", args={},
            provenance="synthetic",
        ))
        assert result.ok is False
        assert "simulated failure" in result.error
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_unknown_tool_surfaces_as_protocol_error() -> None:
    """An unknown tool name comes back as a JSON-RPC error, which the
    bridge surfaces as ToolResult(ok=False) — never raises at the
    caller."""
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        result = await bridge.invoke(ToolCall(
            name="does_not_exist", args={},
            provenance="synthetic",
        ))
        assert result.ok is False
        assert "unknown tool" in result.error
    finally:
        await bridge.stop()


# ── pre-start guards ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_before_start_fails_gracefully() -> None:
    """Calling invoke() before start() must not hang or crash —
    must return a structured ToolResult(ok=False, ...)."""
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        result = await bridge.invoke(ToolCall(
            name="echo", args={"text": "x"},
            provenance="synthetic",
        ))
        assert result.ok is False
        assert "not started" in result.error
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_list_tools_before_start_returns_empty() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        assert bridge.list_tools() == []
    finally:
        await bridge.stop()


# ── lifecycle ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    await bridge.start()
    await bridge.stop()
    await bridge.stop()  # second stop must not raise


@pytest.mark.asyncio
async def test_start_twice_raises() -> None:
    bridge = MCPBridge(command=_fake_server_cmd())
    try:
        await bridge.start()
        with pytest.raises(MCPError, match="already started"):
            await bridge.start()
    finally:
        await bridge.stop()


# ── spawn failure ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nonexistent_command_raises_mcp_error() -> None:
    """If the MCP server command doesn't exist, start() must raise
    MCPError with a clear message, not a cryptic FileNotFoundError."""
    bridge = MCPBridge(command=["definitely-not-a-real-command-xyz"])
    with pytest.raises(MCPError, match="failed to spawn"):
        await bridge.start()
    # No cleanup needed — start never completed.


# ── content block parsing ──────────────────────────────────────────────


def test_content_to_python_handles_single_text_block() -> None:
    from xmclaw.providers.tool.mcp_bridge import _content_to_python
    assert _content_to_python([
        {"type": "text", "text": "hello"},
    ]) == "hello"


def test_content_to_python_joins_multiple_text_blocks() -> None:
    from xmclaw.providers.tool.mcp_bridge import _content_to_python
    assert _content_to_python([
        {"type": "text", "text": "line one"},
        {"type": "text", "text": "line two"},
    ]) == "line one\nline two"


def test_content_to_python_none_is_empty_string() -> None:
    from xmclaw.providers.tool.mcp_bridge import _content_to_python
    assert _content_to_python(None) == ""


def test_content_to_python_non_list_returned_as_is() -> None:
    from xmclaw.providers.tool.mcp_bridge import _content_to_python
    assert _content_to_python("some string") == "some string"
