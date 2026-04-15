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
        self._sessions: dict[str, Any] = {}
        self._available = False
        try:
            import mcp
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
        """List configured MCP servers from agent config."""
        from xmclaw.daemon.config import DaemonConfig
        config = DaemonConfig.load()
        mcp_servers = getattr(config, "mcp_servers", {})
        if not mcp_servers:
            return "No MCP servers configured."
        lines = ["Configured MCP servers:"]
        for name, info in mcp_servers.items():
            lines.append(f"  - {name}: {info.get('command', info.get('url', 'unknown'))}")
        return "\n".join(lines)

    async def _list_tools(self, server_name: str) -> str:
        session = await self._get_session(server_name)
        if session is None:
            return f"[Error: MCP server '{server_name}' not found or failed to connect]"
        try:
            tools = await session.list_tools()
            lines = [f"Tools available on '{server_name}':"]
            for tool in tools:
                lines.append(f"  - {tool.name}: {tool.description}")
            return "\n".join(lines)
        except Exception as e:
            logger.error("mcp_list_tools_failed", server=server_name, error=str(e))
            return f"[Error listing tools: {e}]"

    async def _call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        session = await self._get_session(server_name)
        if session is None:
            return f"[Error: MCP server '{server_name}' not found or failed to connect]"
        try:
            result = await session.call_tool(tool_name, arguments)
            return f"[MCP result] {result}"
        except Exception as e:
            logger.error("mcp_call_tool_failed", server=server_name, tool=tool_name, error=str(e))
            return f"[Error calling MCP tool: {e}]"

    async def _get_session(self, server_name: str):
        if server_name in self._sessions:
            return self._sessions[server_name]

        from xmclaw.daemon.config import DaemonConfig
        config = DaemonConfig.load()
        mcp_servers = getattr(config, "mcp_servers", {})
        if server_name not in mcp_servers:
            return None

        server_config = mcp_servers[server_name]
        try:
            import mcp
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = server_config.get("command", "")
            args = server_config.get("args", [])
            env = server_config.get("env")

            params = StdioServerParameters(
                command=command,
                args=args,
                env=env,
            )
            transport = stdio_client(params)
            read, write = await transport.__aenter__()
            session = ClientSession(read, write)
            await session.initialize()
            self._sessions[server_name] = session
            logger.info("mcp_session_created", server=server_name)
            return session
        except Exception as e:
            logger.error("mcp_session_failed", server=server_name, error=str(e))
            return None
