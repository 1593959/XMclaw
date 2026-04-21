"""Tiny fake MCP server — used by tests/integration/test_v2_mcp_bridge.py.

Speaks just enough JSON-RPC 2.0 over stdin/stdout to support:
  * initialize
  * notifications/initialized
  * tools/list (returns two fake tools)
  * tools/call (echo + fail variants)

Launched by the test as a subprocess via ``MCPBridge(command=[sys.executable,
"tests/fixtures/fake_mcp_server.py"])``. Kept minimal so the test
exercises the client's protocol handling, not the server's.
"""
from __future__ import annotations

import io
import json
import sys

# On Windows, sys.stdin / sys.stdout default to the ANSI code page
# (cp936 / GBK for zh-CN), which mangles UTF-8 input. Real MCP servers
# written in Node/Rust don't hit this — but a Python test fixture has
# to reconfigure explicitly. Do it once at startup before any read.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
else:  # pragma: no cover — very old Python
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def _respond(req_id: int, result: dict) -> None:
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": req_id, "result": result,
    }) + "\n")
    sys.stdout.flush()


def _respond_error(req_id: int, message: str) -> None:
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32000, "message": message},
    }) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "fake-mcp-server", "version": "0.0.1",
                },
            })
        elif method == "notifications/initialized":
            # Notifications have no id and get no response.
            continue
        elif method == "tools/list":
            _respond(req_id, {
                "tools": [
                    {
                        "name": "echo",
                        "description": "returns its ``text`` argument verbatim",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "always_fails",
                        "description": "always returns an error — for test coverage",
                        "inputSchema": {"type": "object"},
                    },
                ],
            })
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = str(args.get("text", ""))
                _respond(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            elif name == "always_fails":
                _respond(req_id, {
                    "content": [{"type": "text", "text": "simulated failure"}],
                    "isError": True,
                })
            else:
                _respond_error(req_id, f"unknown tool: {name}")
        else:
            if req_id is not None:
                _respond_error(req_id, f"unknown method: {method}")


if __name__ == "__main__":
    main()
