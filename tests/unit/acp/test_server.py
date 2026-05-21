"""Tests for AcpServer."""
from __future__ import annotations


from xmclaw.acp.server import AcpServer


class FakeToolProvider:
    def get_tool_schemas(self):
        return [{"name": "echo", "description": "echo", "parameters": {}}]

    def invoke_tool(self, name, arguments):
        return f"echo:{name}:{arguments}"


class TestAcpServer:
    def test_initialize(self):
        srv = AcpServer()
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = srv._handle(req)
        assert resp is not None
        assert resp["result"]["serverInfo"]["name"] == "xmclaw-acp"

    def test_tools_list(self):
        srv = AcpServer(tool_provider=FakeToolProvider())
        # Must initialize first.
        srv._handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        srv._initialized = True
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = srv._handle(req)
        assert resp is not None
        assert resp["result"]["tools"][0]["name"] == "echo"

    def test_tools_call(self):
        srv = AcpServer(tool_provider=FakeToolProvider())
        srv._initialized = True
        req = {
            "jsonrpc": "2.0", "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"x": 1}},
        }
        resp = srv._handle(req)
        assert resp is not None
        assert "echo:echo" in resp["result"]["content"][0]["text"]

    def test_shutdown(self):
        srv = AcpServer()
        req = {"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": {}}
        resp = srv._handle(req)
        assert resp is not None
        assert resp["result"] is None
        assert not srv._running
