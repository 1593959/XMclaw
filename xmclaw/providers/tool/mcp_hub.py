"""MCP Hub — multi-server registry over the existing single-server bridge.

Direct port of cline's ``src/services/mcp/McpHub.ts:213-273, 286-549``,
adapted for Python + the existing :class:`MCPBridge` (which already
handles per-connection JSON-RPC stdio). Cline's hub adds:

  1. **Settings JSON file** — ``~/.xmclaw/mcpServers.json`` mirroring
     Claude-Desktop / Claude-Code's ``{ "mcpServers": { name: cfg } }``
     shape so users can drop in configs from those tools verbatim
     (cline's ``schemas.ts:5-93`` Zod union ported as Pydantic-style
     validation here).
  2. **Multi-server orchestration** — a dict of name → MCPBridge,
     started lazily, with per-server enable/disable + auto-approve
     fields propagated.
  3. **64-char tool-name mangling** — MCP names can collide; cline
     uses ``f"{server_uid}__{tool_name}"`` then truncates to 64 chars
     (anthropic limit), keeping a reverse map for dispatch (mirrors
     ``ClineToolSet.ts:198-257``).
  4. **Composite ``ToolProvider``** — the hub IS a ``ToolProvider``,
     so it slots into the existing ``CompositeToolProvider`` as just
     another child alongside ``BuiltinTools`` and ``AgentInterTools``.

Out of scope for v1 (Phase 4.5+ follow-up):
  * SSE / streamableHttp transports (cline's `connectToServer` covers
    them; our :class:`MCPBridge` is stdio-only today)
  * OAuth provider integration (cline's ``McpOAuthManager``)
  * File-watcher hot reload (use ``hub.reload()`` manually for now)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.mcp_bridge import MCPBridge

_log = logging.getLogger(__name__)

# Anthropic tool-name limit — same constraint cline ports here
# (`ClineToolSet.ts:198-257`).
_TOOL_NAME_MAX = 64
_NAME_SEPARATOR = "__"
_VALID_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _sanitize_id(s: str) -> str:
    """MCP server name → identifier-safe slug. Mirrors cline's
    ``mcpToolName.ts:6-18``."""
    out = re.sub(r"[^A-Za-z0-9]", "_", s)
    if not out:
        return "srv"
    if not out[0].isalpha():
        out = "s_" + out
    return out


def _mangle_tool_name(server_id: str, tool_name: str) -> str:
    """``f"{server}__{tool}"`` truncated to 64 chars.

    Mirrors cline ``ClineToolSet.ts:198-257``. When truncation kicks in
    we append a 3-digit hash of the original tool name so two long
    names with a shared prefix don't collide post-truncation.
    """
    sid = _sanitize_id(server_id)
    out = f"{sid}{_NAME_SEPARATOR}{tool_name}"
    if len(out) <= _TOOL_NAME_MAX:
        return out
    # Reserve 4 chars for "_xNNN" hash suffix.
    suffix = f"_x{abs(hash(tool_name)) % 1000:03d}"
    base = f"{sid}{_NAME_SEPARATOR}"
    keep = _TOOL_NAME_MAX - len(base) - len(suffix)
    if keep < 1:
        keep = 1
    truncated_tool = tool_name[:keep]
    return f"{base}{truncated_tool}{suffix}"


# ──────────────────────────────────────────────────────────────────────
# Config schema (Pydantic-like validation, no extra dep)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One server entry from ``mcpServers.json``.

    Mirrors cline ``schemas.ts:5-93`` stdio union (sse / streamableHttp
    are recognised but not yet executed — :meth:`MCPHub.start` skips
    them with a log warning until v1.5 transports land).
    """
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None  # for sse / streamableHttp (Phase 4.5)
    transport: str = "stdio"  # "stdio" | "sse" | "streamableHttp"
    disabled: bool = False
    auto_approve: list[str] = field(default_factory=list)
    timeout_s: float = 30.0


def _parse_server_config(name: str, raw: Any) -> McpServerConfig | None:
    """Best-effort coerce one config entry. Returns None on garbage."""
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    url = raw.get("url")
    transport = raw.get("transport") or ("stdio" if command else "sse" if url else "stdio")
    if transport not in ("stdio", "sse", "streamableHttp"):
        return None
    args_raw = raw.get("args") or []
    args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
    env_raw = raw.get("env") or {}
    env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
    auto_raw = raw.get("autoApprove") or raw.get("auto_approve") or []
    auto = [str(t) for t in auto_raw] if isinstance(auto_raw, list) else []
    timeout_raw = raw.get("timeout") or raw.get("timeout_s") or 30.0
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_s = 30.0
    return McpServerConfig(
        name=str(name),
        command=str(command) if command else None,
        args=args,
        env=env,
        url=str(url) if url else None,
        transport=transport,
        disabled=bool(raw.get("disabled", False)),
        auto_approve=auto,
        timeout_s=timeout_s,
    )


def parse_settings_file(text: str) -> dict[str, McpServerConfig]:
    """Parse ``mcpServers.json``. Tolerates partial garbage."""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    container = raw.get("mcpServers") or raw.get("servers") or raw
    if not isinstance(container, dict):
        return {}
    out: dict[str, McpServerConfig] = {}
    for name, entry in container.items():
        cfg = _parse_server_config(str(name), entry)
        if cfg is not None:
            out[cfg.name] = cfg
    return out


def default_settings_path() -> Path:
    from xmclaw.utils.paths import data_dir
    return data_dir() / "mcpServers.json"


