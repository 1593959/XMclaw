"""B-158 — SKILL.md ``<base>_v<N>`` version dedup.

Pins:
  * loader keeps only the highest version per base_id
  * skills without ``_v<N>`` suffix pass through untouched
  * list_for_api exposes ``base_id`` / ``version`` / ``older_versions``
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.daemon.learned_skills import (
    LearnedSkillsLoader,
    _split_versioned,
)


def _write_skill(root: Path, skill_id: str, body: str = "step 1") -> None:
    d = root / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: test\n---\n# {skill_id}\n\n{body}\n",
        encoding="utf-8",
    )


# ── _split_versioned helper ─────────────────────────────────────────


def test_split_versioned_with_suffix() -> None:
    base, n = _split_versioned("auto_repair_bdf153_v37")
    assert base == "auto_repair_bdf153"
    assert n == 37


def test_split_versioned_without_suffix() -> None:
    base, n = _split_versioned("find-skills")
    assert base == "find-skills"
    assert n is None


def test_split_versioned_handles_v_in_middle() -> None:
    """Only trailing _v<N> counts — 'v1_helper' shouldn't strip 'v1'."""
    base, n = _split_versioned("v1_helper")
    assert base == "v1_helper"
    assert n is None


# ── loader dedup ────────────────────────────────────────────────────


def test_loader_keeps_only_latest_version(tmp_path: Path) -> None:
    """Three versions of the same base_id → only highest survives."""
    root = tmp_path / "skills"
    _write_skill(root, "auto_repair_xxx_v37")
    _write_skill(root, "auto_repair_xxx_v38")
    _write_skill(root, "auto_repair_xxx_v36")
    loader = LearnedSkillsLoader(root)
    skills = loader.list_skills()
    assert len(skills) == 1
    assert skills[0].skill_id == "auto_repair_xxx_v38"


def test_loader_passes_through_unversioned(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "find-skills")
    _write_skill(root, "writing-plans")
    loader = LearnedSkillsLoader(root)
    ids = sorted(s.skill_id for s in loader.list_skills())
    assert ids == ["find-skills", "writing-plans"]


def test_loader_mixes_versioned_and_unversioned(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "find-skills")
    _write_skill(root, "auto_repair_v1")
    _write_skill(root, "auto_repair_v3")
    _write_skill(root, "auto_repair_v2")
    loader = LearnedSkillsLoader(root)
    ids = sorted(s.skill_id for s in loader.list_skills())
    # find-skills (no version) + auto_repair_v3 (latest)
    assert ids == ["auto_repair_v3", "find-skills"]


# ── list_for_api exposes version metadata ──────────────────────────


def test_list_for_api_carries_base_and_version(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "auto_repair_xxx_v37")
    _write_skill(root, "auto_repair_xxx_v38")
    loader = LearnedSkillsLoader(root)
    rows = loader.list_for_api()
    assert len(rows) == 1
    row = rows[0]
    assert row["skill_id"] == "auto_repair_xxx_v38"
    assert row["base_id"] == "auto_repair_xxx"
    assert row["version"] == 38
    # The v37 entry shows up under older_versions
    older = row["older_versions"]
    assert len(older) == 1
    assert older[0]["skill_id"] == "auto_repair_xxx_v37"
    assert older[0]["version"] == 37


def test_list_for_api_unversioned_has_null_version(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "find-skills")
    rows = LearnedSkillsLoader(root).list_for_api()
    assert rows[0]["base_id"] == "find-skills"
    assert rows[0]["version"] is None
    assert rows[0]["older_versions"] == []


def test_loader_dedupe_works_across_extra_roots(tmp_path: Path) -> None:
    """B-149+158 interplay: extra_roots scan + version dedup combined."""
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    _write_skill(primary, "auto_x_v2")
    _write_skill(extra, "auto_x_v5")  # newer in extra root
    loader = LearnedSkillsLoader(primary, extra_roots=[extra])
    skills = loader.list_skills()
    # Only one survives. extra_roots may or may not win depending on
    # order — primary is scanned first, gets "auto_x_v2" reserved.
    # The B-149 cross-root dedup runs FIRST (by skill_id), then B-158
    # version dedup runs on the survivors.
    # In this case primary's v2 wins (primary scanned first), v5 in
    # extra is ignored. Both have different skill_ids though
    # (auto_x_v2 vs auto_x_v5), so B-149 dedup doesn't kick in —
    # B-158 dedup does.
    assert len(skills) == 1
    assert skills[0].skill_id == "auto_x_v5"
