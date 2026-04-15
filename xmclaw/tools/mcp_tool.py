"""MCP (Model Context Protocol) integration tool."""
import json
import asyncio
from typing import Any

from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class MCPTool(Tool):
    name = "mcp"
    description = (
        "Call tools exposed by an MCP (Model Context Protocol) server. "
        "Actions: list_servers, list_tools, call."
    )

    def __init__(self):
        self._available = False
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            self._available = True
        except ImportError:
            pass

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_servers", "list_tools", "call"],
                        "description": "MCP action",
                    },
                    "server_name": {
                        "type": "string",
                        "description": "Name of the configured MCP server",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Tool name to call on the MCP server",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments for the MCP tool call",
                        "default": {},
                    },
                },
                "required": ["action"],
            },
        }

    async def execute(self, action: str, **kwargs) -> str:
        if not self._available:
            return (
                "[Error: mcp package is not installed. "
                "Run: pip install mcp to enable MCP integration.]"
            )

        action = action.lower()
        if action == "list_servers":
            return self._list_servers()
        elif action == "list_tools":
            server_name = kwargs.get("server_name", "")
            return await self._list_tools(server_name)
        elif action == "call":
            server_name = kwargs.get("server_name", "")
            tool_name = kwargs.get("tool_name", "")
            arguments = kwargs.get("arguments", {})
            if not server_name or not tool_name:
                return "[Error: call requires server_name and tool_name]"
            return await self._call_tool(server_name, tool_name, arguments)
        else:
            return f"[Error: Unknown action '{action}']"

    def _list_servers(self) -> str:
        """List configured MCP servers from daemon config."""
        from xmclaw.daemon.config import DaemonConfig
        config = DaemonConfig.load()
        mcp_servers = getattr(config, "mcp_servers", {})
        if not mcp_servers:
            return "No MCP servers configured."
        lines = ["Configured MCP servers:"]
        for name, info in mcp_servers.items():
            cmd = info.get("command", info.get("url", "unknown"))
            args = " ".join(info.get("args", []))
            lines.append(f"  - {name}: {cmd} {args}")
        return "\n".join(lines)

    async def _list_tools(self, server_name: str) -> str:
        try:
            result = await self._with_session(server_name, self._do_list_tools)
            return result
        except Exception as e:
            logger.error("mcp_list_tools_failed", server=server_name, error=str(e))
            return f"[Error listing tools: {e}]"

    async def _do_list_tools(self, session) -> str:
        tools_result = await session.list_tools()
        tools = getattr(tools_result, "tools", tools_result)
        lines = [f"Tools available:"]
        for tool in tools:
            name = getattr(tool, "name", str(tool))
            desc = getattr(tool, "description", "")
            lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)

    async def _call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        try:
            result = await self._with_session(server_name, lambda s: s.call_tool(tool_name, arguments))
            return self._format_result(result)
        except Exception as e:
            logger.error("mcp_call_tool_failed", server=server_name, tool=tool_name, error=str(e))
            return f"[Error calling MCP tool: {e}]"

    def _format_result(self, result) -> str:
        """Format CallToolResult into a string."""
        if hasattr(result, "content"):
            parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(result)

    async def _with_session(self, server_name: str, callback):
        """Create a temporary MCP session, run callback, then clean up."""
        from xmclaw.daemon.config import DaemonConfig
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        config = DaemonConfig.load()
        mcp_servers = getattr(config, "mcp_servers", {})
        if server_name not in mcp_servers:
            raise RuntimeError(f"MCP server '{server_name}' not configured")

        server_config = mcp_servers[server_name]
        command = server_config.get("command", "")
        args = server_config.get("args", [])
        env = server_config.get("env")

        if not command:
            raise RuntimeError(f"MCP server '{server_name}' has no command")

        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await callback(session)
