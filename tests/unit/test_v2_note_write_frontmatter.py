"""B-93: note_write writes YAML frontmatter when description / tags
are passed (the LLM-picker indexer reads these to choose relevant
notes during turn injection).

Pins:
  * description-only → ``description:`` field, no tags line
  * tags-only → ``tags: [...]`` line, no description line
  * both → both lines
  * neither → no frontmatter (unchanged shape from B-45)
  * append mode: frontmatter NOT injected (preserves any existing one)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(args: dict) -> ToolCall:
    return ToolCall(
        id="c1", provenance="synthetic", name="note_write", args=args,
    )


@pytest.mark.asyncio
async def test_description_only_emits_frontmatter(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "xmclaw.utils.paths.file_memory_dir", lambda: tmp_path,
    )
    tools = BuiltinTools()
    result = await tools.invoke(_call({
        "name": "build-pipeline",
        "content": "Body of the note.\n",
        "description": "Build pipeline notes",
    }))
    assert result.ok is True
    body = (tmp_path / "build-pipeline.md").read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "description: Build pipeline notes" in body
    assert "tags:" not in body
    assert "Body of the note." in body


@pytest.mark.asyncio
async def test_tags_only_emits_frontmatter(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "xmclaw.utils.paths.file_memory_dir", lambda: tmp_path,
    )
    tools = BuiltinTools()
    result = await tools.invoke(_call({
        "name": "x", "content": "body\n",
        "tags": ["build", "frontend"],
    }))
    assert result.ok is True
    body = (tmp_path / "x.md").read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "tags: [build, frontend]" in body
    assert "description:" not in body


@pytest.mark.asyncio
async def test_no_meta_emits_no_frontmatter(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "xmclaw.utils.paths.file_memory_dir", lambda: tmp_path,
    )
    tools = BuiltinTools()
    result = await tools.invoke(_call({
        "name": "plain", "content": "just body\n",
    }))
    assert result.ok is True
    body = (tmp_path / "plain.md").read_text(encoding="utf-8")
    assert not body.startswith("---")
    assert body.startswith("just body")


@pytest.mark.asyncio
async def test_append_skips_frontmatter_injection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "xmclaw.utils.paths.file_memory_dir", lambda: tmp_path,
    )
    # Pre-existing file with its own frontmatter.
    (tmp_path / "ex.md").write_text(
        "---\ndescription: original\n---\noriginal body\n",
        encoding="utf-8",
    )
    tools = BuiltinTools()
    result = await tools.invoke(_call({
        "name": "ex", "content": "appended chunk",
        "mode": "append",
        # description+tags must NOT be re-prepended on append.
        "description": "this should be ignored",
        "tags": ["ignored"],
    }))
    assert result.ok is True
    body = (tmp_path / "ex.md").read_text(encoding="utf-8")
    # The original frontmatter survives untouched at the top.
    assert body.startswith("---\ndescription: original\n---\n")
    # The new description / tags MUST NOT have been re-prepended.
    # (B-45 append separates with ``\n---\n\n`` so the count of ``---``
    # lines is misleading — check field uniqueness instead.)
    assert body.count("description:") == 1
    assert "this should be ignored" not in body
    assert "appended chunk" in body


@pytest.mark.asyncio
async def test_description_with_dashes_is_escaped(tmp_path, monkeypatch) -> None:
    """Three consecutive dashes inside the description would terminate
    the frontmatter block early; we replace them with em-dash."""
    monkeypatch.setattr(
        "xmclaw.utils.paths.file_memory_dir", lambda: tmp_path,
    )
    tools = BuiltinTools()
    result = await tools.invoke(_call({
        "name": "x", "content": "body",
        "description": "before --- after",
    }))
    assert result.ok is True
    body = (tmp_path / "x.md").read_text(encoding="utf-8")
    # Frontmatter block must be intact: opening + closing on their own lines.
    lines = body.split("\n")
    assert lines[0] == "---"
    closing_idx = next(i for i, ln in enumerate(lines[1:], 1) if ln == "---")
    # The description line lives between opening and closing.
    assert any("description:" in ln for ln in lines[1:closing_idx])
    # The literal --- got rewritten to em-dash.
    assert "before — after" in body
