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


# ── B-328: advisory permissions visibility ────────────────────────────


def test_b328_manifest_to_dict_includes_permissions_enforced() -> None:
    """B-328: ``permissions_enforced`` is shipped on every manifest so
    the Skills UI can render an "advisory" / "enforced" badge. Default
    is False — the current Local / Process runtimes can't enforce
    permissions; a Phase 3.5+ Docker / nsjail runtime would set this
    True on the manifests it can sandbox."""
    from xmclaw.skills.manifest import SkillManifest
    m = SkillManifest(
        id="x", version=1,
        permissions_subprocess=("git",),
    )
    d = m.to_dict()
    assert "permissions_enforced" in d
    assert d["permissions_enforced"] is False


def test_b328_permissions_are_meaningful_helper() -> None:
    """B-328: helper used by the loader to gate the advisory AST scan."""
    from xmclaw.skills.manifest import SkillManifest

    # Empty across the board → nothing meaningful to cross-check.
    m_empty = SkillManifest(id="x", version=1)
    assert m_empty.permissions_are_meaningful() is False

    # Any non-trivial permission claim → cross-check should run.
    assert SkillManifest(
        id="x", version=1, permissions_fs=("/tmp",),
    ).permissions_are_meaningful() is True
    assert SkillManifest(
        id="x", version=1, permissions_net=("api.example.com",),
    ).permissions_are_meaningful() is True
    assert SkillManifest(
        id="x", version=1, permissions_subprocess=("git",),
    ).permissions_are_meaningful() is True


def test_b341_permissions_enforced_alone_counts_as_meaningful() -> None:
    """B-341 (audit pass-2 #8): closing the gate gap. Pre-B-341 a
    manifest with all ``permissions_*`` empty AND
    ``permissions_enforced: true`` returned False from
    ``permissions_are_meaningful`` — even though the explicit
    ``enforced=true`` is the strongest possible "I mean these
    constraints" signal an operator can give. The advisory cross-
    check therefore never ran on those manifests, even when their
    code clearly violated the implied deny-all. Now ``enforced=true``
    alone flips the gate."""
    from xmclaw.skills.manifest import SkillManifest

    # All permissions_* empty BUT permissions_enforced opt-in →
    # treated as "operator engaged with the system" → meaningful.
    assert SkillManifest(
        id="x", version=1, permissions_enforced=True,
    ).permissions_are_meaningful() is True

    # And of course empty + not enforced still returns False (no
    # operator engagement at all → no cross-check noise).
    assert SkillManifest(
        id="x", version=1, permissions_enforced=False,
    ).permissions_are_meaningful() is False


_SKILL_USING_SUBPROCESS = """
import subprocess
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class SubSkill(Skill):
    id = "{skill_id}"
    version = {version}

    async def run(self, inp: SkillInput) -> SkillOutput:
        # Calls subprocess despite a manifest that may forbid it.
        subprocess.run(["echo", "hi"], check=False)
        return SkillOutput(ok=True, result={{}}, side_effects=[])
"""


def test_b328_advisory_warning_when_no_subprocess_claim_but_source_uses_it(
    tmp_path: Path, caplog,
) -> None:
    """B-328 core regression: a Python skill whose manifest declares
    ``permissions_subprocess: []`` (empty = "no subprocess allowed",
    the most natural author intent) but whose source actually calls
    ``subprocess.run`` must produce a WARNING at load time. Pre-B-328
    the discrepancy was silent: the SKILL.md ``permissions_subprocess: []``
    line travelled all the way to the Skills UI as if it were
    enforced, and operators reading it were misled."""
    import logging as _logging

    skill_id = "subproc-skill"
    # Manifest says non-trivial fs constraint (so the helper triggers
    # the cross-check) AND empty subprocess (claim: not allowed).
    _write_skill(
        tmp_path, skill_id, version=1,
        template=_SKILL_USING_SUBPROCESS,
        manifest={
            "id": skill_id, "version": 1,
            "permissions_fs": ["/tmp/safe"],
            "permissions_subprocess": [],
        },
    )
    reg = SkillRegistry()
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.skills.user_loader",
    ):
        results = UserSkillsLoader(reg, tmp_path).load_all()

    # Load itself MUST succeed — visibility, not behaviour change.
    assert len(results) == 1
    assert results[0].ok is True

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "permissions_advisory_violation" in m and skill_id in m
        for m in msgs
    ), f"expected advisory warning for {skill_id}; got: {msgs!r}"


