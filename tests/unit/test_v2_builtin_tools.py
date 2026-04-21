"""BuiltinTools (file_read / file_write) — unit tests.

Covers: happy paths, structured-error paths, side_effects population,
allowlist enforcement. Every failure mode must return a structured
ToolResult with ``ok=False`` and an ``error`` message — the orchestrator
inspects those, never swallowed exceptions.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


# ── list_tools ────────────────────────────────────────────────────────────

def test_list_tools_has_read_and_write() -> None:
    tools = BuiltinTools().list_tools()
    names = {t.name for t in tools}
    assert names == {"file_read", "file_write"}


def test_list_tools_schemas_well_formed() -> None:
    for spec in BuiltinTools().list_tools():
        assert spec.parameters_schema["type"] == "object"
        assert "path" in spec.parameters_schema["required"]


# ── file_read happy path ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_read_returns_content_and_empty_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.txt"
        p.write_text("hello world", encoding="utf-8")
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_read", {"path": str(p)}))
        assert result.ok is True
        assert result.content == "hello world"
        assert result.side_effects == ()  # pure read — nothing to verify
        assert result.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_file_read_missing_file_returns_structured_error() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {"path": "/no/such/file.abc"}))
    assert result.ok is False
    assert "not found" in result.error.lower() or "no such" in result.error.lower()


@pytest.mark.asyncio
async def test_file_read_missing_path_arg() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {}))
    assert result.ok is False
    assert "path" in result.error


# ── file_write happy path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_write_creates_file_and_reports_side_effect() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "nested" / "out.txt"
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(p), "content": "hello",
        }))
        assert result.ok is True
        assert p.exists()
        assert p.read_text(encoding="utf-8") == "hello"
        # side_effects carries the resolved path for the grader.
        assert len(result.side_effects) == 1
        assert str(p.resolve()) in result.side_effects[0]
        assert result.content["bytes"] == 5


@pytest.mark.asyncio
async def test_file_write_overwrites_existing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "f.txt"
        p.write_text("old", encoding="utf-8")
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(p), "content": "new",
        }))
        assert result.ok is True
        assert p.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_file_write_rejects_non_string_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(Path(tmp) / "x.txt"),
            "content": 12345,  # not a string
        }))
        assert result.ok is False
        assert "content" in result.error


# ── allowlist ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allowlist_blocks_write_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "_outside_xmc_test.txt"
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    try:
        result = await tools.invoke(_call("file_write", {
            "path": str(outside), "content": "should not land",
        }))
        assert result.ok is False
        assert "permission" in result.error.lower()
        assert not outside.exists()
    finally:
        # Defensive cleanup in case the test framework's tmp_path.parent is
        # shared with other runs.
        if outside.exists():
            outside.unlink()


@pytest.mark.asyncio
async def test_allowlist_blocks_read_outside(tmp_path: Path) -> None:
    # Create a file in tmp_path/ but allowlist only a sibling dir.
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    other = tmp_path.parent / "_other"
    other.mkdir(exist_ok=True)
    try:
        tools = BuiltinTools(allowed_dirs=[other])
        result = await tools.invoke(_call("file_read", {"path": str(real)}))
        assert result.ok is False
        assert "permission" in result.error.lower()
    finally:
        if other.exists():
            other.rmdir()


@pytest.mark.asyncio
async def test_no_allowlist_means_trust_caller(tmp_path: Path) -> None:
    p = tmp_path / "ok.txt"
    p.write_text("yo", encoding="utf-8")
    tools = BuiltinTools()  # None allowlist
    result = await tools.invoke(_call("file_read", {"path": str(p)}))
    assert result.ok is True


# ── unknown tool ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("definitely_not_a_tool", {}))
    assert result.ok is False
    assert "unknown tool" in result.error
