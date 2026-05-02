"""B-171 — migrator for legacy ``~/.xmclaw/auto_evo/skills/``.

Pins:
  * Discovery groups directories by frontmatter ``name`` and picks
    highest ``_v<N>`` version per lineage.
  * ``xm-auto-evo`` directory is excluded.
  * Directories without SKILL.md or with no frontmatter ``name``
    are dropped silently.
  * Migration writes target SKILL.md with rewritten frontmatter:
    ``created_by: evolved``, ``signals_match`` → ``triggers``, drops
    ``auto_created`` / ``created_at`` / ``level``.
  * Existing target dir is preserved (NOT clobbered).
  * Dry-run reports what WOULD happen but writes nothing.
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.cli.migrate_auto_evo import (
    discover_candidates,
    migrate,
)


_DEFAULT_NON_SHELL_BODY = (
    "# Procedure\n\n"
    "Step 1: read the input. Step 2: process. Step 3: emit output.\n"
    "Use grep / file_read directly — no external dependency.\n"
)


def _write_legacy_skill(
    root: Path, dirname: str, *, frontmatter: str,
    body: str = _DEFAULT_NON_SHELL_BODY,
) -> Path:
    sd = root / dirname
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n" + frontmatter.strip() + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return sd


# ── discovery ──────────────────────────────────────────────────────


def test_discover_picks_highest_version_per_lineage(tmp_path: Path) -> None:
    """3 dirs share the same frontmatter ``name`` — winner is the
    one with the largest ``_v<N>`` suffix."""
    src = tmp_path / "auto_evo" / "skills"
    _write_legacy_skill(
        src, "auto_repair_aaa_v37",
        frontmatter='name: repair\ndescription: "Repair v37"\n',
    )
    _write_legacy_skill(
        src, "auto_repair_bbb_v38",
        frontmatter='name: repair\ndescription: "Repair v38"\n',
    )
    _write_legacy_skill(
        src, "auto_repair_ccc",  # no version suffix
        frontmatter='name: repair\ndescription: "Repair no-version"\n',
    )
    cands = discover_candidates(src)
    assert len(cands) == 1
    assert cands[0].canonical_name == "repair"
    assert cands[0].source_dir.name == "auto_repair_bbb_v38"
    assert cands[0].version == 38
    assert cands[0].target_id == "auto-repair"


def test_discover_skips_xm_auto_evo_and_no_skill_md(tmp_path: Path) -> None:
    src = tmp_path / "auto_evo" / "skills"
    src.mkdir(parents=True)
    (src / "xm-auto-evo").mkdir()
    (src / "xm-auto-evo" / "SKILL.md").write_text(
        "---\nname: xm-auto-evo\n---\n", encoding="utf-8",
    )
    (src / "empty_dir").mkdir()  # no SKILL.md
    _write_legacy_skill(
        src, "auto_real_v1",
        frontmatter='name: real\ndescription: "real one"\n',
    )
    cands = discover_candidates(src)
    assert {c.target_id for c in cands} == {"auto-real"}


def test_discover_skips_missing_or_blank_name(tmp_path: Path) -> None:
    src = tmp_path / "auto_evo" / "skills"
    _write_legacy_skill(
        src, "auto_no_name",
        frontmatter='description: "missing name"\n',
    )
    _write_legacy_skill(
        src, "auto_blank_name",
        frontmatter='name: ""\ndescription: "blank"\n',
    )
    _write_legacy_skill(
        src, "auto_real",
        frontmatter='name: real_thing\ndescription: "ok"\n',
    )
    cands = discover_candidates(src)
    assert {c.target_id for c in cands} == {"auto-real-thing"}


def test_discover_returns_empty_when_root_missing(tmp_path: Path) -> None:
    assert discover_candidates(tmp_path / "nope") == []


# ── migration ──────────────────────────────────────────────────────


def test_migrate_rewrites_frontmatter(tmp_path: Path) -> None:
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "auto_entity_reference_xxx_v29",
        frontmatter=(
            'name: entity_reference\n'
            'description: "Auto-generated skill"\n'
            'level: 0\n'
            'auto_created: true\n'
            'created_at: 2026-04-28T07:24:37.254Z\n'
            'signals_match:\n'
            '  - "category:entity_reference"\n'
        ),
        body=(
            "# entity_reference\n\n"
            "Step 1: parse the entity. Step 2: resolve the reference.\n"
        ),
    )
    results = migrate(src, target)
    assert len(results) == 1
    r = results[0]
    assert r.ok and not r.skipped
    assert r.target_id == "auto-entity-reference"

    written = (
        target / "auto-entity-reference" / "SKILL.md"
    ).read_text(encoding="utf-8")
    # Old housekeeping fields must be gone.
    assert "auto_created" not in written
    assert "created_at" not in written
    assert "level: 0" not in written
    # signals_match → triggers
    assert "signals_match" not in written
    assert "triggers" in written
    assert "category:entity_reference" in written
    # New audit fields injected.
    assert "created_by: evolved" in written
    assert "migrated_from: auto_entity_reference_xxx_v29" in written
    # Body preserved.
    assert "# entity_reference" in written


def test_migrate_does_not_clobber_existing_target(tmp_path: Path) -> None:
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "auto_repair_v38",
        frontmatter='name: repair\ndescription: "from migrator"\n',
        body=(
            "MIGRATED BODY\n\n"
            "Step 1: pull. Step 2: build. Step 3: test. Step 4: ship.\n"
        ),
    )
    # User already has a hand-installed `auto-repair` — must NOT
    # be overwritten.
    (target / "auto-repair").mkdir(parents=True)
    (target / "auto-repair" / "SKILL.md").write_text(
        "USER WROTE THIS", encoding="utf-8",
    )
    results = migrate(src, target)
    assert len(results) == 1
    assert results[0].skipped is True
    assert "already exists" in results[0].reason
    # Original user content untouched.
    text = (target / "auto-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert text == "USER WROTE THIS"


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "auto_x_v1",
        frontmatter='name: x_thing\ndescription: "y"\n',
    )
    results = migrate(src, target, dry_run=True)
    assert len(results) == 1
    assert results[0].ok and not results[0].skipped
    assert "dry-run" in results[0].reason
    assert not (target / "auto-x-thing").exists()


def test_migrate_users_actual_dataset_pattern(tmp_path: Path) -> None:
    """End-to-end smoke matching the user's actual auto_evo layout
    (5 lineages + xm-auto-evo + b29 test artifact)."""
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"

    # The shapes from the real machine.
    _write_legacy_skill(src, "auto_analysis_9372cc",
        frontmatter='name: analysis\ndescription: "auto-gen"\n')
    _write_legacy_skill(src, "auto_b29_invoke_test",
        frontmatter='name: auto_b29_invoke_test\ndescription: "test"\n')
    _write_legacy_skill(src, "auto_capability_gap_rpa6m8_v1",
        frontmatter='name: capability_gap\ndescription: "gap"\n')
    _write_legacy_skill(src, "auto_entity_reference_a4087e_v28",
        frontmatter='name: entity_reference\ndescription: "v28"\n')
    _write_legacy_skill(src, "auto_entity_reference_d90533_v29",
        frontmatter='name: entity_reference\ndescription: "v29"\n')
    _write_legacy_skill(src, "auto_entity_reference_k0z31a_v1",
        frontmatter='name: entity_reference\ndescription: "v1"\n')
    _write_legacy_skill(src, "auto_quality_issue_tigis1_v1",
        frontmatter='name: quality_issue\ndescription: "qi"\n')
    _write_legacy_skill(src, "auto_repair_40bb68_v38",
        frontmatter='name: repair\ndescription: "v38"\n')
    _write_legacy_skill(src, "auto_repair_bdf153_v37",
        frontmatter='name: repair\ndescription: "v37"\n')
    (src / "xm-auto-evo").mkdir()  # the deleted Node project itself

    results = migrate(src, target)
    target_ids = sorted(r.target_id for r in results if r.ok)
    # auto_b29_invoke_test has name 'auto_b29_invoke_test' so it
    # IS migrated as auto-auto-b29-invoke-test (its frontmatter name
    # is itself prefix-y but it's data, not the system) — but our
    # rule is to migrate any non-xm-auto-evo with a real name, so it
    # comes through. The 5 the user named come through too.
    assert "auto-analysis" in target_ids
    assert "auto-capability-gap" in target_ids
    assert "auto-entity-reference" in target_ids
    assert "auto-quality-issue" in target_ids
    assert "auto-repair" in target_ids
    # b29 test gets migrated (frontmatter name decides — no special-case)
    # → "auto-auto-b29-invoke-test"; user can rm that one if they want.
    # repair winner is v38, not v37.
    repair_text = (
        target / "auto-repair" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert '"v38"' in repair_text or "v38" in repair_text
    # entity_reference winner is v29, not v28 or v1.
    er_text = (
        target / "auto-entity-reference" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "v29" in er_text


def test_migrate_skips_shell_skills_referencing_index_js(tmp_path: Path) -> None:
    """B-178: SKILL.md bodies pointing at the deleted ``index.js`` Node
    project are auto_evo placeholders that fail at runtime — refuse to
    migrate them so re-running the tool doesn't keep recreating the
    noise the joint audit just deleted from the user's machine."""
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"

    # Shell body — references index.js, the classic auto_evo placeholder.
    _write_legacy_skill(
        src, "auto_repair_v38",
        frontmatter='name: repair\ndescription: "v38"\n',
        body=(
            "# 使用时机\n\n"
            "当系统检测到该模式时触发：error_feedback\n\n"
            "# 使用方法\n\n"
            "直接调用 repair 的主要函数，传入对应的上下文参数。"
            "具体函数取决于 index.js 中的导出。\n"
        ),
    )
    # Real body — should still migrate fine.
    _write_legacy_skill(
        src, "auto_real_v1",
        frontmatter='name: realwork\ndescription: "ok"\n',
        body=(
            "# Procedure\n\n"
            "Step 1: read the input. Step 2: do the work. Step 3: emit.\n"
        ),
    )
    results = migrate(src, target)
    target_ids = sorted(r.target_id for r in results if r.ok)
    assert target_ids == ["auto-realwork"]
    assert not (target / "auto-repair").exists()


