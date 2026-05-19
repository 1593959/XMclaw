"""Epic #27 P2 G-08 (2026-05-19) — skill_propose meta-tool +
loader UNTRUSTED-trust assignment for agent-proposed skills.

Pins:
  * skill_propose validates name (slug), body length, refuses
    duplicates.
  * Writes both SKILL.md + .proposed.json under user_skills_dir().
  * Prepends frontmatter when body lacks it AND description given.
  * UserSkillsLoader._scan_proposed_skill_ids reads markers.
  * _trust_for returns UNTRUSTED when marker present, overriding
    even INSTALLED tier (proposal precedence rule).
  * Routes through the always-on prefilter whitelist.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest, SkillTrustLevel
from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    META_PROPOSE_TOOL_NAME,
    SkillToolProvider,
)
from xmclaw.skills.user_loader import UserSkillsLoader


class _NoopSkill(Skill):
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result="ok", side_effects=[])


# ── invocation validation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_propose_requires_name(tmp_path: Path) -> None:
    bridge = SkillToolProvider(SkillRegistry())
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={"body": "x" * 100},
            provenance="synthetic",
        ))
    assert res.ok is False
    assert "name" in (res.error or "")


@pytest.mark.asyncio
async def test_skill_propose_requires_body_min_50_chars(
    tmp_path: Path,
) -> None:
    bridge = SkillToolProvider(SkillRegistry())
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={"name": "my-skill", "body": "tiny"},
            provenance="synthetic",
        ))
    assert res.ok is False
    assert "50" in (res.error or "")


@pytest.mark.asyncio
async def test_skill_propose_rejects_bad_slug(tmp_path: Path) -> None:
    bridge = SkillToolProvider(SkillRegistry())
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={
                "name": "Has Spaces!",
                "body": "x" * 100,
            },
            provenance="synthetic",
        ))
    assert res.ok is False
    assert "name" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_skill_propose_refuses_existing(tmp_path: Path) -> None:
    """Re-proposing an existing skill is refused — author should
    use skill_diff + skill_rollback to edit the existing one."""
    bridge = SkillToolProvider(SkillRegistry())
    (tmp_path / "existing-skill").mkdir()
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={
                "name": "existing-skill",
                "body": "x" * 100,
            },
            provenance="synthetic",
        ))
    assert res.ok is False
    assert "already exists" in (res.error or "")


# ── file writes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_propose_writes_skill_md_and_marker(
    tmp_path: Path,
) -> None:
    bridge = SkillToolProvider(SkillRegistry())
    body = "## My Skill\n\nDoes a useful thing. " + "x" * 30
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={"name": "my-new-skill", "body": body},
            provenance="synthetic",
        ))
    assert res.ok is True
    assert res.content["trust"] == "untrusted"
    skill_dir = tmp_path / "my-new-skill"
    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == body
    marker = json.loads(
        (skill_dir / ".proposed.json").read_text(encoding="utf-8"),
    )
    assert marker["proposed_by"] == "agent"
    assert marker["evidence_count"] == 0
    assert marker["promote_after_evidence"] == 3
    assert "proposed_at" in marker


@pytest.mark.asyncio
async def test_skill_propose_prepends_frontmatter_when_missing(
    tmp_path: Path,
) -> None:
    """When body has no frontmatter AND description given, the tool
    synthesises a minimal frontmatter block."""
    bridge = SkillToolProvider(SkillRegistry())
    body = "# Skill body content " + "x" * 50
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={
                "name": "no-frontmatter",
                "body": body,
                "description": "One-line summary",
            },
            provenance="synthetic",
        ))
    assert res.ok is True
    written = (tmp_path / "no-frontmatter" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    assert written.startswith("---")
    assert "name: no-frontmatter" in written
    assert "description: One-line summary" in written
    assert "# Skill body content" in written


@pytest.mark.asyncio
async def test_skill_propose_preserves_existing_frontmatter(
    tmp_path: Path,
) -> None:
    """When body already has frontmatter, no synthesis."""
    bridge = SkillToolProvider(SkillRegistry())
    body = (
        "---\nname: explicit\ndescription: author wrote this\n---\n\n"
        + "x" * 100
    )
    with patch("xmclaw.utils.paths.user_skills_dir", return_value=tmp_path):
        res = await bridge.invoke(ToolCall(
            name=META_PROPOSE_TOOL_NAME,
            args={
                "name": "with-frontmatter",
                "body": body,
                "description": "agent override should NOT be used",
            },
            provenance="synthetic",
        ))
    assert res.ok is True
    written = (tmp_path / "with-frontmatter" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    # Exact same body — the description arg should NOT have been
    # injected because the body already had its own frontmatter.
    assert written == body


# ── loader trust integration ───────────────────────────────────────


def test_loader_finds_proposed_marker(tmp_path: Path) -> None:
    """UserSkillsLoader scans for .proposed.json markers; skills
    with the marker get UNTRUSTED trust."""
    # Set up: one proposed skill + one regular user skill.
    (tmp_path / "proposed-skill").mkdir()
    (tmp_path / "proposed-skill" / ".proposed.json").write_text(
        '{"proposed_by": "agent"}', encoding="utf-8",
    )
    (tmp_path / "regular-skill").mkdir()

    nonexistent = tmp_path / "no-marketplace.json"
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=nonexistent,
    ):
        loader = UserSkillsLoader(SkillRegistry(), tmp_path)

    assert "proposed-skill" in loader._proposed_skill_ids
    assert "regular-skill" not in loader._proposed_skill_ids
    assert loader._trust_for("proposed-skill") == SkillTrustLevel.UNTRUSTED
    assert loader._trust_for("regular-skill") == SkillTrustLevel.USER


def test_proposed_trumps_installed_trust(tmp_path: Path) -> None:
    """If a skill is BOTH proposed (agent wrote it) AND in the
    marketplace registry, UNTRUSTED wins — the proposal marker is
    the strongest signal that the skill hasn't been reviewed."""
    (tmp_path / "weird-skill").mkdir()
    (tmp_path / "weird-skill" / ".proposed.json").write_text(
        '{"proposed_by": "agent"}', encoding="utf-8",
    )

    # Fake a marketplace registry that ALSO claims this skill.
    market_path = tmp_path / ".marketplace.json"
    market_path.write_text(json.dumps({
        "skills": [{"id": "weird-skill", "version": "1", "source": "github:x/y"}],
    }), encoding="utf-8")

    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=market_path,
    ):
        loader = UserSkillsLoader(SkillRegistry(), tmp_path)
    assert loader._trust_for("weird-skill") == SkillTrustLevel.UNTRUSTED


