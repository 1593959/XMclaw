"""Tests for Claude Desktop plugin.json bridge (ClaudePluginSkill)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from xmclaw.skills.claude_plugin_skill import (
    ClaudePluginSkill,
    build_skill_from_plugin_json,
    parse_plugin_json,
)


def test_parse_plugin_json_happy_path() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"name": "hyperframes", "version": "1.2.3"}, f)
        f.flush()
        data = parse_plugin_json(Path(f.name))
    assert data["name"] == "hyperframes"
    assert data["version"] == "1.2.3"


def test_parse_plugin_json_invalid() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("not json")
        f.flush()
        with pytest.raises(ValueError):
            parse_plugin_json(Path(f.name))


def test_build_skill_from_plugin_json_minimal() -> None:
    tmp = Path(tempfile.mkdtemp())
    plugin_json = tmp / "plugin.json"
    plugin_json.write_text(
        json.dumps({
            "name": "TestPlugin",
            "version": "2.0.0",
            "description": "A test plugin",
        }),
        encoding="utf-8",
    )
    skill, err = build_skill_from_plugin_json(tmp, plugin_json)
    assert err == ""
    assert skill is not None
    assert skill.id == tmp.name
    assert skill.version == 2
    assert "A test plugin" in skill.body
    assert "Claude Desktop plugin" in skill.body


def test_build_skill_from_plugin_json_with_tools() -> None:
    tmp = Path(tempfile.mkdtemp())
    plugin_json = tmp / "plugin.json"
    plugin_json.write_text(
        json.dumps({
            "name": "HyperFrames",
            "version": "1.0.0",
            "description": "Frame manager",
            "instructions": "Use these tools to manage frames.",
            "tools": [
                {"name": "create_frame", "description": "Create a frame"},
                {"name": "delete_frame", "description": "Delete a frame"},
            ],
        }),
        encoding="utf-8",
    )
    skill, err = build_skill_from_plugin_json(tmp, plugin_json)
    assert err == ""
    assert skill is not None
    assert "create_frame" in skill.body
    assert "delete_frame" in skill.body
    assert "Use these tools" in skill.body


def test_build_skill_from_plugin_json_reads_readme() -> None:
    tmp = Path(tempfile.mkdtemp())
    plugin_json = tmp / "plugin.json"
    plugin_json.write_text(
        json.dumps({"name": "ReadmePlugin", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (tmp / "README.md").write_text("# Extra context\nThis is helpful.", encoding="utf-8")
    skill, err = build_skill_from_plugin_json(tmp, plugin_json)
    assert err == ""
    assert skill is not None
    assert "Extra context" in skill.body


@pytest.mark.asyncio
async def test_claude_plugin_skill_run() -> None:
    from xmclaw.skills.base import SkillInput

    skill = ClaudePluginSkill(
        id="test_plugin",
        body="# Hello\nDo this and that.",
        version=1,
        skill_dir="/tmp/test",
    )
    out = await skill.run(SkillInput(args={}))
    assert out.ok is True
    assert out.result["kind"] == "claude_plugin_procedure"
    assert "Do this and that" in out.result["instructions"]
    assert "/tmp/test" in out.result["instructions"]