# ──────────────────────────────────────────────────────────────────────
# MCPHub — multi-server orchestrator that IS a ToolProvider.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _ServerState:
    config: McpServerConfig
    bridge: MCPBridge | None = None
    status: str = "disconnected"  # disconnected | connecting | connected | error
    last_error: str | None = None
    # Reverse map: mangled name → original tool name (so we can dispatch).
    tool_name_map: dict[str, str] = field(default_factory=dict)


class MCPHub(ToolProvider):
    """Multi-MCP-server registry. Implements :class:`ToolProvider`.

    Args:
        settings_path: path to ``mcpServers.json``; defaults to
            ``~/.xmclaw/mcpServers.json``. Reread on
            :meth:`reload` (no automatic file watcher in v1).
    """

    def __init__(self, settings_path: Path | None = None) -> None:
        self._settings_path = settings_path or default_settings_path()
        self._servers: dict[str, _ServerState] = {}
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def reload(self) -> dict[str, str]:
        """Re-read settings file, start new servers, stop removed ones.

        Returns a name → status map for observability.
        """
        text = ""
        if self._settings_path.exists():
            try:
                text = self._settings_path.read_text(encoding="utf-8")
            except OSError as exc:
                _log.warning("mcp.settings_read_failed: %s", exc)
                text = ""
        configs = parse_settings_file(text) if text else {}
        return await self._apply_configs(configs)

    async def reload_from_config(
        self, mcp_servers: dict[str, Any] | None,
    ) -> dict[str, str]:
        """B-142: load servers directly from ``daemon/config.json``'s
        ``mcp_servers`` dict instead of (or in addition to) the
        Claude-Desktop ``mcpServers.json`` file.

        Same shape as the file format: each value is a dict with
        ``command`` / ``args`` / ``env`` / ``disabled`` / etc. Lets
        the user manage MCP servers from the same config they edit
        for everything else, without learning about a second file.
        """
        configs: dict[str, McpServerConfig] = {}
        if isinstance(mcp_servers, dict):
            for name, raw in mcp_servers.items():
                cfg = _parse_server_config(str(name), raw)
                if cfg is not None:
                    configs[cfg.name] = cfg
        return await self._apply_configs(configs)

    async def _apply_configs(
        self, configs: dict[str, McpServerConfig],
    ) -> dict[str, str]:
        """Shared diff-and-restart machinery used by both reload paths."""

        async with self._lock:
            existing_names = set(self._servers.keys())
            new_names = set(configs.keys())

            # Stop removed servers.
            for name in existing_names - new_names:
                await self._stop_server(name)

            # Start / restart added or changed servers.
            for name, cfg in configs.items():
                prior = self._servers.get(name)
                if prior is not None:
                    if prior.config == cfg and prior.status == "connected":
                        continue
                    await self._stop_server(name)
                if cfg.disabled:
                    self._servers[name] = _ServerState(
                        config=cfg, status="disabled"
                    )
                    continue
                if cfg.transport != "stdio" or not cfg.command:
                    self._servers[name] = _ServerState(
                        config=cfg,
                        status="error",
                        last_error="non-stdio transports not yet supported",
                    )
                    continue
                state = _ServerState(config=cfg, status="connecting")
                self._servers[name] = state
                try:
                    bridge = MCPBridge(
                        command=[cfg.command, *cfg.args],
                        env={**os.environ, **cfg.env} if cfg.env else None,
                    )
                    await bridge.start()
                    state.bridge = bridge
                    state.status = "connected"
                    # Build the tool_name_map for dispatch lookup.
                    for spec in bridge.list_tools():
                        state.tool_name_map[
                            _mangle_tool_name(name, spec.name)
                        ] = spec.name
                except Exception as exc:  # noqa: BLE001
                    state.status = "error"
                    state.last_error = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "mcp.start_failed name=%s err=%s", name, state.last_error
                    )

        return {n: s.status for n, s in self._servers.items()}

    async def _stop_server(self, name: str) -> None:
        state = self._servers.pop(name, None)
        if state and state.bridge is not None:
            try:
                await state.bridge.stop()
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        """Stop every connected bridge. Idempotent."""
        async with self._lock:
            for name in list(self._servers.keys()):
                await self._stop_server(name)

    def status(self) -> dict[str, dict[str, Any]]:
        """Snapshot of (name → {status, transport, tools_count, error?})."""
        out: dict[str, dict[str, Any]] = {}
        for name, st in self._servers.items():
            out[name] = {
                "status": st.status,
                "transport": st.config.transport,
                "tools_count": len(st.tool_name_map),
                "last_error": st.last_error,
                "auto_approve": list(st.config.auto_approve),
                "disabled": st.config.disabled,
            }
        return out

    # ── ToolProvider impl ─────────────────────────────────────────────

    def list_tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for name, st in self._servers.items():
            if st.bridge is None or st.status != "connected":
                continue
            for spec in st.bridge.list_tools():
                mangled = _mangle_tool_name(name, spec.name)
                out.append(
                    ToolSpec(
                        name=mangled,
                        description=f"[mcp:{name}] {spec.description}",
                        parameters_schema=spec.parameters_schema,
                    )
                )
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        # Look up which server owns this mangled name.
        for srv_name, st in self._servers.items():
            if call.name in st.tool_name_map and st.bridge is not None:
                original = st.tool_name_map[call.name]
                inner = ToolCall(
                    id=call.id,
                    name=original,
                    args=call.args,
                    provenance=f"mcp:{srv_name}",
                    session_id=call.session_id,
                )
                return await st.bridge.invoke(inner)
        return ToolResult(
            call_id=call.id,
            ok=False,
            content=None,
            error=f"unknown MCP tool: {call.name}",
        )