def test_b341_warning_when_allowlist_set_but_runtime_cant_enforce(
    tmp_path: Path, caplog,
) -> None:
    """B-341 (audit pass-2 #8): when ``permissions_subprocess`` is a
    non-empty allowlist (e.g. ``["git"]``) AND source uses subprocess,
    the cross-check must STILL warn — because no current runtime
    enforces the allowlist (anti-req #5: sandbox is a future-runtime
    feature). Pre-B-341 the audit short-circuited unless the field
    was empty, so an operator who wrote ``permissions_subprocess:
    ["git"]`` and called ``os.system("rm -rf /")`` got zero feedback.
    The warning surfaces the gap with a distinct
    ``allowlist_advisory_only_no_runtime_enforcement`` note so
    operators understand the list is informational only."""
    import logging as _logging

    skill_id = "subproc-allowed"
    _write_skill(
        tmp_path, skill_id, version=1,
        template=_SKILL_USING_SUBPROCESS,
        manifest={
            "id": skill_id, "version": 1,
            "permissions_subprocess": ["echo", "git"],  # non-empty allowlist
        },
    )
    reg = SkillRegistry()
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.skills.user_loader",
    ):
        UserSkillsLoader(reg, tmp_path).load_all()

    msgs = [r.getMessage() for r in caplog.records]
    advisory = [m for m in msgs if "permissions_advisory_violation" in m]
    assert advisory, (
        f"expected advisory warning for non-empty allowlist + "
        f"subprocess use; got: {msgs!r}"
    )
    # Distinct flavour from the deny-all case so log readers know
    # which discrepancy class fired.
    assert any(
        "allowlist_advisory_only_no_runtime_enforcement" in m
        for m in advisory
    ), f"expected allowlist-flavour note; got: {advisory!r}"


def test_b328_no_warning_when_no_meaningful_permissions(
    tmp_path: Path, caplog,
) -> None:
    """A manifest without ``permissions_*`` declarations — i.e. the
    user didn't try to constrain anything — must not surface advisory
    warnings even if the source uses subprocess. Otherwise enabling
    the cross-check would spam every default skill."""
    import logging as _logging

    skill_id = "subproc-default"
    _write_skill(
        tmp_path, skill_id, version=1,
        template=_SKILL_USING_SUBPROCESS,
        # No manifest.json → loader synthesises one with all
        # permissions empty. helper.permissions_are_meaningful()
        # returns False → cross-check skipped.
    )
    reg = SkillRegistry()
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.skills.user_loader",
    ):
        UserSkillsLoader(reg, tmp_path).load_all()

    msgs = [r.getMessage() for r in caplog.records]
    assert not any(
        "permissions_advisory_violation" in m for m in msgs
    ), f"synthesised manifests should not trigger advisory: {msgs!r}"


# ── Epic #27 P1 G-09 (2026-05-19): realpath dedup + duplicate-id warn ──


