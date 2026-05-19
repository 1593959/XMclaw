"""B-173 — SkillsWatcher unit tests.

Pins:
  * On tick, newly-dropped SKILL.md under skills_root → registered live.
  * On tick, newly-dropped SKILL.md under an extra_root → registered.
  * Already-registered skills are idempotent (no double-register, no
    crash, ``new_skill_count`` unchanged).
  * ``start`` / ``stop`` are idempotent.
  * ``stop`` cancels the running task within seconds.
  * ``enabled=False`` makes ``start`` a no-op.
  * ``resolve_skill_roots`` honours empty ``evolution.skill_paths.extra``
    explicitly (for users who want to opt out of shared-dir scanning).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from xmclaw.daemon.skills_watcher import SkillsWatcher
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import resolve_skill_roots


def _drop_skill_md(
    root: Path, skill_id: str, *, body: str = "# stub\nbody\n",
) -> Path:
    sd = root / skill_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        "---\nname: " + skill_id + "\ndescription: 'auto'\n---\n\n" + body,
        encoding="utf-8",
    )
    return sd


# ── happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_registers_new_skill_md(tmp_path: Path) -> None:
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    # Drop a skill AFTER watcher was created — first tick must
    # register it.
    _drop_skill_md(canonical, "fresh-install")

    new_count = await watcher.tick()
    assert new_count == 1
    assert "fresh-install" in reg.list_skill_ids()
    assert watcher.new_skill_count == 1
    assert watcher.tick_count == 1


@pytest.mark.asyncio
async def test_tick_registers_new_skill_in_extra_root(tmp_path: Path) -> None:
    """``npx skills add`` writes to ~/.agents/skills/<id>/. The watcher
    must find skills there too, not just the canonical root."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    extra = tmp_path / "agents_skills"
    extra.mkdir()
    watcher = SkillsWatcher(
        reg, canonical, extra_roots=[extra], interval_s=3600.0,
    )

    _drop_skill_md(extra, "from-shared-dir")

    await watcher.tick()
    assert "from-shared-dir" in reg.list_skill_ids()


@pytest.mark.asyncio
async def test_idempotent_re_tick_no_double_register(tmp_path: Path) -> None:
    """A second tick with no new files → 0 new skills, no errors,
    counters track correctly."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    _drop_skill_md(canonical, "stable-skill")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.tick()
    assert watcher.new_skill_count == 1

    # Second tick: nothing new on disk.
    new_count = await watcher.tick()
    assert new_count == 0
    assert watcher.new_skill_count == 1  # unchanged
    assert watcher.tick_count == 2


@pytest.mark.asyncio
async def test_tick_finds_files_added_between_ticks(tmp_path: Path) -> None:
    """Drop tick 1: 1 skill. Tick 2: still 1. Drop another. Tick 3: 2 total."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    _drop_skill_md(canonical, "first")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    assert await watcher.tick() == 1
    assert await watcher.tick() == 0  # idempotent

    _drop_skill_md(canonical, "second")
    assert await watcher.tick() == 1
    assert reg.list_skill_ids() == ["first", "second"]


# ── lifecycle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_start_no_op(tmp_path: Path) -> None:
    reg = SkillRegistry()
    watcher = SkillsWatcher(
        reg, tmp_path, interval_s=3600.0, enabled=False,
    )
    await watcher.start()
    assert not watcher.is_running()
    await watcher.stop()  # also no-op


@pytest.mark.asyncio
async def test_start_stop_idempotent(tmp_path: Path) -> None:
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.start()
    await watcher.start()  # second call no-op
    assert watcher.is_running()

    await watcher.stop()
    await watcher.stop()  # second call no-op
    assert not watcher.is_running()


