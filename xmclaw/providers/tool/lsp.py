"""Language Server Protocol bridge — ``lsp_hover`` + ``lsp_definition``.

XMclaw inherits the MCP-over-stdio pattern from xmclaw/providers/tool/
mcp_bridge.py; here we speak LSP (also JSON-RPC but with different
framing: ``Content-Length:`` headers instead of newline-delimited).
The small amount of boilerplate is duplicated on purpose rather than
shared with mcp_bridge -- the two protocols evolve independently and
coupling them makes every LSP quirk leak into MCP.

Scope this phase:
  - Python only, via ``python-lsp-server`` (pylsp). Other languages
    plug in later as separate extras.
  - Two tools:
      lsp_hover(path, line, column)      -> symbol info (docstring, type)
      lsp_definition(path, line, column) -> destination (path, line, col)
  - The server is booted lazily on first use and kept alive for the
    life of the LSPTools instance. Shutdown via ``await shutdown()``.

Design trade-offs I took deliberately:
  - **No ``textDocument/didOpen`` tracking.** Each hover/definition
    call synchronously opens -> queries -> closes the document. Slower
    per-call (a few extra MB passed over stdio) but eliminates the
    "LSP stale buffer" class of bugs where the model's view diverges
    from disk. Files change underneath the agent all the time in our
    workflow; didOpen would need a watcher we don't have yet.
  - **Zero-indexed positions.** LSP is zero-indexed by spec; the tool
    args use 0-indexed line/column too. Agents get confused if some
    tools use 1-indexed and others 0-indexed -- pick one, enforce it
    in the tool description.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


_LSP_HOVER_SPEC = ToolSpec(
    name="lsp_hover",
    description=(
        "Look up a symbol's signature / docstring / inferred type at a "
        "given point in a Python file. Uses python-lsp-server. "
        "Positions are 0-indexed (line 0 = first line, column 0 = first char)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path":   {"type": "string", "description": "Absolute path to a .py file."},
            "line":   {"type": "integer", "description": "0-indexed line number."},
            "column": {"type": "integer", "description": "0-indexed character column."},
        },
        "required": ["path", "line", "column"],
    },
)

_LSP_DEFINITION_SPEC = ToolSpec(
    name="lsp_definition",
    description=(
        "Jump to the definition of the symbol at a given point. Returns "
        "the target path + 0-indexed line/column. Useful for navigating "
        "an unfamiliar codebase without reading every file."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path":   {"type": "string"},
            "line":   {"type": "integer"},
            "column": {"type": "integer"},
        },
        "required": ["path", "line", "column"],
    },
)


class _LSPUnavailable(RuntimeError):
    pass


class LSPTools(ToolProvider):
    """Python LSP (pylsp) bridge.

    Parameters
    ----------
    root
        Workspace root URI. Defaults to the current working directory.
    startup_timeout_s
        How long to wait for ``initialize`` to return before giving up.
    """

    def __init__(
        self,
        root: str | Path = ".",
        *,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._root = Path(root).resolve()
        self._startup_timeout_s = startup_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._boot_lock = asyncio.Lock()

    def list_tools(self) -> list[ToolSpec]:
        return [_LSP_HOVER_SPEC, _LSP_DEFINITION_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "lsp_hover":
                return await self._hover(call, t0)
            if call.name == "lsp_definition":
                return await self._definition(call, t0)
            return _fail(call, t0, f"unknown tool: {call.name!r}")
        except _LSPUnavailable as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

    async def shutdown(self) -> None:
        if self._proc is not None:
            try:
                await self._send_rpc("shutdown", {}, expect_response=True,
                                     timeout_s=2.0)
                await self._send_rpc("exit", None, expect_response=False)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                try: self._proc.kill()
                except Exception: pass  # noqa: BLE001,S110
            self._proc = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

    # ── internals ─────────────────────────────────────────────────

    async def _ensure_server(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._boot_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            try:
                import pylsp  # noqa: F401 -- availability check only
            except ImportError as exc:
                raise _LSPUnavailable(
                    "python-lsp-server not installed -- run "
                    "`pip install xmclaw[lsp]`"
                ) from exc
            # Launch pylsp via the installed entry point. Cross-platform
            # because pylsp installs a `pylsp` script on PATH.
            self._proc = await asyncio.create_subprocess_exec(
                "pylsp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            # LSP handshake: initialize + initialized notification.
            root_uri = self._root.as_uri()
            init_result = await asyncio.wait_for(
                self._send_rpc("initialize", {
                    "processId": os.getpid(),
                    "rootUri": root_uri,
                    "capabilities": {},
                }, expect_response=True),
                timeout=self._startup_timeout_s,
            )
            # We don't inspect init_result capabilities -- pylsp supports
            # hover/definition by default. If that changes, fail loud.
            _ = init_result
            await self._send_rpc("initialized", {}, expect_response=False)

    async def _read_loop(self) -> None:
        """Read Content-Length-framed JSON-RPC messages from pylsp stdout."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            # Header: Content-Length: N\r\n\r\n
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await stdout.read(1)
                if not chunk:
                    return
                header += chunk
            # Find content-length.
            length = 0
            for line in header.decode("ascii", "ignore").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            if length <= 0:
                continue
            body = await stdout.readexactly(length)
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    fut.set_exception(RuntimeError(
                        f"LSP error: {msg['error'].get('message', msg['error'])}"
                    ))
                else:
                    fut.set_result(msg.get("result"))
            # Notifications (msg_id None) are ignored for now.

    async def _send_rpc(
        self, method: str, params: Any,
        *, expect_response: bool = True,
        timeout_s: float = 5.0,
    ) -> Any:
        assert self._proc is not None and self._proc.stdin is not None
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        fut: asyncio.Future[Any] | None = None
        if expect_response:
            msg_id = self._next_id
            self._next_id += 1
            req["id"] = msg_id
            fut = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = fut
        body = json.dumps(req).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        self._proc.stdin.write(frame)
        await self._proc.stdin.drain()
        if fut is not None:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        return None

    async def _query(self, method: str, call: ToolCall, t0: float) -> ToolResult:
        path = call.args.get("path")
        line = call.args.get("line")
        col = call.args.get("column")
        if not isinstance(path, str) or not path:
            return _fail(call, t0, "missing or empty 'path'")
        if not isinstance(line, int) or not isinstance(col, int):
            return _fail(call, t0, "'line' and 'column' must be integers")
        p = Path(path)
        if not p.exists():
            return _fail(call, t0, f"file does not exist: {path}")
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"unreadable: {exc}")

        await self._ensure_server()
        uri = p.as_uri()
        # didOpen so pylsp knows about this file for the request.
        await self._send_rpc("textDocument/didOpen", {
            "textDocument": {
                "uri": uri, "languageId": "python",
                "version": 1, "text": text,
            },
        }, expect_response=False)
        try:
            result = await self._send_rpc(method, {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": col},
            }, expect_response=True)
        finally:
            # didClose so pylsp doesn't accumulate open buffers.
            await self._send_rpc("textDocument/didClose", {
                "textDocument": {"uri": uri},
            }, expect_response=False)
        return ToolResult(
            call_id=call.id, ok=True,
            content=result if result is not None else {},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _hover(self, call: ToolCall, t0: float) -> ToolResult:
        return await self._query("textDocument/hover", call, t0)

    async def _definition(self, call: ToolCall, t0: float) -> ToolResult:
        return await self._query("textDocument/definition", call, t0)


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
