"""B-127 — UserSkillsLoader unit tests.

Pins:
  * ``~/.xmclaw/skills_user/<id>/skill.py`` discovers + registers
    its Skill subclass into the SkillRegistry
  * id/version mismatch fails loudly (not silently)
  * manifest.json overrides defaults; absent → synthesised
    "created_by=user" manifest
  * malformed skill.py is skipped, others still load
  * idempotent re-load (same version registered twice) → ok
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader


# ── fixtures ────────────────────────────────────────────────────────


_GOOD_SKILL = """
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class MySkill(Skill):
    id = "{skill_id}"
    version = {version}

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result={{"hello": inp.args}}, side_effects=[])
"""


_FACTORY_SKILL = """
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class _Inner(Skill):
    def __init__(self, prefix: str):
        self.id = "{skill_id}"
        self.version = {version}
        self._prefix = prefix

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result={{"out": self._prefix}}, side_effects=[])

def build_skill():
    return _Inner(prefix="hello")
"""


def _write_skill(root: Path, skill_id: str, *, version: int = 1,
                 template: str = _GOOD_SKILL,
                 manifest: dict | None = None) -> Path:
    sd = root / skill_id
    sd.mkdir(parents=True)
    (sd / "skill.py").write_text(
        template.format(skill_id=skill_id, version=version),
        encoding="utf-8",
    )
    if manifest is not None:
        (sd / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
    return sd


# ── happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_arg_skill_loads_and_registers(tmp_path: Path) -> None:
    _write_skill(tmp_path, "my_skill", version=1)
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert len(results) == 1
    assert results[0].ok
    assert results[0].skill_id == "my_skill"
    assert results[0].version == 1
    assert "my_skill" in reg.list_skill_ids()
    assert reg.active_version("my_skill") == 1

    # Skill actually runs.
    skill = reg.get("my_skill")
    from xmclaw.skills.base import SkillInput
    out = await skill.run(SkillInput(args={"x": 1}))
    assert out.ok
    assert out.result == {"hello": {"x": 1}}


@pytest.mark.asyncio
async def test_factory_function_used_for_arg_taking_init(tmp_path: Path) -> None:
    """Skill subclass with non-zero __init__ → loader uses build_skill()."""
    _write_skill(tmp_path, "fact_skill", template=_FACTORY_SKILL)
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert results[0].ok
    skill = reg.get("fact_skill")
    from xmclaw.skills.base import SkillInput
    out = await skill.run(SkillInput(args={}))
    assert out.result == {"out": "hello"}


# ── manifest ──────────────────────────────────────────────────────


def test_manifest_synthesised_when_absent(tmp_path: Path) -> None:
    _write_skill(tmp_path, "x", version=1)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()
    ref = reg.ref("x")
    assert ref.manifest.created_by == "user"
    assert ref.manifest.id == "x"
    assert ref.manifest.version == 1


def test_manifest_loaded_from_disk(tmp_path: Path) -> None:
    _write_skill(tmp_path, "x", version=1, manifest={
        "created_by": "evolved",
        "permissions_fs": ["/tmp/safe"],
        "max_cpu_seconds": 60.0,
    })
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()
    m = reg.ref("x").manifest
    assert m.created_by == "evolved"
    assert m.permissions_fs == ("/tmp/safe",)
    assert m.max_cpu_seconds == 60.0


def test_manifest_id_mismatch_fails(tmp_path: Path) -> None:
    _write_skill(tmp_path, "x", version=1, manifest={"id": "wrong"})
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert not results[0].ok
    assert "disagrees" in (results[0].error or "")


# ── error handling ────────────────────────────────────────────────


def test_dir_id_mismatch_fails_loudly(tmp_path: Path) -> None:
    """Class declares id='other' but directory is 'mine' → reject."""
    sd = tmp_path / "mine"
    sd.mkdir()
    (sd / "skill.py").write_text(
        _GOOD_SKILL.format(skill_id="other", version=1),
        encoding="utf-8",
    )
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert not results[0].ok
    assert "disagrees" in (results[0].error or "")


def test_missing_skill_py_and_md_skipped(tmp_path: Path) -> None:
    """Epic #24 Phase 5: error message updated to mention both
    skill.py and SKILL.md after the markdown branch was added."""
    (tmp_path / "empty").mkdir()
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert len(results) == 1
    assert not results[0].ok
    assert "skill.py" in (results[0].error or "")
    assert "SKILL.md" in (results[0].error or "")


def test_import_error_does_not_kill_other_skills(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "skill.py").write_text(
        "import this_module_does_not_exist", encoding="utf-8",
    )
    _write_skill(tmp_path, "good", version=1)
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    bad_r = next(r for r in results if r.skill_id == "bad")
    good_r = next(r for r in results if r.skill_id == "good")
    assert not bad_r.ok
    assert good_r.ok
    assert "good" in reg.list_skill_ids()
    assert "bad" not in reg.list_skill_ids()


def test_no_skill_subclass_in_module(tmp_path: Path) -> None:
    sd = tmp_path / "noclass"
    sd.mkdir()
    (sd / "skill.py").write_text(
        "x = 1  # no Skill subclass at all\n", encoding="utf-8",
    )
    reg = SkillRegistry()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert not results[0].ok
    assert "no concrete Skill subclass" in (results[0].error or "")


def test_idempotent_reload_same_version(tmp_path: Path) -> None:
    """Re-running load_all on the same dir is a no-op for already-
    registered (id, version) pairs — daemon restart shouldn't
    explode."""
    _write_skill(tmp_path, "x", version=1)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()
    results = UserSkillsLoader(reg, tmp_path).load_all()
    assert results[0].ok  # treated as ok, not duplicate-error


# ── empty cases ───────────────────────────────────────────────────


def test_empty_root_returns_no_results(tmp_path: Path) -> None:
    assert UserSkillsLoader(SkillRegistry(), tmp_path).load_all() == []


def test_missing_root_returns_no_results(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope"
    assert UserSkillsLoader(SkillRegistry(), nonexistent).load_all() == []


def test_hidden_dirs_skipped(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "_pycache").mkdir()
    _write_skill(tmp_path, "real", version=1)
    results = UserSkillsLoader(SkillRegistry(), tmp_path).load_all()
    assert {r.skill_id for r in results} == {"real"}


# ── B-170 SKILL.md frontmatter → manifest.description ─────────────────


def _write_skill_md(
    root: Path, skill_id: str, *, body: str,
) -> Path:
    sd = root / skill_id
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(body, encoding="utf-8")
    return sd


def test_skill_md_frontmatter_populates_manifest(tmp_path: Path) -> None:
    """skills.sh-style SKILL.md → manifest carries description, title,
    triggers (so /api/v2/skills can ship them to the UI)."""
    md = (
        "---\n"
        "name: git-commit\n"
        "description: Execute git commit with conventional commit "
        "message analysis.\n"
        "triggers: ['/commit', 'commit changes']\n"
        "---\n\n"
        "# Git Commit\n\n"
        "Standardised commits using Conventional Commits.\n"
    )
    _write_skill_md(tmp_path, "git-commit", body=md)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()

    m = reg.ref("git-commit").manifest
    assert m.title == "git-commit"
    assert "conventional commit message" in m.description.lower()
    assert m.triggers == ("/commit", "commit changes")


def test_skill_md_no_frontmatter_uses_h1_and_first_para(tmp_path: Path) -> None:
    """Plain SKILL.md without frontmatter → fallback heuristic
    (first H1 → title, first paragraph → description)."""
    md = (
        "# Brainstorming Session\n\n"
        "Walk the user through a structured brainstorming session "
        "with divergent then convergent passes.\n\n"
        "## Steps\n"
        "1. ...\n"
    )
    _write_skill_md(tmp_path, "brainstorming", body=md)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()

    m = reg.ref("brainstorming").manifest
    assert m.title == "Brainstorming Session"
    assert "structured brainstorming" in m.description


def test_skill_md_partial_frontmatter_fills_missing_from_h1(
    tmp_path: Path,
) -> None:
    """Frontmatter has only ``description`` → title still comes from H1."""
    md = (
        "---\n"
        "description: Help draft pull-request descriptions.\n"
        "---\n\n"
        "# Documentation Writer\n\n"
        "Body...\n"
    )
    _write_skill_md(tmp_path, "documentation-writer", body=md)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()

    m = reg.ref("documentation-writer").manifest
    assert m.title == "Documentation Writer"
    assert m.description == "Help draft pull-request descriptions."


def test_skill_md_quoted_description_unwraps(tmp_path: Path) -> None:
    """Single-quoted multi-clause description (skills.sh style) →
    quotes stripped."""
    md = (
        "---\n"
        "name: enhance-prompt\n"
        "description: 'Improve prompts iteratively. Use when user "
        "asks for prompt feedback.'\n"
        "---\n\n"
        "# Enhance Prompt\n\nBody.\n"
    )
    _write_skill_md(tmp_path, "enhance-prompt", body=md)
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()

    m = reg.ref("enhance-prompt").manifest
    assert m.description.startswith("Improve prompts iteratively")
    assert "'" not in m.description.split(".")[0]  # leading quote stripped


def test_manifest_to_dict_round_trips_description(tmp_path: Path) -> None:
    """Sanity: SkillManifest.to_dict() puts description in JSON output
    so /api/v2/skills can ship it to the UI (the gap that produced the
    'all skills show —' bug)."""
    from xmclaw.skills.manifest import SkillManifest
    m = SkillManifest(
        id="x", version=1, title="X", description="does x",
        triggers=("a", "b"),
    )
    d = m.to_dict()
    assert d["description"] == "does x"
    assert d["title"] == "X"
    assert d["triggers"] == ["a", "b"]  # tuple → list for JSON
