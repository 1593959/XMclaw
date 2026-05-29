"""Memory v3 phase 4.2 — ``memory_get`` reads persona MD files.

Pins:
  - file-arg required + case-insensitive basename resolution
  - section filter extracts ``## Section`` segment only
  - lines filter applies AFTER section
  - fids_present collected from ``<!-- fid:xxx -->`` markers
  - missing-but-expected file returns empty content (not error)
  - persona dir not configured → clean error
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(args: dict) -> ToolCall:
    return ToolCall(name="memory_get", args=args, provenance="synthetic")


@pytest.fixture
def tools_with_persona(tmp_path: Path):
    """BuiltinTools whose persona_dir_provider points at a real
    tmp dir so memory_get can actually read files."""
    pdir = tmp_path / "persona"
    pdir.mkdir()
    tools = BuiltinTools(persona_dir_provider=lambda: pdir)
    return tools, pdir


# ─── basic guards ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_requires_file_arg():
    tools = BuiltinTools(persona_dir_provider=lambda: Path("/tmp"))
    r = await tools.invoke(_call({}))
    assert r.ok is False
    assert "file" in r.error


@pytest.mark.asyncio
async def test_memory_get_no_persona_dir_configured():
    tools = BuiltinTools()  # no persona_dir_provider
    r = await tools.invoke(_call({"file": "MEMORY.md"}))
    assert r.ok is False
    assert "persona profile dir" in r.error


# ─── happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_returns_whole_file(tools_with_persona):
    tools, pdir = tools_with_persona
    (pdir / "USER.md").write_text(
        "# USER\n\nhello world\n", encoding="utf-8",
    )
    r = await tools.invoke(_call({"file": "USER.md"}))
    assert r.ok is True
    assert r.content["file"] == "USER.md"
    assert "hello world" in r.content["content"]


@pytest.mark.asyncio
async def test_memory_get_case_insensitive_basename(tools_with_persona):
    tools, pdir = tools_with_persona
    (pdir / "MEMORY.md").write_text("body", encoding="utf-8")
    r = await tools.invoke(_call({"file": "memory.md"}))
    assert r.ok is True
    # Canonical name on disk is returned, not the user's casing.
    assert r.content["file"] == "MEMORY.md"


# ─── section filter ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_extracts_named_section(tools_with_persona):
    tools, pdir = tools_with_persona
    (pdir / "USER.md").write_text(
        "## Auto-identity\n- 用户叫敬宇\n\n"
        "## Auto-extracted preferences\n- 用户偏好简洁\n- 用户用 Edge\n\n"
        "## Other\n- something else\n",
        encoding="utf-8",
    )
    r = await tools.invoke(_call({
        "file": "USER.md",
        "section": "Auto-extracted preferences",
    }))
    assert r.ok is True
    assert "用户偏好简洁" in r.content["content"]
    assert "用户用 Edge" in r.content["content"]
    # Other sections excluded.
    assert "用户叫敬宇" not in r.content["content"]
    assert "something else" not in r.content["content"]


@pytest.mark.asyncio
async def test_memory_get_section_with_or_without_hash_prefix(
    tools_with_persona,
):
    tools, pdir = tools_with_persona
    (pdir / "AGENTS.md").write_text(
        "## Workflows\n- step 1\n",
        encoding="utf-8",
    )
    r1 = await tools.invoke(_call({
        "file": "AGENTS.md", "section": "Workflows",
    }))
    r2 = await tools.invoke(_call({
        "file": "AGENTS.md", "section": "## Workflows",
    }))
    assert r1.content["content"] == r2.content["content"]


@pytest.mark.asyncio
async def test_memory_get_missing_section_reports_clearly(
    tools_with_persona,
):
    tools, pdir = tools_with_persona
    (pdir / "USER.md").write_text("## Real\n- a\n", encoding="utf-8")
    r = await tools.invoke(_call({
        "file": "USER.md", "section": "Imaginary",
    }))
    assert r.ok is True
    assert "not found" in r.content["content"]


# ─── lines filter ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_line_range(tools_with_persona):
    tools, pdir = tools_with_persona
    body = "\n".join(f"line {i}" for i in range(1, 11))
    (pdir / "TOOLS.md").write_text(body, encoding="utf-8")
    r = await tools.invoke(_call({
        "file": "TOOLS.md", "lines": "3-5",
    }))
    assert r.ok is True
    assert r.content["content"] == "line 3\nline 4\nline 5"


@pytest.mark.asyncio
async def test_memory_get_invalid_lines_format(tools_with_persona):
    tools, pdir = tools_with_persona
    (pdir / "TOOLS.md").write_text("x", encoding="utf-8")
    r = await tools.invoke(_call({
        "file": "TOOLS.md", "lines": "garbage",
    }))
    assert r.ok is False
    assert "lines" in r.error.lower()


# ─── fid extraction ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_collects_fids_from_content(tools_with_persona):
    tools, pdir = tools_with_persona
    (pdir / "MEMORY.md").write_text(
        "- fact A <!-- fid:abc123 -->\n"
        "- fact B <!-- fid:def456 -->\n"
        "- fact C with no marker\n"
        "- fact A again <!-- fid:abc123 -->\n",
        encoding="utf-8",
    )
    r = await tools.invoke(_call({"file": "MEMORY.md"}))
    assert r.ok is True
    # Deduped + in-order.
    assert r.content["fids_present"] == ["abc123", "def456"]


# ─── expected-but-missing file ────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_known_file_not_yet_on_disk(tools_with_persona):
    """Persona dir exists but a known file (e.g. SOUL.md) hasn't
    been rendered yet — no facts in its buckets. Returning empty
    content with a helpful ``note`` is friendlier than an error."""
    tools, _ = tools_with_persona
    r = await tools.invoke(_call({"file": "SOUL.md"}))
    assert r.ok is True
    assert r.content["content"] == ""
    assert "note" in r.content


@pytest.mark.asyncio
async def test_memory_get_unknown_file_errors(tools_with_persona):
    tools, _ = tools_with_persona
    r = await tools.invoke(_call({"file": "RANDOM_NAME.md"}))
    assert r.ok is False
    assert "not found" in r.error.lower()
