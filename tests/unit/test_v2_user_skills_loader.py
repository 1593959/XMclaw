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
