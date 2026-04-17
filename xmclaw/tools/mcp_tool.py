"""MCP (Model Context Protocol) integration tool.

Supports three transport modes:
- stdio: Local subprocess (command + args). Default.
- sse:  HTTP Server-Sent Events endpoint.
- ws:   WebSocket endpoint.
"""
import asyncio
import json
from typing import Any

from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class MCPTool(Tool):
    name = "mcp"
    description = (
        "Call tools exposed by an MCP (Model Context Protocol) server. "
        "Supports stdio (local), SSE (HTTP), and WebSocket transport. "
        "Actions: list_servers, list_tools, call."
    )

    def __init__(self):
        self._available = False
        self._sessions: dict[str, Any] = {}  # cached sessions per server
        try:
            from mcp import ClientSession
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
            transport = info.get("transport", "stdio")
            if transport == "stdio":
                cmd = info.get("command", "")
                args = " ".join(info.get("args", []))
                lines.append(f"  - {name}: stdio → {cmd} {args}")
            elif transport == "sse":
                url = info.get("url", "")
                lines.append(f"  - {name}: SSE → {url}")
            elif transport == "ws":
                url = info.get("url", "")
                lines.append(f"  - {name}: WebSocket → {url}")
            else:
                lines.append(f"  - {name}: {transport}")
        return "\n".join(lines)

    async def _list_tools(self, server_name: str) -> str:
        try:
            session = await self._get_or_create_session(server_name)
            result = await self._do_list_tools(session)
            return result
        except Exception as e:
            logger.error("mcp_list_tools_failed", server=server_name, error=str(e))
            return f"[Error listing tools: {e}]"

    async def _do_list_tools(self, session) -> str:
        tools_result = await session.list_tools()
        tools = getattr(tools_result, "tools", tools_result)
        lines = ["Tools available:"]
        for tool in tools:
            name = getattr(tool, "name", str(tool))
            desc = getattr(tool, "description", "")
            lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)

    async def _call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        try:
            session = await self._get_or_create_session(server_name)
            result = await session.call_tool(tool_name, arguments)
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

    async def _get_or_create_session(self, server_name: str):
        """Get or create a cached MCP session for the server.

        Sessions are cached to avoid reconnection overhead.
        """
        if server_name in self._sessions:
            return self._sessions[server_name]

        from xmclaw.daemon.config import DaemonConfig
        config = DaemonConfig.load()
        mcp_servers = getattr(config, "mcp_servers", {})
        if server_name not in mcp_servers:
            raise RuntimeError(f"MCP server '{server_name}' not configured")

        server_config = mcp_servers[server_name]
        transport = server_config.get("transport", "stdio")

        if transport == "stdio":
            session = await self._create_stdio_session(server_config)
        elif transport == "sse":
            session = await self._create_sse_session(server_name, server_config)
        elif transport == "ws":
            session = await self._create_ws_session(server_name, server_config)
        else:
            raise RuntimeError(f"Unknown MCP transport: {transport}")

        self._sessions[server_name] = session
        return session

    async def _create_stdio_session(self, server_config: dict):
        """Create session via stdio (local subprocess)."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        from mcp.client.stdio import StdioServerParameters

        command = server_config.get("command", "")
        args = server_config.get("args", [])
        env = server_config.get("env")

        if not command:
            raise RuntimeError("MCP stdio server has no command")

        params = StdioServerParameters(command=command, args=args, env=env)
        read, write = await stdio_client(params).__aenter__()
        session = ClientSession(read, write)
        await session.initialize()
        return session

    async def _create_sse_session(self, server_name: str, server_config: dict):
        """Create session via HTTP Server-Sent Events.

        Connects to an SSE endpoint that streams MCP protocol messages.
        Requires the mcp package with sse_client support.
        """
        try:
            from mcp.client.sse import sse_client
        except ImportError:
            raise RuntimeError(
                "MCP SSE not available. Install: pip install 'mcp[sse]' "
                "or upgrade: pip install 'mcp>=1.0.0'"
            )

        url = server_config.get("url", "")
        headers = server_config.get("headers", {})
        if not url:
            raise RuntimeError(f"MCP server '{server_name}' has no SSE URL")

        try:
            read, write = await sse_client(url, headers=headers).__aenter__()
        except Exception as e:
            raise RuntimeError(f"MCP SSE connection failed: {e}")

        from mcp import ClientSession
        session = ClientSession(read, write)
        await session.initialize()
        return session

    async def _create_ws_session(self, server_name: str, server_config: dict):
        """Create session via WebSocket.

        Connects to a WebSocket endpoint that speaks the MCP protocol.
        """
        url = server_config.get("url", "")
        headers = server_config.get("headers", {})
        if not url:
            raise RuntimeError(f"MCP server '{server_name}' has no WebSocket URL")

        try:
            import websockets
            import json
            ws = await websockets.connect(url, extra_headers=headers or {})
        except ImportError:
            raise RuntimeError("pip install websockets to use MCP WebSocket transport")
        except Exception as e:
            raise RuntimeError(f"MCP WebSocket connection failed: {e}")

        # Wrap WebSocket as async reader/writer for MCP ClientSession
        from xmclaw.tools.mcp_ws_transport import WebSocketTransport
        transport = WebSocketTransport(ws)
        from mcp import ClientSession
        session = ClientSession(transport)
        await session.initialize()
        return session

    async def close(self) -> None:
        """Close all cached sessions."""
        for name, session in list(self._sessions.items()):
            try:
                if hasattr(session, "close"):
                    await session.close()
                elif hasattr(session, "__aexit__"):
                    await session.__aexit__(None, None, None)
            except Exception:
                pass
            del self._sessions[name]