def test_migrate_skips_b29_test_artifact(tmp_path: Path) -> None:
    """The B-29 test stub ('mention specialword/magicstring') is also a
    noise body even though it doesn't reference index.js."""
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "auto_b29_invoke_test",
        frontmatter='name: auto_b29_invoke_test\n',
        body=(
            "# auto_b29_invoke_test\n\n"
            "When the user mentions 'specialword' or 'magicstring', "
            "simply confirm receipt.\n"
        ),
    )
    results = migrate(src, target)
    assert results == []


def test_migrate_skips_short_body(tmp_path: Path) -> None:
    """A SKILL.md with a body that's effectively empty (< 30 chars
    after frontmatter) is also a shell — skip."""
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "auto_tiny",
        frontmatter='name: tiny\n',
        body="# tiny\n",  # 7 chars after stripping
    )
    results = migrate(src, target)
    assert results == []


def test_migrate_handles_already_auto_prefixed_name(tmp_path: Path) -> None:
    """Frontmatter ``name: auto_b29_invoke_test`` already starts with
    ``auto`` — mustn't double-prefix to ``auto-auto-...``.

    Actually we DO want it to become ``auto-auto-b29-invoke-test`` if
    the name itself is ``auto_b29_invoke_test`` because the prefix
    semantics is "this is an evolution-produced skill", not "the name
    starts with auto". Test that the existing single ``auto-`` prefix
    on the kebab'd name is detected and dedupe to one prefix."""
    src = tmp_path / "auto_evo" / "skills"
    target = tmp_path / "skills_user"
    _write_legacy_skill(
        src, "weird",
        frontmatter='name: auto-already-prefixed\ndescription: "x"\n',
    )
    results = migrate(src, target)
    # Single auto- prefix preserved, not doubled.
    assert results[0].target_id == "auto-already-prefixed"
    assert (target / "auto-already-prefixed" / "SKILL.md").is_file()