@pytest.mark.asyncio
async def test_stop_cancels_long_interval(tmp_path: Path) -> None:
    """interval_s=3600 but stop() returns within seconds (mirrors
    SkillDreamCycle's same-shape contract)."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.start()
    await asyncio.sleep(0.01)  # let task enter wait_for
    t0 = asyncio.get_running_loop().time()
    await watcher.stop()
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 1.0
    assert not watcher.is_running()


@pytest.mark.asyncio
async def test_loop_runs_at_least_one_tick(tmp_path: Path) -> None:
    """Smoke: with a small interval the loop fires _tick at least once
    before we stop it, and a fresh skill drop is picked up by the
    background tick (not just by manually-called tick())."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    watcher = SkillsWatcher(reg, canonical, interval_s=0.05)

    await watcher.start()
    _drop_skill_md(canonical, "auto-found")
    # Wait for the loop to tick.
    for _ in range(40):
        if "auto-found" in reg.list_skill_ids():
            break
        await asyncio.sleep(0.05)
    await watcher.stop()
    assert "auto-found" in reg.list_skill_ids()
    assert watcher.tick_count >= 1


# ── resolve_skill_roots helper ────────────────────────────────────


def test_resolve_skill_roots_default_includes_shared() -> None:
    """B-234 dropped ``~/.claude/skills`` from the default — that's
    Claude Code's user-level config space, not XMclaw's. Default extras
    is now just ``~/.agents/skills`` (the open agent-skills marketplace
    where ``npx skills add`` writes). Users who want to share skills
    with Claude Code can opt in via ``evolution.skill_paths.extra``."""
    canonical, extras = resolve_skill_roots(None)
    extra_strs = [str(p) for p in extras]
    assert any(".agents" in s and "skills" in s for s in extra_strs)
    # B-234: ``.claude/skills`` is NOT in the default any more.
    assert not any(".claude" in s and "skills" in s for s in extra_strs)


def test_resolve_skill_roots_explicit_empty_disables_shared() -> None:
    """User explicitly opts out of shared-dir scan."""
    cfg = {"evolution": {"skill_paths": {"extra": []}}}
    _, extras = resolve_skill_roots(cfg)
    assert extras == []


def test_resolve_skill_roots_explicit_extras_used() -> None:
    cfg = {"evolution": {"skill_paths": {"extra": ["~/my/skills"]}}}
    _, extras = resolve_skill_roots(cfg)
    assert len(extras) == 1
    assert "my" in str(extras[0]) and "skills" in str(extras[0])


# ── B-175: edit existing SKILL.md propagates without restart ─────


def _touch_with_mtime(path: Path, mtime: float) -> None:
    """Force a file's mtime so we don't depend on filesystem
    sub-second resolution (CI on FAT/ exFAT can round to 2s)."""
    os.utime(path, (mtime, mtime))


@pytest.mark.asyncio
async def test_edit_skill_md_propagates_to_registry(tmp_path: Path) -> None:
    """Drop a skill, tick to register, edit body, tick again →
    registry.get(id).body shows the new content (no restart)."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_skill_md(canonical, "git-commit", body="OLD body\n")
    _touch_with_mtime(skill_path / "SKILL.md", 1000.0)
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    # Tick 1: register + seed mtime cache.
    await watcher.tick()
    assert reg.get("git-commit").body.strip().endswith("OLD body")

    # User edits SKILL.md — write new content + bump mtime.
    new_text = (
        "---\nname: git-commit\ndescription: 'updated desc'\n---\n\nNEW body\n"
    )
    (skill_path / "SKILL.md").write_text(new_text, encoding="utf-8")
    _touch_with_mtime(skill_path / "SKILL.md", 2000.0)

    # Tick 2: detect mtime change, refresh body.
    await watcher.tick()
    assert "NEW body" in reg.get("git-commit").body
    assert "OLD body" not in reg.get("git-commit").body
    # Manifest description also refreshed.
    assert reg.ref("git-commit").manifest.description == "updated desc"
    assert watcher.updated_body_count == 1


@pytest.mark.asyncio
async def test_edit_unchanged_file_no_update(tmp_path: Path) -> None:
    """Tick on an unchanged tree must NOT report fake updates."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_skill_md(canonical, "stable", body="body\n")
    _touch_with_mtime(skill_path / "SKILL.md", 1000.0)
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.tick()  # register + seed
    await watcher.tick()  # nothing changed
    await watcher.tick()
    assert watcher.updated_body_count == 0


