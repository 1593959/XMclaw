"""Epic #27 P2 G-07 (2026-05-19) — version_history snapshot + rollback.

Pins:
  * snapshot() writes ``<dir>/.versions/<ts>.<ext>`` with the live
    content; idempotent against repeated-save-no-change.
  * list_versions() returns newest-first, filters by ext.
  * rollback() restores a snapshot in place + snapshots current first.
  * diff() returns a unified diff or empty string (identical).
  * MAX_VERSIONS pruning drops the oldest above the cap.
  * Snapshot is best-effort: IO errors swallow + return None.
  * skill_diff / skill_rollback meta-tools route correctly through
    SkillToolProvider, including the "no snapshots yet" base case.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    META_DIFF_TOOL_NAME,
    META_ROLLBACK_TOOL_NAME,
    SkillToolProvider,
)
from xmclaw.skills.version_history import (
    MAX_VERSIONS_PER_SKILL,
    VERSIONS_DIR_NAME,
    diff,
    list_versions,
    rollback,
    snapshot,
)


class _NoopSkill(Skill):
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result="ok", side_effects=[])


# ── snapshot() base behaviour ─────────────────────────────────────


def test_snapshot_writes_versions_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("v1 content", encoding="utf-8")

    out = snapshot(skill_dir, live)
    assert out is not None
    assert out.parent == skill_dir / VERSIONS_DIR_NAME
    assert out.suffix == ".md"
    assert out.read_text(encoding="utf-8") == "v1 content"


def test_snapshot_skips_when_identical_to_newest(tmp_path: Path) -> None:
    """Idempotent: snapshotting unchanged content twice produces a
    single .versions/ entry, not two."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("steady content", encoding="utf-8")

    first = snapshot(skill_dir, live)
    second = snapshot(skill_dir, live)
    assert first is not None
    assert second is None  # second was deduped
    assert len(list(first.parent.iterdir())) == 1


