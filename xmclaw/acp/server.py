"""AcpServer — JSON-RPC over stdio for IDE integration.

Implements the Agent Client Protocol (ACP) subset:
  * initialize / initialized
  * tools/list
  * tools/call
  * prompts/list
  * resources/list
  * shutdown / exit

Usage::

    python -m xmclaw.acp.server
"""
from __future__ import annotations

import json
import sys
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class AcpServer:
    """Minimal JSON-RPC server reading from stdin and writing to stdout.

    Parameters
    ----------
    tool_provider :
        Object with ``get_tool_schemas()`` and ``invoke_tool(name, args)``.
    prompt_provider :
        Object with ``list_prompts()`` → list[dict].
    resource_provider :
        Object with ``list_resources()`` → list[dict].
    """

    def __init__(
        self,
        *,
        tool_provider: Any | None = None,
        prompt_provider: Any | None = None,
        resource_provider: Any | None = None,
    ) -> None:
        self._tool_provider = tool_provider
        self._prompt_provider = prompt_provider
        self._resource_provider = resource_provider
        self._initialized = False
        self._running = True

    def run(self) -> None:
        """Block and serve requests from stdin until shutdown."""
        _log.info("acp.server.started")
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            except Exception as exc:  # noqa: BLE001
                _log.warning("acp.read_failed: %s", exc)
                continue

            resp = self._handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    def _handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        # Notifications (no id) don't get responses.
        is_notification = req_id is None

        def _result(data: Any) -> dict[str, Any] | None:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req_id, "result": data}

        def _error(code: int, message: str) -> dict[str, Any] | None:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

        try:
            if method == "initialize":
                return _result({
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "xmclaw-acp", "version": "1.0.0"},
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "prompts": {"listChanged": False},
                        "resources": {"listChanged": False},
                    },
                })
            if method == "initialized":
                self._initialized = True
                return None
            if method == "shutdown":
                self._running = False
                return _result(None)
            if method == "exit":
                self._running = False
                return None

            if not self._initialized and method != "initialize":
                return _error(-32002, "Server not initialized")

            if method == "tools/list":
                return _result({"tools": self._list_tools()})
            if method == "tools/call":
                return _result(self._call_tool(params))
            if method == "prompts/list":
                return _result({"prompts": self._list_prompts()})
            if method == "resources/list":
                return _result({"resources": self._list_resources()})

            return _error(-32601, f"Method not found: {method}")
        except Exception as exc:  # noqa: BLE001
            _log.warning("acp.method_failed method=%s err=%s", method, exc)
            return _error(-32603, "Internal server error")

    def _list_tools(self) -> list[dict[str, Any]]:
        if self._tool_provider is None:
            return []
        try:
            schemas = self._tool_provider.get_tool_schemas()
            return [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                    "inputSchema": s.get("parameters", {}),
                }
                for s in schemas
            ]
        except Exception as exc:  # noqa: BLE001
            _log.warning("acp.list_tools_failed: %s", exc)
            return []

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        if self._tool_provider is None:
            return {"content": [{"type": "text", "text": "No tool provider wired"}], "isError": True}
        try:
            result = self._tool_provider.invoke_tool(name, arguments)
            return {"content": [{"type": "text", "text": str(result)}], "isError": False}
        except Exception as exc:  # noqa: BLE001
            _log.warning("acp.tool_call_failed name=%s err=%s", name, exc)
            return {"content": [{"type": "text", "text": "Tool execution failed"}], "isError": True}

    def _list_prompts(self) -> list[dict[str, Any]]:
        if self._prompt_provider is None:
            return []
        try:
            return list(self._prompt_provider.list_prompts())
        except Exception as exc:  # noqa: BLE001
            _log.warning("acp.list_prompts_failed: %s", exc)
            return []

    def _list_resources(self) -> list[dict[str, Any]]:
        if self._resource_provider is None:
            return []
        try:
            return list(self._resource_provider.list_resources())
        except Exception as exc:  # noqa: BLE001
            _log.warning("acp.list_resources_failed: %s", exc)
            return []


def main() -> None:
    """Entry point for ``python -m xmclaw.acp.server``."""
    server = AcpServer()
    server.run()


if __name__ == "__main__":
    main()
