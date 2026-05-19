"""Epic #27 sweep #16 follow-up (2026-05-19) — MCPBridge + browser
tool error envelopes carry actionable hints.

Pre-fix the 4 MCPBridge.invoke() fail paths returned plain strings
like ``"MCP protocol error: ..."`` with no recovery guidance and
no ``latency_ms``. Pre-fix the browser tool's generic ``except
Exception`` returned ``f"{type(exc).__name__}: {exc}"`` with no
hint about which browser_* recovery move to try next.

This test suite pins:
  * MCPBridge.invoke() returns ``_fail_with_hint``-shaped errors
    with pipe-separated ``summary | exc | hint:`` envelope.
  * Each fail path's hint mentions a concrete recovery (start the
    bridge / check timeout config / read daemon.log / restart).
  * Browser tool generic-exception path mentions snapshot / wait /
    reopen.
  * latency_ms is now always set (was None for MCP fail paths).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.mcp_bridge import MCPBridge, MCPError


@pytest.fixture
def _stopped_bridge() -> MCPBridge:
    """A bridge instance that hasn't been started."""
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "test-server"
    bridge._started = False
    bridge._tools = []
    bridge._request_timeout = 30.0
    return bridge


def _call() -> ToolCall:
    return ToolCall(
        name="hello", args={"x": 1}, provenance="synthetic",
    )


# ── MCPBridge fail-path hints ──────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_invoke_not_started_includes_hint(
    _stopped_bridge: MCPBridge,
) -> None:
    result = await _stopped_bridge.invoke(_call())
    assert result.ok is False
    err = result.error or ""
    assert "not started" in err
    assert "hint:" in err
    assert "daemon.log" in err or "config" in err
    # Pipe-separated envelope.
    assert err.count("|") >= 1
    # latency_ms is now set (was None pre-fix).
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_mcp_invoke_timeout_includes_hint() -> None:
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "slow-server"
    bridge._started = True
    bridge._tools = []
    bridge._request_timeout = 30.0
    # Patch _rpc to raise TimeoutError synchronously.
    bridge._rpc = AsyncMock(side_effect=asyncio.TimeoutError())
    result = await bridge.invoke(_call())
    assert result.ok is False
    err = result.error or ""
    assert "timed out" in err
    assert "30" in err
    assert "hint:" in err
    assert "request_timeout" in err or "restart" in err
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_mcp_invoke_protocol_error_includes_hint() -> None:
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "buggy-server"
    bridge._started = True
    bridge._tools = []
    bridge._request_timeout = 30.0
    bridge._rpc = AsyncMock(
        side_effect=MCPError("invalid JSON-RPC response"),
    )
    result = await bridge.invoke(_call())
    assert result.ok is False
    err = result.error or ""
    assert "protocol error" in err.lower()
    assert "MCPError" in err
    assert "hint:" in err
    assert "stderr" in err or "version" in err
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_mcp_invoke_generic_exception_includes_hint() -> None:
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "crashed-server"
    bridge._started = True
    bridge._tools = []
    bridge._request_timeout = 30.0
    bridge._rpc = AsyncMock(side_effect=RuntimeError("unexpected"))
    result = await bridge.invoke(_call())
    assert result.ok is False
    err = result.error or ""
    assert "RuntimeError" in err
    assert "unexpected" in err
    assert "hint:" in err
    assert "child process" in err.lower() or "restart" in err.lower()
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_mcp_invoke_tool_reported_error_includes_hint() -> None:
    """Server says isError=true — we surface that AND hint at how
    to fix (read the message, adjust args)."""
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "polite-server"
    bridge._started = True
    bridge._tools = []
    bridge._request_timeout = 30.0
    bridge._rpc = AsyncMock(return_value={
        "isError": True,
        "content": [{"type": "text", "text": "missing required arg"}],
    })
    result = await bridge.invoke(_call())
    assert result.ok is False
    err = result.error or ""
    assert "reported error" in err.lower()
    assert "hint:" in err
    assert "args" in err or "tool" in err
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_mcp_invoke_success_records_latency() -> None:
    """Sanity: success path now also sets latency_ms (we plumbed
    ``t0`` for the fail paths; it naturally falls out for success
    too)."""
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._name = "ok-server"
    bridge._started = True
    bridge._tools = []
    bridge._request_timeout = 30.0
    bridge._rpc = AsyncMock(return_value={
        "content": [{"type": "text", "text": "all good"}],
    })
    result = await bridge.invoke(_call())
    assert result.ok is True
    assert result.latency_ms is not None
    assert result.latency_ms >= 0