@pytest.mark.asyncio
async def test_edit_versions_file_propagates(tmp_path: Path) -> None:
    """Mutator-archived ``versions/v2.md`` body edits should also flow
    through (lets a manual reviewer tweak a v2 candidate without
    restarting)."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_skill_md(canonical, "auto-foo", body="v1 body\n")
    versions = skill_path / "versions"
    versions.mkdir()
    v2_file = versions / "v2.md"
    v2_file.write_text(
        "---\nname: auto-foo\n---\n\nv2 OLD\n", encoding="utf-8",
    )
    _touch_with_mtime(skill_path / "SKILL.md", 1000.0)
    _touch_with_mtime(v2_file, 1000.0)
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    await watcher.tick()
    assert reg.list_versions("auto-foo") == [1, 2]
    assert "v2 OLD" in reg.get("auto-foo", 2).body

    # Edit v2.md.
    v2_file.write_text(
        "---\nname: auto-foo\n---\n\nv2 NEW\n", encoding="utf-8",
    )
    _touch_with_mtime(v2_file, 2000.0)
    await watcher.tick()
    assert "v2 NEW" in reg.get("auto-foo", 2).body
    # v1 untouched.
    assert "v1 body" in reg.get("auto-foo", 1).body


@pytest.mark.asyncio
async def test_first_observation_does_not_count_as_update(
    tmp_path: Path,
) -> None:
    """A skill that registered THIS tick must not also count as an
    update — that would double-count + log noise."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    _drop_skill_md(canonical, "fresh", body="body\n")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    await watcher.tick()
    assert watcher.new_skill_count == 1
    assert watcher.updated_body_count == 0


