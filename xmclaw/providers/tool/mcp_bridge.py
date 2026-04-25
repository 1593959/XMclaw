"""MCP bridge — expose an external Model Context Protocol server as a ToolProvider.

Anti-req #14: MCP as first-class protocol. Concrete implementation,
not a stub. Users can wire any stdio-based MCP server (official
@modelcontextprotocol/server-filesystem, github, memory, etc.) as a
tool source for v2's AgentLoop:

    bridge = MCPBridge(command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"])
    await bridge.start()
    agent = AgentLoop(llm=..., bus=..., tools=bridge)

Wire protocol: JSON-RPC 2.0 over subprocess stdin/stdout (one JSON
object per line). The three methods we speak:

  initialize       — handshake, caller → server
  tools/list       — discovery,  caller → server
  tools/call       — invocation, caller → server

Everything else (resources, prompts, sampling, notifications) is
deliberately out of scope for Phase 4.9. An MCP server that only
implements tools is the minimum useful adapter.

Isolation: the subprocess runs as a child of the v2 daemon. It
doesn't share state with the daemon's Python heap; if the server
crashes, the bridge surfaces structured ToolResult(ok=False, ...)
rather than propagating. Kill is clean via subprocess.terminate.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


_JSONRPC_VERSION = "2.0"
_CLIENT_NAME = "xmclaw-v2"
_CLIENT_VERSION = "2.0.0.dev0"
# Protocol version we claim to speak. Real MCP has versioned protocols;
# "2024-11-05" is the most widely-deployed stable version at time of
# writing. Servers that require a newer version will reject the
# initialize — we surface that as a clear RuntimeError on start().
_PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """Raised for protocol-level failures during start()."""


@dataclass
class _PendingCall:
    future: asyncio.Future
    method: str


class MCPBridge(ToolProvider):
    """A ToolProvider backed by a subprocess speaking JSON-RPC 2.0 MCP.

    Parameters
    ----------
    command : sequence[str]
        argv for the MCP server process, e.g.::

            ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]

    env : mapping[str, str] | None
        Optional environment overrides for the child. Parent environment
        is inherited; entries here override. Use for API keys etc.
    name : str
        Human label, surfaced in error messages. Default "mcp".
    request_timeout : float
        Per-RPC timeout in seconds. tools/call requests that don't
        respond in this window surface as ToolResult(ok=False, ...).
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        name: str = "mcp",
        request_timeout: float = 30.0,
    ) -> None:
        self._command = list(command)
        self._env = dict(env) if env is not None else None
        self._name = name
        self._request_timeout = request_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, _PendingCall] = {}
        self._tools: list[ToolSpec] = []
        self._started = False
        self._stopped = False

    # ── lifecycle ──

    async def start(self) -> None:
        """Spawn the server, complete the handshake, fetch tools/list.

        Raises ``MCPError`` if:
          - the subprocess can't spawn
          - initialize fails
          - tools/list fails
          - the server emits malformed responses
        """
        if self._started:
            raise MCPError(f"bridge {self._name!r} already started")

        full_env = os.environ.copy()
        if self._env is not None:
            full_env.update(self._env)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
        except FileNotFoundError as exc:
            raise MCPError(
                f"failed to spawn MCP server {self._name!r}: "
                f"command not found: {self._command[0]!r} ({exc})"
            ) from exc

        self._reader_task = asyncio.create_task(self._read_loop())

        # 1. initialize handshake. Response body is unused — we only care
        # that the RPC succeeded; capabilities the server advertises are
        # ignored at this stage.
        try:
            await self._rpc("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": _CLIENT_NAME, "version": _CLIENT_VERSION,
                },
            })
        except Exception as exc:  # noqa: BLE001
            await self._shutdown_process()
            raise MCPError(
                f"initialize RPC failed for {self._name!r}: {exc}"
            ) from exc

        # Per MCP, client must send initialized notification after
        # receiving the initialize response.
        await self._notify("notifications/initialized", {})

        # 2. tools/list discovery.
        try:
            list_resp = await self._rpc("tools/list", {})
        except Exception as exc:  # noqa: BLE001
            await self._shutdown_process()
            raise MCPError(
                f"tools/list failed for {self._name!r}: {exc}"
            ) from exc

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
        """Terminate the subprocess cleanly. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        # Cancel any pending RPCs so callers aren't left hanging.
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(MCPError(
                    f"MCPBridge {self._name!r} stopped with call in flight"
                ))
        self._pending.clear()
        await self._shutdown_process()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── ToolProvider contract ──

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def invoke(self, call: ToolCall) -> ToolResult:
        if not self._started:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"MCPBridge {self._name!r} not started",
            )
        try:
            resp = await self._rpc("tools/call", {
                "name": call.name,
                "arguments": call.args,
            })
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"tools/call timed out after {self._request_timeout}s",
            )
        except MCPError as exc:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"MCP protocol error: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        # MCP tools/call response: either content blocks or an error.
        # Server-level errors come back as {"isError": true, "content":
        # [{"type": "text", "text": "..."}]}.
        is_error = bool(resp.get("isError"))
        content = _content_to_python(resp.get("content"))
        side_effects = tuple(resp.get("_meta", {}).get("side_effects", ()))
        if is_error:
            err_text = content if isinstance(content, str) else str(content)
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"tool reported error: {err_text}",
            )
        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=side_effects,
        )

    # ── JSON-RPC plumbing ──

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one request, await the matching response, return its result."""
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = _PendingCall(future=future, method=method)

        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send(msg)
        try:
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self._send({
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
            "params": params,
        })

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("subprocess not running")
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MCPError(f"pipe to subprocess closed: {exc}") from exc

    async def _read_loop(self) -> None:
        """Drain the subprocess's stdout line-by-line, resolving
        pending RPCs as matching responses arrive. Loops until the
        process exits or is cancelled."""
        if self._proc is None or self._proc.stdout is None:
            return
        while True:
            try:
                raw = await self._proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                return
            if not raw:  # EOF — subprocess exited
                return
            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                # Malformed line — ignore and keep reading. Real MCP
                # servers shouldn't emit these; if one does, we log
                # nothing (keeps the bridge resilient) but the pending
                # RPC will time out cleanly.
                continue
            if not isinstance(msg, dict):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message. We only correlate
        responses; inbound requests/notifications from the server are
        currently ignored (Phase 4.9 is client-role only)."""
        req_id = msg.get("id")
        if req_id is None:
            # Notification or malformed — ignore.
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

    async def _shutdown_process(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            await self._proc.wait()


def _content_to_python(blocks: Any) -> Any:  # noqa: ANN401
    """Convert MCP content blocks to a Python-native value.

    MCP returns content as a list of blocks, each with a ``type``
    field. For Phase 4.9 we handle the common case: a single text
    block is returned as its ``text``; multiple text blocks are
    joined with newlines; anything non-text is returned verbatim
    (serialized repr).
    """
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
    return blocks  # return the raw list for unknown shapes
