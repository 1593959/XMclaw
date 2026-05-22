"""MCP HTTP bridge — JSON-RPC 2.0 over SSE / streamableHttp.

Anti-req #14 Phase 2: remote MCP servers (e.g. deployed via Docker,
cloud function, or standalone HTTP service) that don't speak stdio.
Two transport modes:

  * ``sse``          — Server-Sent Events. POST requests carry JSON-RPC
                       payloads; responses stream back via SSE. This is
                       the most widely-deployed HTTP transport in the
                       MCP ecosystem today.
  * ``streamableHttp`` — HTTP POST with direct JSON response (no SSE
                         streaming). Simpler but less common.

Both modes share the same JSON-RPC dispatch logic; only the wire
format differs.

Usage::

    bridge = MCPHttpBridge(
        url="https://mcp.example.com/sse",
        transport="sse",
    )
    await bridge.start()
    agent = AgentLoop(llm=..., bus=..., tools=bridge)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


_JSONRPC_VERSION = "2.0"
_CLIENT_NAME = "xmclaw-v2"
_CLIENT_VERSION = "1.1.0"
_PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """Raised for protocol-level failures during start()."""


@dataclass
class _PendingCall:
    future: asyncio.Future[Any]
    method: str


class MCPHttpBridge(ToolProvider):
    """A ToolProvider backed by a remote MCP server over HTTP.

    Parameters
    ----------
    url : str
        Base URL of the MCP server, e.g. ``"https://mcp.example.com/sse"``.
    transport : str
        ``"sse"`` or ``"streamableHttp"``.
    name : str
        Human label, surfaced in error messages.
    request_timeout : float
        Per-RPC timeout in seconds.
    headers : dict[str, str] | None
        Extra HTTP headers (e.g. auth tokens).
    """

    def __init__(
        self,
        url: str,
        *,
        transport: str = "sse",
        name: str = "mcp-http",
        request_timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._transport = transport
        self._name = name
        self._request_timeout = request_timeout
        self._headers = dict(headers) if headers is not None else {}
        self._env = dict(env) if env is not None else {}
        self._next_id = 1
        self._pending: dict[int, _PendingCall] = {}
        self._tools: list[ToolSpec] = []
        self._started = False
        self._stopped = False
        self._sse_session_id: str | None = None
        self._sse_reader_task: asyncio.Task[None] | None = None
        self._client: Any | None = None  # httpx.AsyncClient

    # ── lifecycle ──

    async def start(self) -> None:
        if self._started:
            raise MCPError(f"bridge {self._name!r} already started")

        try:
            import httpx
        except ImportError as exc:
            raise MCPError(
                f"httpx is required for MCP HTTP transport ({self._name!r})"
            ) from exc

        self._client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/event-stream",
                **self._headers,
            },
            timeout=httpx.Timeout(30.0, connect=10.0, read=60.0, write=30.0),
        )

        # 1. initialize handshake.
        init_resp = await self._rpc("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": _CLIENT_NAME, "version": _CLIENT_VERSION,
            },
        })
        # Capture server protocol version if provided (for future compat).
        server_info = init_resp.get("serverInfo") or {}
        _ = server_info  # logged below if we had a logger

        await self._notify("notifications/initialized", {})

        # 2. tools/list discovery.
        list_resp = await self._rpc("tools/list", {})
        tools_raw = list_resp.get("tools") or []
        self._tools = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            desc = t.get("description", "")
            schema = t.get("inputSchema", {"type": "object"})
            if not isinstance(name, str) or not name:
                continue
            self._tools.append(ToolSpec(
                name=name, description=desc,
                parameters_schema=schema if isinstance(schema, dict)
                else {"type": "object"},
            ))

        self._started = True

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(MCPError(
                    f"MCPHttpBridge {self._name!r} stopped with call in flight"
                ))
        self._pending.clear()
        if self._sse_reader_task is not None:
            self._sse_reader_task.cancel()
            try:
                await self._sse_reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    # ── ToolProvider contract ──

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def invoke(self, call: ToolCall) -> ToolResult:
        import time
        from xmclaw.providers.tool._helpers import _fail_with_hint
        t0 = time.perf_counter()
        if not self._started:
            return _fail_with_hint(
                call, t0,
                f"MCPHttpBridge {self._name!r} not started",
                hint=(
                    "this MCP server's HTTP adapter never started (or "
                    "crashed at boot). Check daemon.log for "
                    "``mcp.start_failed`` lines + the URL configured in "
                    "``mcp_servers.{name}.url``; the agent cannot use "
                    f"any tools from {self._name!r} until it boots."
                ),
            )
        try:
            resp = await self._rpc("tools/call", {
                "name": call.name,
                "arguments": call.args,
            })
        except asyncio.TimeoutError as exc:
            return _fail_with_hint(
                call, t0,
                f"tools/call timed out after {self._request_timeout}s",
                exc=exc,
                hint=(
                    "the MCP HTTP server didn't respond in time. Either the "
                    "tool legitimately takes longer than the configured "
                    f"timeout ({self._request_timeout}s — bump via "
                    "``request_timeout`` in the server config), OR the "
                    "server is unreachable. Check the URL and network."
                ),
            )
        except MCPError as exc:
            return _fail_with_hint(
                call, t0,
                "MCP protocol error",
                exc=exc,
                hint=(
                    "the MCP HTTP server returned a malformed JSON-RPC "
                    "response. Check the server's logs for tracebacks; "
                    "common cause is a version mismatch between client "
                    "and server."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _fail_with_hint(
                call, t0,
                "MCP RPC raised", exc=exc,
                hint=(
                    "unexpected exception talking to the MCP HTTP server. "
                    "If the same call works after a daemon restart, "
                    "the server may have restarted — check daemon.log."
                ),
            )

        is_error = bool(resp.get("isError"))
        content = _content_to_python(resp.get("content"))
        side_effects = tuple(resp.get("_meta", {}).get("side_effects", ()))
        if is_error:
            err_text = content if isinstance(content, str) else str(content)
            return _fail_with_hint(
                call, t0,
                "tool reported error",
                hint=(
                    f"the MCP server's ``{call.name}`` handler returned "
                    f"isError=true with message: {err_text!r}. This is "
                    "the SERVER's structured failure — read the message "
                    "+ adjust args or pick a different tool."
                ),
            )
        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=side_effects,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── JSON-RPC plumbing ──

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = _PendingCall(future=future, method=method)

        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }

        if self._transport == "streamableHttp":
            await self._send_http_post(msg)
        else:
            # SSE mode: start SSE reader on first RPC if not running.
            if self._sse_reader_task is None:
                self._sse_reader_task = asyncio.create_task(self._sse_read_loop())
            await self._send_sse_post(msg)

        try:
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
            "params": params,
        }
        if self._transport == "streamableHttp":
            await self._send_http_post(msg)
        else:
            await self._send_sse_post(msg)

    async def _send_http_post(self, msg: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPError("HTTP client not initialised")
        url = f"{self._url}/message"
        try:
            resp = await self._client.post(
                url,
                json=msg,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"HTTP POST to {url} failed: {exc}") from exc

        # streamableHttp: response is the JSON-RPC response directly.
        if msg.get("id") is not None:
            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise MCPError(f"invalid JSON response: {exc}") from exc
            self._dispatch(body)

    async def _send_sse_post(self, msg: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPError("HTTP client not initialised")
        url = f"{self._url}/message"
        if self._sse_session_id:
            url = f"{url}?sessionId={self._sse_session_id}"
        try:
            resp = await self._client.post(
                url,
                json=msg,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"HTTP POST to {url} failed: {exc}") from exc

    async def _sse_read_loop(self) -> None:
        """Read the SSE stream and dispatch JSON-RPC responses."""
        if self._client is None:
            return
        url = f"{self._url}/sse"
        try:
            import httpx
            async with self._client.stream("GET", url) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        self._handle_sse_block(block)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # SSE stream ended or failed — pending RPCs will time out.
            pass

    def _handle_sse_block(self, block: str) -> None:
        lines = block.strip().splitlines()
        event_type = "message"
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        if event_type == "endpoint":
            # Some SSE implementations send the POST endpoint URL.
            self._sse_session_id = payload.strip()
            return
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict):
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        if req_id is None:
            return
        pending = self._pending.get(req_id)
        if pending is None or pending.future.done():
            return
        if "error" in msg:
            err = msg["error"]
            err_msg = (
                err.get("message", "unknown error")
                if isinstance(err, dict) else str(err)
            )
            pending.future.set_exception(MCPError(
                f"server returned error: {err_msg}"
            ))
            return
        result = msg.get("result")
        if not isinstance(result, dict):
            pending.future.set_exception(MCPError(
                f"malformed response (no dict result): {msg!r}"
            ))
            return
        pending.future.set_result(result)


def _content_to_python(blocks: Any) -> Any:  # noqa: ANN401
    """Convert MCP content blocks to a Python-native value."""
    if blocks is None:
        return ""
    if not isinstance(blocks, list):
        return blocks
    texts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            texts.append(str(b.get("text", "")))
    if texts:
        return "\n".join(texts)
    return blocks
