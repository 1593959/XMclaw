"""MCP bridge — expose any Model Context Protocol server as a ToolProvider.

Anti-req #14: MCP is a first-class protocol. Phase 2 deliverable.
"""
from __future__ import annotations

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class MCPBridge(ToolProvider):
    def __init__(self, server_uri: str) -> None:
        self.server_uri = server_uri

    def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError("Phase 2")

    async def invoke(self, call: ToolCall) -> ToolResult:  # noqa: ARG002
        raise NotImplementedError("Phase 2")
