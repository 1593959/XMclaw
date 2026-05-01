"""MarkdownProcedureSkill + user_loader SKILL.md/extra-roots — Epic #24 Phase 5.

Locks the contract:

* MarkdownProcedureSkill returns body text via run() with frontmatter
  stripped. SkillToolProvider can bridge it through unchanged.
* UserSkillsLoader picks SKILL.md when skill.py is absent.
* skill.py wins when both exist (user signaled they want code).
* ``extra_roots`` opt-in scans additional paths. First-wins on
  collisions: canonical path is the source-of-truth, extras are
  overlay.
* SkillRegistry sees the wrapped skill the same way it sees Python
  skills (HEAD pointer, history).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.base import SkillInput
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader


# ── MarkdownProcedureSkill ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_markdown_skill_run_returns_body() -> None:
    body = "# Steps\n\n1. Read file\n2. Summarize\n"
    s = MarkdownProcedureSkill(id="demo.md", body=body, version=1)
    out = await s.run(SkillInput(args={}))
    assert out.ok
    assert out.result["kind"] == "markdown_procedure"
    assert out.result["skill_id"] == "demo.md"
    assert "Read file" in out.result["body"]
    assert out.side_effects == []


@pytest.mark.asyncio
async def test_markdown_skill_strips_frontmatter() -> None:
    body = (
        "---\n"
        "name: foo\n"
        "description: a test\n"
        "---\n"
        "# Body starts here\n\nstep 1.\n"
    )
    s = MarkdownProcedureSkill(id="x", body=body)
    out = await s.run(SkillInput(args={}))
    assert "name: foo" not in out.result["body"]
    assert "Body starts here" in out.result["body"]


# ── UserSkillsLoader SKILL.md branch ────────────────────────────────


def test_loader_picks_skill_md_when_no_skill_py(tmp_path: Path) -> None:
    root = tmp_path / "skills_user"
    skill_dir = root / "find-skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Find Skills\n\n1. List skills\n2. Match keywords\n",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    results = UserSkillsLoader(registry, root).load_all()

    assert len(results) == 1
    r = results[0]
    assert r.ok
    assert r.kind == "markdown"
    assert r.skill_id == "find-skills"
    assert r.version == 1
    # Registry sees it.
    assert "find-skills" in registry.list_skill_ids()
    skill = registry.get("find-skills")
    assert isinstance(skill, MarkdownProcedureSkill)


def test_loader_python_wins_over_markdown(tmp_path: Path) -> None:
    """Both skill.py and SKILL.md present → skill.py wins."""
    root = tmp_path / "skills_user"
    skill_dir = root / "x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("md", encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        "from xmclaw.skills.base import Skill, SkillInput, SkillOutput\n"
        "class X(Skill):\n"
        "    id = 'x'\n"
        "    version = 1\n"
        "    async def run(self, inp):\n"
        "        return SkillOutput(ok=True, result='py', side_effects=[])\n",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    results = UserSkillsLoader(registry, root).load_all()

    assert len(results) == 1
    assert results[0].ok
    assert results[0].kind == "python"


def test_loader_handles_missing_root_gracefully(tmp_path: Path) -> None:
    """Non-existent skills_user dir → empty result, no crash."""
    registry = SkillRegistry()
    results = UserSkillsLoader(registry, tmp_path / "does-not-exist").load_all()
    assert results == []


# ── extra_roots opt-in ──────────────────────────────────────────────


def test_loader_scans_extra_roots(tmp_path: Path) -> None:
    canonical = tmp_path / "skills_user"
    extra = tmp_path / "agents_skills"
    canonical.mkdir()
    extra_skill = extra / "git-commit"
    extra_skill.mkdir(parents=True)
    (extra_skill / "SKILL.md").write_text(
        "# git-commit\n\nstaged files only.\n",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    results = UserSkillsLoader(
        registry, canonical, extra_roots=[extra],
    ).load_all()

    assert len(results) == 1
    assert results[0].skill_id == "git-commit"
    assert results[0].kind == "markdown"
    assert str(extra) in (results[0].source_root or "")


def test_canonical_root_shadows_extra_root(tmp_path: Path) -> None:
    """Same skill_id in both roots → canonical wins."""
    canonical = tmp_path / "skills_user"
    extra = tmp_path / "agents_skills"

    # Canonical: SKILL.md says "canonical version"
    canonical_skill = canonical / "duplicate"
    canonical_skill.mkdir(parents=True)
    (canonical_skill / "SKILL.md").write_text(
        "canonical version", encoding="utf-8",
    )

    # Extra: SKILL.md says "extra version"
    extra_skill = extra / "duplicate"
    extra_skill.mkdir(parents=True)
    (extra_skill / "SKILL.md").write_text(
        "extra version", encoding="utf-8",
    )

    registry = SkillRegistry()
    results = UserSkillsLoader(
        registry, canonical, extra_roots=[extra],
    ).load_all()

    # Only canonical loaded (first-wins rule).
    ok_results = [r for r in results if r.ok]
    assert len(ok_results) == 1
    assert ok_results[0].skill_id == "duplicate"
    assert str(canonical) in (ok_results[0].source_root or "")
    skill = registry.get("duplicate")
    assert isinstance(skill, MarkdownProcedureSkill)
    assert "canonical version" in skill.body


def test_loader_skips_dot_underscore_dirs(tmp_path: Path) -> None:
    root = tmp_path / "skills_user"
    (root / ".hidden").mkdir(parents=True)
    (root / "_internal").mkdir(parents=True)
    (root / "real").mkdir(parents=True)
    (root / ".hidden" / "SKILL.md").write_text("hidden", encoding="utf-8")
    (root / "_internal" / "SKILL.md").write_text("priv", encoding="utf-8")
    (root / "real" / "SKILL.md").write_text("real", encoding="utf-8")

    registry = SkillRegistry()
    results = UserSkillsLoader(registry, root).load_all()
    skill_ids = {r.skill_id for r in results if r.ok}
    assert skill_ids == {"real"}