@pytest.mark.asyncio
async def test_update_failure_does_not_crash_tick(tmp_path: Path) -> None:
    """An mtime change on a file we somehow can't read must be a
    silent no-op, not a tick crash."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_skill_md(canonical, "fragile", body="body\n")
    _touch_with_mtime(skill_path / "SKILL.md", 1000.0)
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    await watcher.tick()

    # Patch update_body to raise.
    orig = reg.update_body
    def _boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("simulated registry corruption")
    reg.update_body = _boom  # type: ignore[assignment]
    try:
        (skill_path / "SKILL.md").write_text(
            "NEW body\n", encoding="utf-8",
        )
        _touch_with_mtime(skill_path / "SKILL.md", 2000.0)
        # Must not raise.
        await watcher.tick()
        assert watcher.updated_body_count == 0
    finally:
        reg.update_body = orig  # type: ignore[assignment]


# ── B-333 (audit #19): SKILL_UPDATE_REQUIRES_RESTART for skill.py ──


_PY_SKILL_TEMPLATE = '''
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class _S(Skill):
    id = "{skill_id}"
    version = 1
    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result={{"v": "{tag}"}}, side_effects=[])
'''


def _drop_python_skill(root: Path, skill_id: str, *, tag: str = "v0") -> Path:
    sd = root / skill_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id=skill_id, tag=tag),
        encoding="utf-8",
    )
    return sd


@pytest.mark.asyncio
async def test_b333_python_skill_edit_publishes_restart_event(
    tmp_path: Path,
) -> None:
    """B-333 (audit #19): editing a Python skill's source must
    produce a SKILL_UPDATE_REQUIRES_RESTART bus event so the UI can
    render a "restart needed" banner. Pre-B-333 the watcher silently
    no-op'd on skill.py and operators had no signal that their edit
    wouldn't take effect until restart.
    """
    from xmclaw.core.bus import InProcessEventBus, EventType
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_python_skill(canonical, "py-skill", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()
    assert "py-skill" in reg.list_skill_ids()

    bus = InProcessEventBus()
    captured: list = []
    bus.subscribe(
        lambda e: e.type == EventType.SKILL_UPDATE_REQUIRES_RESTART,
        lambda e: captured.append(e),
    )

    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0, bus=bus)
    # Seed mtime cache (first tick is a no-op for unchanged files —
    # by design, see _maybe_update_body docstring).
    _touch_with_mtime(skill_path / "skill.py", 1000.0)
    await watcher.tick()
    await bus.drain()
    assert captured == [], "first tick must seed without firing"

    # Now actually edit the file + bump mtime.
    (skill_path / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id="py-skill", tag="v1"),
        encoding="utf-8",
    )
    _touch_with_mtime(skill_path / "skill.py", 2000.0)
    await watcher.tick()
    await bus.drain()

    assert len(captured) == 1, (
        f"expected exactly one restart-required event, got "
        f"{len(captured)}: {[e.payload for e in captured]}"
    )
    payload = captured[0].payload
    assert payload["skill_id"] == "py-skill"
    assert payload["version"] == 1
    assert "skill.py" in payload["path"]


@pytest.mark.asyncio
async def test_b333_python_skill_restart_announced_only_once(
    tmp_path: Path,
) -> None:
    """Multiple edits to the same skill.py within one daemon
    lifetime should produce ONE event (not N) — otherwise the UI
    banner would re-fire and the operator gets toast-spam during
    iterative dev. The seen-set resets on daemon restart, which is
    the correct semantic (a restart picks up the change so the
    warning becomes irrelevant).
    """
    from xmclaw.core.bus import InProcessEventBus, EventType
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_python_skill(canonical, "py-skill", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()

    bus = InProcessEventBus()
    captured: list = []
    bus.subscribe(
        lambda e: e.type == EventType.SKILL_UPDATE_REQUIRES_RESTART,
        lambda e: captured.append(e),
    )
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0, bus=bus)
    _touch_with_mtime(skill_path / "skill.py", 1000.0)
    await watcher.tick()
    await bus.drain()

    # Edit twice in succession.
    for i, (tag, mtime) in enumerate([("v1", 2000.0), ("v2", 3000.0)]):
        (skill_path / "skill.py").write_text(
            _PY_SKILL_TEMPLATE.format(skill_id="py-skill", tag=tag),
            encoding="utf-8",
        )
        _touch_with_mtime(skill_path / "skill.py", mtime)
        await watcher.tick()
        await bus.drain()

    # Still ONE event despite two edits.
    assert len(captured) == 1, (
        f"expected one event across two edits, got {len(captured)}"
    )


@pytest.mark.asyncio
async def test_b333_no_bus_no_crash(tmp_path: Path) -> None:
    """Backward compat: building SkillsWatcher without ``bus=`` still
    works (tests / echo-mode). Edit-detection still happens, just no
    event is published."""
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_python_skill(canonical, "py-skill", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()

    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)  # no bus
    _touch_with_mtime(skill_path / "skill.py", 1000.0)
    await watcher.tick()

    (skill_path / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id="py-skill", tag="v1"),
        encoding="utf-8",
    )
    _touch_with_mtime(skill_path / "skill.py", 2000.0)
    # Must not raise.
    await watcher.tick()


# ── B-341 (audit pass-2 #6): pending_restarts() public surface ────


@pytest.mark.asyncio
async def test_b341_pending_restarts_starts_empty(tmp_path: Path) -> None:
    """Fresh watcher → empty list. Mirrors the daemon-restart
    semantic: a restart picks up the change so the warning becomes
    irrelevant, and ``pending_restarts()`` correctly reflects "no
    edits pending"."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    assert watcher.pending_restarts() == []


@pytest.mark.asyncio
async def test_b341_pending_restarts_lists_announced_edits(
    tmp_path: Path,
) -> None:
    """After a Python skill edit, ``pending_restarts()`` returns
    ``[{skill_id, version, path}]``. Pre-B-341 the watcher emitted
    SKILL_UPDATE_REQUIRES_RESTART to the bus but exposed no public
    surface for the Skills page banner — the event had zero
    subscribers and operators saw no warning.
    """
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_python_skill(canonical, "py-skill", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()

    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    _touch_with_mtime(skill_path / "skill.py", 1000.0)
    await watcher.tick()
    assert watcher.pending_restarts() == [], "seed tick → no entries"

    # Edit + bump mtime → one announced entry.
    (skill_path / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id="py-skill", tag="v1"),
        encoding="utf-8",
    )
    _touch_with_mtime(skill_path / "skill.py", 2000.0)
    await watcher.tick()

    pending = watcher.pending_restarts()
    assert len(pending) == 1
    entry = pending[0]
    assert entry["skill_id"] == "py-skill"
    assert entry["version"] == 1
    assert "skill.py" in entry["path"]


@pytest.mark.asyncio
async def test_b341_pending_restarts_one_per_skill_lifetime(
    tmp_path: Path,
) -> None:
    """Two edits to the same skill.py in one daemon-lifetime should
    produce one banner entry, not two — same dedup posture as the
    bus-event de-dup. Operators should not see two banner items
    when iterating fast."""
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    skill_path = _drop_python_skill(canonical, "py-skill", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()

    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    _touch_with_mtime(skill_path / "skill.py", 1000.0)
    await watcher.tick()

    for tag, mtime in [("v1", 2000.0), ("v2", 3000.0)]:
        (skill_path / "skill.py").write_text(
            _PY_SKILL_TEMPLATE.format(skill_id="py-skill", tag=tag),
            encoding="utf-8",
        )
        _touch_with_mtime(skill_path / "skill.py", mtime)
        await watcher.tick()

    pending = watcher.pending_restarts()
    assert len(pending) == 1, (
        f"expected one entry across two edits, got {len(pending)}: "
        f"{pending}"
    )


# ── Epic #27 P0 G-02 (2026-05-19): load_failures tracking ──────────


def _drop_broken_python_skill(root: Path, skill_id: str) -> Path:
    """Drop a skill.py with NO Skill subclass — UserSkillsLoader will
    refuse to register it. Used to seed a load failure."""
    sd = root / skill_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "skill.py").write_text(
        "# Intentionally empty — no Skill subclass.\n"
        "WHATEVER = 42\n",
        encoding="utf-8",
    )
    return sd


@pytest.mark.asyncio
async def test_load_failures_starts_empty(tmp_path: Path) -> None:
    """Fresh watcher with no broken skills → zero load failures."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    _drop_skill_md(canonical, "fine-skill")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    await watcher.tick()
    assert watcher.load_failures() == []


@pytest.mark.asyncio
async def test_load_failures_captures_broken_python_skill(
    tmp_path: Path,
) -> None:
    """A skill.py without a Skill subclass is the exact failure mode
    that bit the user with hyperframes 2026-05-19. The watcher must
    surface it so the agent / UI can see WHY skill_browse can't find it."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    _drop_broken_python_skill(canonical, "broken-skill")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.tick()

    failures = watcher.load_failures()
    assert len(failures) == 1
    row = failures[0]
    assert row["skill_id"] == "broken-skill"
    assert row["kind"] == "python"
    assert "no concrete Skill subclass" in row["error"].lower() or \
        "skill" in row["error"].lower()
    assert row["ticks_failing"] == 1
    assert row["first_seen"] == row["last_seen"]  # first tick

    # Second tick: same failure → ticks_failing bumps, first_seen stays.
    await watcher.tick()
    failures2 = watcher.load_failures()
    assert len(failures2) == 1
    assert failures2[0]["ticks_failing"] == 2
    assert failures2[0]["first_seen"] == row["first_seen"]
    assert failures2[0]["last_seen"] >= row["last_seen"]


@pytest.mark.asyncio
async def test_load_failures_clears_when_skill_recovers(
    tmp_path: Path,
) -> None:
    """The user fixes their skill.py → next tick clears the failure
    row. This is the path back to green from a broken state."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    sd = _drop_broken_python_skill(canonical, "recoverable")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.tick()
    assert len(watcher.load_failures()) == 1

    # Fix the skill by replacing skill.py with a SKILL.md instead
    # (avoids importlib caching of the broken module — SKILL.md is
    # the simplest recovery path).
    (sd / "skill.py").unlink()
    (sd / "SKILL.md").write_text(
        "---\nname: recoverable\ndescription: now valid\n---\n# ok\n",
        encoding="utf-8",
    )
    await watcher.tick()
    assert watcher.load_failures() == []
    assert "recoverable" in reg.list_skill_ids()


@pytest.mark.asyncio
async def test_load_failures_gc_when_skill_dir_deleted(
    tmp_path: Path,
) -> None:
    """User deletes the broken skill dir entirely → failure row drops
    out (no point complaining about a thing that no longer exists)."""
    import shutil

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    sd = _drop_broken_python_skill(canonical, "ghost-skill")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    await watcher.tick()
    assert len(watcher.load_failures()) == 1

    shutil.rmtree(sd)
    await watcher.tick()
    assert watcher.load_failures() == []


# ── Epic #27 P0 G-03 (2026-05-19): requires_restart for fixed-after-failure ─


@pytest.mark.asyncio
async def test_g03_skill_py_fix_after_failure_announces_restart(
    tmp_path: Path,
) -> None:
    """The hyperframes case: user wrote broken skill.py → daemon
    failed to load → user fixes it → daemon STILL has the cached
    failure (importlib stale-cache or in-memory registry void).
    The watcher must surface a requires_restart event with state=
    'fixed_after_failure' so the agent / UI prompts a reload."""
    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    sd = _drop_broken_python_skill(canonical, "hyper-like")
    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)

    # Tick 1: skill.py is broken → load_failures has it. seeds mtime.
    _touch_with_mtime(sd / "skill.py", 1000.0)
    await watcher.tick()
    assert len(watcher.load_failures()) == 1
    assert watcher.pending_restarts() == []  # mtime just seeded, no fire yet

    # User fixes the file — write a valid Skill subclass.
    (sd / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id="hyper-like", tag="fixed"),
        encoding="utf-8",
    )
    _touch_with_mtime(sd / "skill.py", 2000.0)

    # Tick 2: mtime change detected. Since the skill was in
    # load_failures, the announce payload tags state="fixed_after_failure".
    await watcher.tick()
    pending = watcher.pending_restarts()
    assert len(pending) == 1
    assert pending[0]["skill_id"] == "hyper-like"
    assert pending[0]["state"] == "fixed_after_failure"


@pytest.mark.asyncio
async def test_g03_skill_py_edit_of_working_skill_announces_edited(
    tmp_path: Path,
) -> None:
    """Already-registered, working skill.py gets edited → state='edited'
    (not 'fixed_after_failure'). Distinguishes the two ways the
    user might reach the same "you have to restart" outcome."""
    from xmclaw.skills.user_loader import UserSkillsLoader

    reg = SkillRegistry()
    canonical = tmp_path / "skills_user"
    canonical.mkdir()
    sd = _drop_python_skill(canonical, "stable-py", tag="v0")
    UserSkillsLoader(reg, canonical).load_all()
    assert "stable-py" in reg.list_skill_ids()

    watcher = SkillsWatcher(reg, canonical, interval_s=3600.0)
    _touch_with_mtime(sd / "skill.py", 1000.0)
    await watcher.tick()  # seed mtime

    (sd / "skill.py").write_text(
        _PY_SKILL_TEMPLATE.format(skill_id="stable-py", tag="v1"),
        encoding="utf-8",
    )
    _touch_with_mtime(sd / "skill.py", 2000.0)
    await watcher.tick()

    pending = watcher.pending_restarts()
    assert len(pending) == 1
    assert pending[0]["state"] == "edited"
    assert pending[0]["registered"] is True
