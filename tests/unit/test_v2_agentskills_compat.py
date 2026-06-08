"""Tests for xmclaw.skills.agentskills_compat."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from xmclaw.skills.agentskills_compat import (
    AgentSkillsCatalog,
    AgentSkillsIndex,
    _normalize_skill_name,
    validate_skill_md_frontmatter,
)
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRef, SkillRegistry


class FakeManifest:
    def __init__(self, title="", description="", trust_level=""):
        self.title = title
        self.description = description
        self.trust_level = trust_level
        self.permissions_fs = ()
        self.permissions_net = ()


class FakeRef:
    def __init__(self, title="", description="", skill_dir=""):
        self.manifest = FakeManifest(title=title, description=description)
        self.version = 1
        self.skill_dir = skill_dir


class FakeRegistry:
    def __init__(self, skills=None):
        self._skills = skills or {}

    def list_skill_ids(self):
        return list(self._skills.keys())

    def get_ref(self, sid):
        return self._skills[sid]


def test_normalize_skill_name():
    assert _normalize_skill_name("MySkill") == "myskill"
    assert _normalize_skill_name("demo.read_and_summarize") == "demo-read-and-summarize"
    assert _normalize_skill_name("skill_with_underscores") == "skill-with-underscores"
    assert _normalize_skill_name("a--b__c") == "a-b-c"
    assert _normalize_skill_name("-leading-") == "leading"
    # 64-char cap
    long_name = "a" * 100
    assert len(_normalize_skill_name(long_name)) == 64


def test_validate_skill_md_frontmatter_ok():
    body = "---\nname: foo\ndescription: bar\n---\n# Body"
    ok, issues = validate_skill_md_frontmatter(body)
    assert ok is True
    assert issues == []


def test_validate_skill_md_frontmatter_missing_name():
    body = "---\ndescription: bar\n---\n# Body"
    ok, issues = validate_skill_md_frontmatter(body)
    assert ok is False
    assert any("name" in i for i in issues)


def test_validate_skill_md_frontmatter_missing_description():
    body = "---\nname: foo\n---\n# Body"
    ok, issues = validate_skill_md_frontmatter(body)
    assert ok is False
    assert any("description" in i for i in issues)


def test_validate_skill_md_frontmatter_no_frontmatter():
    ok, issues = validate_skill_md_frontmatter("# No frontmatter")
    assert ok is False
    assert any("missing" in i.lower() for i in issues)


def test_catalog_build():
    reg = FakeRegistry({
        "skill-a": FakeRef(title="Skill A", description="Does A"),
        "skill-b": FakeRef(title="Skill B", description="Does B"),
    })
    catalog = AgentSkillsCatalog(reg)
    text = catalog.build()
    assert "## Available Skills" in text
    assert "Skill A" in text
    assert "Does A" in text
    assert "Skill B" in text


def test_catalog_empty():
    reg = FakeRegistry()
    catalog = AgentSkillsCatalog(reg)
    assert catalog.build() == ""


def test_index_build():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: test skill\n---\n# Body",
            encoding="utf-8",
        )
        reg = FakeRegistry({
            "my-skill": FakeRef(
                title="My Skill",
                description="test skill",
                skill_dir=str(skill_dir),
            ),
        })
        idx = AgentSkillsIndex(reg, data_dir=tmp, base_url="")
        data = idx.build()
        assert data["$schema"]
        assert len(data["skills"]) == 1
        sk = data["skills"][0]
        assert sk["name"] == "my-skill"
        assert sk["type"] == "skill-md"
        assert sk["description"] == "test skill"
        assert sk["url"].startswith("file://")
        assert sk["digest"].startswith("sha256:")
        assert "metadata" in sk
        assert "xmclaw" in sk["metadata"]


def test_index_write():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: test\n---\n# Body",
            encoding="utf-8",
        )
        reg = FakeRegistry({
            "my-skill": FakeRef(
                title="My Skill",
                description="test",
                skill_dir=str(skill_dir),
            ),
        })
        idx = AgentSkillsIndex(reg, data_dir=tmp)
        path = idx.write()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["skills"]) == 1


def test_index_missing_skill_md():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "my-skill"
        skill_dir.mkdir()
        # No SKILL.md
        reg = FakeRegistry({
            "my-skill": FakeRef(
                title="My Skill",
                description="test",
                skill_dir=str(skill_dir),
            ),
        })
        idx = AgentSkillsIndex(reg, data_dir=tmp)
        data = idx.build()
        assert len(data["skills"]) == 0