def test_snapshot_writes_when_content_differs(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("first", encoding="utf-8")
    s1 = snapshot(skill_dir, live)
    # Sleep so the timestamps separate cleanly (microsecond precision).
    time.sleep(0.002)
    live.write_text("second", encoding="utf-8")
    s2 = snapshot(skill_dir, live)
    assert s1 is not None and s2 is not None
    assert s1 != s2
    assert s2.read_text(encoding="utf-8") == "second"


def test_snapshot_returns_none_on_missing_file(tmp_path: Path) -> None:
    """Best-effort: missing file → None, no exception."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    out = snapshot(skill_dir, skill_dir / "does-not-exist.md")
    assert out is None


# ── list_versions() ───────────────────────────────────────────────


def test_list_versions_newest_first(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    for i, c in enumerate(["a", "b", "c"]):
        live.write_text(c, encoding="utf-8")
        snapshot(skill_dir, live)
        time.sleep(0.002)
    vs = list_versions(skill_dir, ext="md")
    assert len(vs) == 3
    assert vs[0].ts > vs[1].ts > vs[2].ts
    # And index field reflects newest-first ordering.
    assert [v.index for v in vs] == [0, 1, 2]


def test_list_versions_filters_by_extension(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    py = skill_dir / "skill.py"
    md.write_text("markdown", encoding="utf-8")
    py.write_text("python", encoding="utf-8")
    snapshot(skill_dir, md)
    time.sleep(0.002)
    snapshot(skill_dir, py)
    md_vs = list_versions(skill_dir, ext="md")
    py_vs = list_versions(skill_dir, ext="py")
    assert len(md_vs) == 1
    assert len(py_vs) == 1
    assert md_vs[0].path.suffix == ".md"
    assert py_vs[0].path.suffix == ".py"


def test_list_versions_empty_when_no_dir(tmp_path: Path) -> None:
    """Fresh skill, no edits yet: no ``.versions/`` dir → empty list."""
    skill_dir = tmp_path / "fresh"
    skill_dir.mkdir()
    assert list_versions(skill_dir) == []


# ── rollback() ────────────────────────────────────────────────────


def test_rollback_restores_snapshot(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("original", encoding="utf-8")
    snapshot(skill_dir, live)
    time.sleep(0.002)
    # Edit the live file.
    live.write_text("broken-new-content", encoding="utf-8")
    restored = rollback(skill_dir, live, to_index=0)
    assert restored is not None
    # Wait — index 0 is now the snapshot OF THE BROKEN CONTENT
    # because snapshot_current=True wrote one. Actually the
    # snapshots after rollback are: [pre-rollback-live, original].
    # The function picked to_index=0 BEFORE the pre-rollback
    # snapshot was written? Let me check the implementation order:
    # rollback() reads target FIRST, then writes the snapshot of
    # current, then writes live. So index 0 in the list_versions
    # at call time was the only existing snapshot ("original").
    assert live.read_text(encoding="utf-8") == "original"


def test_rollback_snapshots_current_for_undo(tmp_path: Path) -> None:
    """rollback() should snapshot the current live content BEFORE
    overwriting so the operator can roll the rollback back."""
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("v1", encoding="utf-8")
    snapshot(skill_dir, live)
    time.sleep(0.002)
    live.write_text("v2-broken", encoding="utf-8")
    # Pre-rollback there's one snapshot: "v1".
    assert len(list_versions(skill_dir, ext="md")) == 1

    rollback(skill_dir, live, to_index=0)

    # Post-rollback there should be TWO snapshots: the original "v1"
    # AND a fresh snapshot of "v2-broken" (the pre-rollback live).
    after = list_versions(skill_dir, ext="md")
    assert len(after) == 2
    bodies = {p.path.read_text(encoding="utf-8") for p in after}
    assert bodies == {"v1", "v2-broken"}


def test_rollback_returns_none_when_no_snapshot(tmp_path: Path) -> None:
    """Calling rollback on an unedited skill → None (nothing to restore)."""
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("only-one", encoding="utf-8")
    assert rollback(skill_dir, live, to_index=0) is None


def test_rollback_out_of_range_index(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("v1", encoding="utf-8")
    snapshot(skill_dir, live)
    assert rollback(skill_dir, live, to_index=99) is None


# ── diff() ────────────────────────────────────────────────────────


def test_diff_unified_format(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("line a\nline b\nline c\n", encoding="utf-8")
    snapshot(skill_dir, live)
    time.sleep(0.002)
    live.write_text("line a\nline B-modified\nline c\n", encoding="utf-8")
    d = diff(skill_dir, live, against_index=0)
    assert d is not None
    assert "-line b" in d
    assert "+line B-modified" in d


def test_diff_empty_when_identical(tmp_path: Path) -> None:
    """Same content → empty string (not None)."""
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("steady", encoding="utf-8")
    snapshot(skill_dir, live)
    assert diff(skill_dir, live, against_index=0) == ""


def test_diff_none_when_no_snapshot(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("only-one", encoding="utf-8")
    assert diff(skill_dir, live) is None


def test_diff_truncates_huge_output(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    live.write_text("a\n" * 500, encoding="utf-8")
    snapshot(skill_dir, live)
    time.sleep(0.002)
    live.write_text("b\n" * 500, encoding="utf-8")
    d = diff(skill_dir, live, against_index=0, max_lines=20)
    assert d is not None
    assert "diff truncated" in d


# ── pruning ───────────────────────────────────────────────────────


def test_snapshot_prunes_oldest_above_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When count exceeds MAX_VERSIONS_PER_SKILL the oldest fall
    off. Use a small cap so the test is fast."""
    monkeypatch.setattr(
        "xmclaw.skills.version_history.MAX_VERSIONS_PER_SKILL", 3,
    )
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    live = skill_dir / "SKILL.md"
    for i in range(6):
        live.write_text(f"v{i}", encoding="utf-8")
        snapshot(skill_dir, live)
        time.sleep(0.002)
    versions = list_versions(skill_dir, ext="md")
    assert len(versions) == 3
    # Newest-first; oldest were pruned.
    bodies = [v.path.read_text(encoding="utf-8") for v in versions]
    assert bodies == ["v5", "v4", "v3"]


# ── meta-tool routing (skill_diff / skill_rollback) ───────────────


def _registry_with(*manifests: SkillManifest) -> SkillRegistry:
    reg = SkillRegistry()
    for m in manifests:
        reg.register(_NoopSkill(m.id), m)
    return reg


def test_meta_tools_in_list_tools() -> None:
    """skill_diff + skill_rollback must surface alongside the other
    meta-tools so the prefilter's always-on whitelist applies."""
    bridge = SkillToolProvider(_registry_with())
    names = {s.name for s in bridge.list_tools()}
    assert META_DIFF_TOOL_NAME in names
    assert META_ROLLBACK_TOOL_NAME in names


def test_meta_tools_pass_prefilter() -> None:
    bridge = SkillToolProvider(_registry_with(
        *[SkillManifest(id=f"misc-{i}", version=1) for i in range(35)]
    ))
    survivors = select_relevant_skills(
        "天气", bridge.list_tools(), top_k=12,
    )
    names = {s.name for s in survivors}
    assert META_DIFF_TOOL_NAME in names
    assert META_ROLLBACK_TOOL_NAME in names


@pytest.mark.asyncio
async def test_skill_diff_missing_skill_id() -> None:
    bridge = SkillToolProvider(_registry_with())
    res = await bridge.invoke(ToolCall(
        name=META_DIFF_TOOL_NAME, args={}, provenance="synthetic",
    ))
    assert res.ok is False
    assert "skill_id" in (res.error or "")


@pytest.mark.asyncio
async def test_skill_rollback_missing_skill_id() -> None:
    bridge = SkillToolProvider(_registry_with())
    res = await bridge.invoke(ToolCall(
        name=META_ROLLBACK_TOOL_NAME, args={}, provenance="synthetic",
    ))
    assert res.ok is False
    assert "skill_id" in (res.error or "")
