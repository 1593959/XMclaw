"""Unit tests for Phase 4 MCP hub — settings parser + name mangling + status."""
from __future__ import annotations

import json

import pytest

from xmclaw.providers.tool.mcp_hub import (
    MCPHub,
    McpServerConfig,
    _mangle_tool_name,
    _sanitize_id,
    parse_settings_file,
)


# ── Settings parser ──────────────────────────────────────────────────


def test_parse_empty_returns_empty():
    assert parse_settings_file("") == {}
    assert parse_settings_file("not json") == {}


def test_parse_claude_desktop_shape():
    text = json.dumps({
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "autoApprove": ["read_file"],
            },
        },
    })
    out = parse_settings_file(text)
    assert "filesystem" in out
    fs = out["filesystem"]
    assert isinstance(fs, McpServerConfig)
    assert fs.command == "npx"
    assert fs.transport == "stdio"
    assert fs.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert fs.auto_approve == ["read_file"]
    assert fs.disabled is False
    assert fs.timeout_s == 30.0


def test_parse_handles_disabled_flag():
    text = json.dumps({
        "mcpServers": {"x": {"command": "echo", "disabled": True}},
    })
    out = parse_settings_file(text)
    assert out["x"].disabled is True


def test_parse_handles_explicit_timeout():
    text = json.dumps({
        "mcpServers": {"x": {"command": "echo", "timeout": 90}},
    })
    out = parse_settings_file(text)
    assert out["x"].timeout_s == 90.0


def test_parse_skips_garbage_entries():
    text = json.dumps({
        "mcpServers": {
            "ok":   {"command": "echo"},
            "bad":  "not a dict",
            "weird": {"transport": "carrier-pigeon"},  # unknown transport
        },
    })
    out = parse_settings_file(text)
    assert "ok" in out
    assert "bad" not in out
    assert "weird" not in out


def test_parse_old_servers_key_alias():
    text = json.dumps({"servers": {"x": {"command": "echo"}}})
    out = parse_settings_file(text)
    assert "x" in out


# ── Name mangling ────────────────────────────────────────────────────


def test_sanitize_id_replaces_specials():
    assert _sanitize_id("my-server.v2") == "my_server_v2"
    assert _sanitize_id("123start").startswith("s_")
    assert _sanitize_id("") == "srv"


def test_mangle_tool_name_round_trip_short():
    out = _mangle_tool_name("filesystem", "read_file")
    assert out == "filesystem__read_file"
    assert len(out) <= 64


def test_mangle_tool_name_truncates_for_anthropic_64char_limit():
    long_server = "very_long_server_name_that_is_already_pushing_things"
    long_tool = "an_unreasonably_long_tool_name_that_overflows_limits"
    out = _mangle_tool_name(long_server, long_tool)
    assert len(out) <= 64
    # Server prefix is preserved + separator + truncated tail.
    assert out.startswith(_sanitize_id(long_server) + "__")


def test_mangle_tool_name_collision_resistant():
    # Truncation injects a hash suffix to keep two long names distinct.
    a = _mangle_tool_name("server", "the_same_prefix_for_a_thousand_chars" + "_aaa")
    b = _mangle_tool_name("server", "the_same_prefix_for_a_thousand_chars" + "_bbb")
    assert a != b


# ── MCPHub — empty / no-config behaviour ─────────────────────────────


@pytest.mark.asyncio
async def test_hub_empty_settings_yields_no_tools(tmp_path):
    settings = tmp_path / "mcpServers.json"
    hub = MCPHub(settings_path=settings)
    statuses = await hub.reload()
    assert statuses == {}
    assert hub.list_tools() == []
    assert hub.status() == {}


@pytest.mark.asyncio
async def test_hub_marks_disabled_servers(tmp_path):
    settings = tmp_path / "mcpServers.json"
    settings.write_text(
        json.dumps({"mcpServers": {"x": {"command": "echo", "disabled": True}}}),
        encoding="utf-8",
    )
    hub = MCPHub(settings_path=settings)
    statuses = await hub.reload()
    assert statuses == {"x": "disabled"}
    assert hub.list_tools() == []


@pytest.mark.asyncio
async def test_hub_rejects_non_stdio_transport(tmp_path):
    settings = tmp_path / "mcpServers.json"
    settings.write_text(
        json.dumps({
            "mcpServers": {
                "remote": {"url": "https://example.com/sse", "transport": "sse"},
            }
        }),
        encoding="utf-8",
    )
    hub = MCPHub(settings_path=settings)
    statuses = await hub.reload()
    assert statuses["remote"] == "error"
    snapshot = hub.status()["remote"]
    assert "non-stdio" in (snapshot["last_error"] or "")


# ── B-142: reload_from_config (no settings file needed) ────────────


@pytest.mark.asyncio
async def test_reload_from_config_dict(tmp_path):
    """B-142 — daemon config.mcp_servers can drive the hub directly,
    without users having to maintain a separate ~/.xmclaw/mcpServers.json."""
    hub = MCPHub(settings_path=tmp_path / "ignored.json")
    cfg = {
        "fs-disabled": {"command": "echo", "disabled": True},
    }
    statuses = await hub.reload_from_config(cfg)
    assert statuses == {"fs-disabled": "disabled"}
    assert hub.list_tools() == []


@pytest.mark.asyncio
async def test_reload_from_config_none_or_invalid(tmp_path):
    """None / non-dict input is a no-op (treated as 'no MCP servers
    configured') — matches the path where the user hasn't filled in
    mcp_servers in their daemon config yet."""
    hub = MCPHub(settings_path=tmp_path / "x.json")
    assert await hub.reload_from_config(None) == {}
    assert await hub.reload_from_config("not-a-dict") == {}  # type: ignore[arg-type]
    assert await hub.reload_from_config({}) == {}


@pytest.mark.asyncio
async def test_reload_from_config_rejects_non_stdio(tmp_path):
    hub = MCPHub(settings_path=tmp_path / "x.json")
    statuses = await hub.reload_from_config({
        "remote": {"url": "https://example.com/sse", "transport": "sse"},
    })
    assert statuses["remote"] == "error"
