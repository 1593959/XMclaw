"""Tests for the 2026-05-26 ``update_persona`` ``remove_bullet`` mode.

Pre-fix the agent's only way to drop a single wrong bullet from a
persona file was ``mode=replace`` — overwriting the whole file with
hand-curated content, losing every other line in the same write.
The new ``remove_bullet`` mode does surgical deletion by case-
sensitive substring match.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="anthropic", id="call-1",
    )


def _make_tools(persona_dir: Path) -> BuiltinTools:
    persona_dir.mkdir(parents=True, exist_ok=True)
    return BuiltinTools(
        persona_dir_provider=lambda: persona_dir,
        allowed_dirs=[str(persona_dir)],
    )


@pytest.mark.asyncio
async def test_remove_bullet_drops_only_matching_line(tmp_path: Path) -> None:
    pdir = tmp_path / "persona"
    user_md = pdir / "USER.md"
    pdir.mkdir(parents=True, exist_ok=True)
    user_md.write_text(
        "# USER.md\n\n"
        "## Notes\n\n"
        "- 2026-05-25: user is 张伟\n"
        "- 2026-05-25: user prefers Chinese\n"
        "- 2026-05-25: user uses 🐾 emoji\n",
        encoding="utf-8",
    )
    tools = _make_tools(pdir)
    result = await tools.invoke(_call(
        "update_persona",
        {"file": "USER.md", "mode": "remove_bullet", "match": "张伟"},
    ))
    assert result.ok is True, result.error
    body = user_md.read_text(encoding="utf-8")
    assert "张伟" not in body
    # Sibling bullets survive intact.
    assert "user prefers Chinese" in body
    assert "user uses 🐾 emoji" in body


@pytest.mark.asyncio
async def test_remove_bullet_drops_all_matches(tmp_path: Path) -> None:
    pdir = tmp_path / "persona"
    user_md = pdir / "USER.md"
    pdir.mkdir(parents=True, exist_ok=True)
    user_md.write_text(
        "## Notes\n\n"
        "- 2026-05-25: 视觉受限 hint A\n"
        "- 2026-05-25: 视觉受限 hint B\n"
        "- 2026-05-25: unrelated bullet\n",
        encoding="utf-8",
    )
    tools = _make_tools(pdir)
    result = await tools.invoke(_call(
        "update_persona",
        {"file": "USER.md", "mode": "remove_bullet", "match": "视觉受限"},
    ))
    assert result.ok is True
    assert "dropped 2" in result.content["summary"]
    body = user_md.read_text(encoding="utf-8")
    assert "视觉受限" not in body
    assert "unrelated bullet" in body


@pytest.mark.asyncio
async def test_remove_bullet_skips_non_bullet_lines(tmp_path: Path) -> None:
    """Headings, prose, and non-bullet lines that contain the match
    must NOT be removed — only list bullets (``- `` / ``* `` prefix).
    Otherwise the agent could accidentally delete section titles."""
    pdir = tmp_path / "persona"
    md = pdir / "USER.md"
    pdir.mkdir(parents=True, exist_ok=True)
    md.write_text(
        "# 张伟's profile\n"  # heading containing "张伟" — keep
        "Some prose mentioning 张伟 in passing.\n"  # prose — keep
        "- 2026-05-25: user is 张伟\n",  # bullet — drop
        encoding="utf-8",
    )
    tools = _make_tools(pdir)
    result = await tools.invoke(_call(
        "update_persona",
        {"file": "USER.md", "mode": "remove_bullet", "match": "张伟"},
    ))
    assert result.ok is True
    body = md.read_text(encoding="utf-8")
    assert "# 张伟's profile" in body
    assert "prose mentioning 张伟" in body
    assert "- 2026-05-25: user is 张伟" not in body


@pytest.mark.asyncio
async def test_remove_bullet_no_match_returns_error(tmp_path: Path) -> None:
    """Helpful error when the match doesn't find anything — points
    the agent at memory_forget for auto-extracted facts (which live
    in LanceDB, not the manual section)."""
    pdir = tmp_path / "persona"
    md = pdir / "USER.md"
    pdir.mkdir(parents=True, exist_ok=True)
    md.write_text("- something else\n", encoding="utf-8")
    tools = _make_tools(pdir)
    result = await tools.invoke(_call(
        "update_persona",
        {"file": "USER.md", "mode": "remove_bullet", "match": "no-match"},
    ))
    assert result.ok is False
    assert "no bullet" in result.error
    assert "memory_forget" in result.error


@pytest.mark.asyncio
async def test_remove_bullet_requires_match_arg(tmp_path: Path) -> None:
    pdir = tmp_path / "persona"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "USER.md").write_text("- bullet\n", encoding="utf-8")
    tools = _make_tools(pdir)
    result = await tools.invoke(_call(
        "update_persona",
        {"file": "USER.md", "mode": "remove_bullet"},
    ))
    assert result.ok is False
    assert "'match' required" in result.error