@pytest.mark.asyncio
async def test_g09_symlinked_skill_dir_loads_once(tmp_path: Path) -> None:
    """If canonical points at the same realpath as an extra root via
    symlink, the skill should load ONCE, not twice. The pre-fix
    name-only dedup would have caught this, but realpath dedup is
    the proper structural fix."""
    import os

    real_root = tmp_path / "real_skills"
    real_root.mkdir()
    _write_skill(real_root, "shared-via-symlink")
    # Make extras root a symlink to the same dir (Windows requires
    # admin or developer mode; skip if symlink isn't supported).
    extras = tmp_path / "extras"
    try:
        os.symlink(real_root, extras, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this env")

    reg = SkillRegistry()
    results = UserSkillsLoader(
        reg, real_root, extra_roots=[extras],
    ).load_all()
    successes = [r for r in results if r.ok]
    # Exactly one successful load — symlinked twin is deduped via realpath.
    assert len(successes) == 1
    assert successes[0].skill_id == "shared-via-symlink"


@pytest.mark.asyncio
async def test_g09_same_id_different_paths_records_duplicate(
    tmp_path: Path,
) -> None:
    """Drop the same skill_id (different dir content) in both
    canonical and extra roots → canonical wins, BUT a duplicate
    row appears in results so the SkillsWatcher / UI / agent can
    surface the conflict."""
    canonical = tmp_path / "skills_user"
    extras = tmp_path / "agents_skills"
    canonical.mkdir()
    extras.mkdir()
    _write_skill(canonical, "conflict-id", version=1)
    _write_skill(extras, "conflict-id", version=1)

    reg = SkillRegistry()
    results = UserSkillsLoader(
        reg, canonical, extra_roots=[extras],
    ).load_all()
    # Canonical version registered.
    assert "conflict-id" in reg.list_skill_ids()
    # A duplicate row exists in results so SkillsWatcher can pick
    # it up + put in load_failures.
    dupes = [r for r in results if r.kind == "duplicate"]
    assert len(dupes) == 1
    assert dupes[0].skill_id == "conflict-id"
    assert not dupes[0].ok
    assert "multiple roots" in (dupes[0].error or "")


# ── Epic #27 P1 G-10 (2026-05-19): frontmatter extras ──────────────


def test_g10_extras_parser_handles_string_fields() -> None:
    from xmclaw.skills.user_loader import _parse_skill_md_frontmatter_extras
    body = (
        "---\n"
        "name: x\n"
        "when_to_use: This skill should be used when refactoring.\n"
        "model: opus\n"
        "---\n# body\n"
    )
    extras = _parse_skill_md_frontmatter_extras(body)
    assert extras["when_to_use"].startswith("This skill")
    assert extras["model"] == "opus"


def test_g10_extras_parser_handles_list_fields() -> None:
    from xmclaw.skills.user_loader import _parse_skill_md_frontmatter_extras
    body = (
        "---\n"
        "allowed_tools: [file_read, bash, web_fetch]\n"
        "paths: [src/**/*.py, tests/**/*.py]\n"
        "---\n"
    )
    extras = _parse_skill_md_frontmatter_extras(body)
    assert extras["allowed_tools"] == ("file_read", "bash", "web_fetch")
    assert extras["paths"] == ("src/**/*.py", "tests/**/*.py")


def test_g10_extras_parser_accepts_hyphenated_keys() -> None:
    """Claude Code uses ``allowed-tools``, Hermes uses ``allowedTools``;
    XMclaw also takes the snake_case form. All three should parse."""
    from xmclaw.skills.user_loader import _parse_skill_md_frontmatter_extras
    body = (
        "---\n"
        "allowed-tools: [a, b]\n"
        "when-to-use: pick me on bug fixes\n"
        "requires-restart: true\n"
        "---\n"
    )
    extras = _parse_skill_md_frontmatter_extras(body)
    assert extras["allowed_tools"] == ("a", "b")
    assert extras["when_to_use"] == "pick me on bug fixes"
    assert extras["requires_restart"] is True


def test_g10_extras_parser_defaults_when_empty() -> None:
    from xmclaw.skills.user_loader import _parse_skill_md_frontmatter_extras
    extras = _parse_skill_md_frontmatter_extras("# no frontmatter\n")
    assert extras == {
        "when_to_use": "",
        "allowed_tools": (),
        "paths": (),
        "requires_restart": False,
        "model": "",
    }


@pytest.mark.asyncio
async def test_g10_markdown_skill_carries_extras_into_manifest(
    tmp_path: Path,
) -> None:
    """End-to-end: drop a SKILL.md with G-10 frontmatter → loader
    surfaces all fields in the registered manifest."""
    sd = tmp_path / "with-extras"
    sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: with-extras\n"
        "description: demo skill for G-10\n"
        "when_to_use: This skill should be used when X happens.\n"
        "allowed_tools: [bash, file_read]\n"
        "paths: [src/**]\n"
        "requires_restart: false\n"
        "model: sonnet\n"
        "---\n"
        "# body\nsteps\n",
        encoding="utf-8",
    )
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()
    assert "with-extras" in reg.list_skill_ids()
    ref = reg.ref("with-extras", 1)
    m = ref.manifest
    assert m.when_to_use.startswith("This skill")
    assert m.allowed_tools == ("bash", "file_read")
    assert m.paths == ("src/**",)
    assert m.model == "sonnet"
    assert m.requires_restart is False


@pytest.mark.asyncio
async def test_g09_duplicate_row_surfaces_in_watcher_failures(
    tmp_path: Path,
) -> None:
    """End-to-end: SkillsWatcher's load_failures() exposes the
    duplicate row so the Skills page banner + skill_status tool
    pick it up alongside genuine load failures."""
    from xmclaw.daemon.skills_watcher import SkillsWatcher

    canonical = tmp_path / "skills_user"
    extras = tmp_path / "agents_skills"
    canonical.mkdir()
    extras.mkdir()
    _write_skill(canonical, "dup-end-to-end")
    _write_skill(extras, "dup-end-to-end")

    reg = SkillRegistry()
    watcher = SkillsWatcher(
        reg, canonical, extra_roots=[extras], interval_s=3600.0,
    )
    await watcher.tick()
    failures = watcher.load_failures()
    assert len(failures) == 1
    assert failures[0]["skill_id"] == "dup-end-to-end"
    assert failures[0]["kind"] == "duplicate"