def test_loader_empty_when_no_proposed_markers(tmp_path: Path) -> None:
    """No .proposed.json files anywhere → empty proposed set."""
    (tmp_path / "skill1").mkdir()
    (tmp_path / "skill2").mkdir()
    nonexistent = tmp_path / "no-marketplace.json"
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=nonexistent,
    ):
        loader = UserSkillsLoader(SkillRegistry(), tmp_path)
    assert loader._proposed_skill_ids == frozenset()


def test_loader_ignores_dot_dirs_in_proposed_scan(tmp_path: Path) -> None:
    """Dot-prefixed dirs (e.g. ``.versions``) and underscore-prefixed
    dirs (e.g. ``__pycache__``) are NOT considered skills, so their
    .proposed.json (if any) doesn't count."""
    bad = tmp_path / ".versions"
    bad.mkdir()
    (bad / ".proposed.json").write_text("{}", encoding="utf-8")
    bad2 = tmp_path / "__pycache__"
    bad2.mkdir()
    (bad2 / ".proposed.json").write_text("{}", encoding="utf-8")

    nonexistent = tmp_path / "no-marketplace.json"
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=nonexistent,
    ):
        loader = UserSkillsLoader(SkillRegistry(), tmp_path)
    assert loader._proposed_skill_ids == frozenset()


# ── prefilter integration ──────────────────────────────────────────


def test_skill_propose_passes_prefilter() -> None:
    """skill_propose must reach the LLM regardless of registry size /
    query token overlap — it's a self-improvement affordance."""
    reg = SkillRegistry()
    for i in range(40):
        reg.register(
            _NoopSkill(f"filler-{i}"),
            SkillManifest(id=f"filler-{i}", version=1),
        )
    bridge = SkillToolProvider(reg)
    survivors = select_relevant_skills(
        "天气", bridge.list_tools(), top_k=12,
    )
    names = {s.name for s in survivors}
    assert META_PROPOSE_TOOL_NAME in names
