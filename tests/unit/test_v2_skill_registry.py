"""SkillRegistry — unit tests.

Anti-req #5 proven here:
  * multiple versions coexist; HEAD is explicit
  * rollback is a first-class, logged event
  * history is append-only — rollbacks don't erase prior promotions

Anti-req #12 proven here:
  * promote() refuses empty ``evidence`` (ValueError)
  * every promote/rollback writes a record that an auditor can read
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import (
    SkillRef,
    SkillRegistry,
    UnknownSkillError,
)


# ── test fixtures ─────────────────────────────────────────────────────────


class _NoopSkill(Skill):
    """Minimal Skill that records its version in run output."""

    def __init__(self, skill_id: str, version: int, marker: str = "") -> None:
        self.id = skill_id
        self.version = version
        self._marker = marker

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"v": self.version, "m": self._marker},
                           side_effects=[])


def _skill(id_: str, v: int, m: str = "") -> _NoopSkill:
    return _NoopSkill(id_, v, m)


def _manifest(id_: str, v: int, *, created_by: str = "human") -> SkillManifest:
    return SkillManifest(id=id_, version=v, created_by=created_by)


# ── register ──────────────────────────────────────────────────────────────


def test_register_returns_ref() -> None:
    reg = SkillRegistry()
    ref = reg.register(_skill("s", 1), _manifest("s", 1))
    assert isinstance(ref, SkillRef)
    assert ref.skill_id == "s"
    assert ref.version == 1
    assert ref.manifest.id == "s"


def test_register_sets_head_on_first_registration() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    assert reg.active_version("s") == 1


def test_register_second_version_does_NOT_move_head() -> None:
    """Anti-req #5: HEAD moves only via explicit promote()."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    # HEAD still points at v1 — registering a newer version does NOT
    # automatically promote it.
    assert reg.active_version("s") == 1


def test_register_same_version_twice_raises() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    with pytest.raises(ValueError):
        reg.register(_skill("s", 1), _manifest("s", 1))


def test_register_id_mismatch_raises() -> None:
    reg = SkillRegistry()
    with pytest.raises(ValueError):
        reg.register(_skill("s", 1), _manifest("DIFFERENT", 1))


def test_register_version_mismatch_raises() -> None:
    reg = SkillRegistry()
    with pytest.raises(ValueError):
        reg.register(_skill("s", 1), _manifest("s", 99))


def test_list_versions() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 3), _manifest("s", 3))
    reg.register(_skill("s", 2), _manifest("s", 2))
    # Registered out of order — list should be sorted ascending
    assert reg.list_versions("s") == [1, 2, 3]


def test_list_skill_ids() -> None:
    reg = SkillRegistry()
    reg.register(_skill("b", 1), _manifest("b", 1))
    reg.register(_skill("a", 1), _manifest("a", 1))
    assert reg.list_skill_ids() == ["a", "b"]


# ── get ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_default_returns_head() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1, "first"), _manifest("s", 1))
    reg.register(_skill("s", 2, "second"), _manifest("s", 2))
    # HEAD is still v1 (no promote yet); get() follows HEAD.
    got = reg.get("s")
    out = await got.run(SkillInput(args={}))
    assert out.result == {"v": 1, "m": "first"}


@pytest.mark.asyncio
async def test_get_specific_version() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2, "target"), _manifest("s", 2))
    got = reg.get("s", version=2)
    out = await got.run(SkillInput(args={}))
    assert out.result["v"] == 2


def test_get_unregistered_raises() -> None:
    reg = SkillRegistry()
    with pytest.raises(UnknownSkillError):
        reg.get("nope")


def test_get_unregistered_version_raises() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    with pytest.raises(UnknownSkillError):
        reg.get("s", version=99)


# ── promote (anti-req #12) ────────────────────────────────────────────────


def test_promote_moves_head() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    record = reg.promote("s", 2, evidence=["grader avg 0.85 over 20 runs"])
    assert reg.active_version("s") == 2
    assert record.kind == "promote"
    assert record.from_version == 1
    assert record.to_version == 2


def test_promote_refuses_empty_evidence() -> None:
    """Anti-req #12: no evidence, no promotion."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    with pytest.raises(ValueError, match="anti-req #12"):
        reg.promote("s", 2, evidence=[])
    # HEAD unchanged
    assert reg.active_version("s") == 1


def test_promote_to_unknown_version_raises() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    with pytest.raises(UnknownSkillError):
        reg.promote("s", 99, evidence=["x"])


def test_promote_preserves_evidence_in_record() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    record = reg.promote("s", 2, evidence=["e1", "e2", "e3"])
    assert record.evidence == ("e1", "e2", "e3")


# ── rollback ─────────────────────────────────────────────────────────────


def test_rollback_moves_head_back() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["e"])
    record = reg.rollback("s", 1, reason="v2 regressed on domain quality")
    assert reg.active_version("s") == 1
    assert record.kind == "rollback"
    assert record.from_version == 2
    assert record.to_version == 1


def test_rollback_preserves_both_versions() -> None:
    """After rollback, both v1 and v2 are still registered and fetchable."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["e"])
    reg.rollback("s", 1, reason="regression")
    # HEAD is 1; but v2 is still fetchable by explicit version
    assert reg.get("s").version == 1
    assert reg.get("s", version=2).version == 2


def test_rollback_without_reason_refused() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["e"])
    with pytest.raises(ValueError, match="reason"):
        reg.rollback("s", 1, reason="")


def test_rollback_to_unknown_version_raises() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    with pytest.raises(UnknownSkillError):
        reg.rollback("s", 99, reason="x")


# ── history ──────────────────────────────────────────────────────────────


def test_history_is_append_only() -> None:
    """Rollback does NOT erase the promotion record."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["prom"])
    reg.rollback("s", 1, reason="rb")
    reg.promote("s", 2, evidence=["re-prom"])
    hist = reg.history("s")
    kinds = [r.kind for r in hist]
    assert kinds == ["promote", "rollback", "promote"]


def test_history_empty_for_unpromoted_skill() -> None:
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    assert reg.history("s") == []


# ── persistence ──────────────────────────────────────────────────────────


def test_persistence_writes_jsonl_per_skill(tmp_path: Path) -> None:
    reg = SkillRegistry(history_dir=tmp_path)
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["bench.ratio=1.12"])
    reg.rollback("s", 1, reason="flaky on weekend traffic")

    log = tmp_path / "s.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import json
    promote_rec = json.loads(lines[0])
    rollback_rec = json.loads(lines[1])
    assert promote_rec["kind"] == "promote"
    assert promote_rec["evidence"] == ["bench.ratio=1.12"]
    assert rollback_rec["kind"] == "rollback"
    assert rollback_rec["reason"] == "flaky on weekend traffic"


# ── B-121: source tag (manual / controller / system) ──────────────────


def test_promote_defaults_source_to_manual() -> None:
    """Direct promote() calls without an explicit source default to
    'manual' — explicit calls are treated as human-driven."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    rec = reg.promote("s", 2, evidence=["bench.ratio=1.12"])
    assert rec.source == "manual"


def test_promote_records_controller_source_when_passed() -> None:
    """Auto-evolution path tags records with source='controller'."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    rec = reg.promote(
        "s", 2,
        evidence=["candidate=s mean=0.78 plays=20"],
        source="controller",
    )
    assert rec.source == "controller"


def test_rollback_records_source_field() -> None:
    """Rollback also carries source — auto-rollbacks via the controller
    path are distinguishable from human emergency rollbacks."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["e"])
    rec = reg.rollback("s", 1, reason="head regressed", source="controller")
    assert rec.source == "controller"

    rec2 = reg.rollback("s", 1, reason="user clicked the button")
    # ^ default — manual override after auto-rollback already moved HEAD
    # to v1; second rollback to v1 still records as a manual entry.
    assert rec2.source == "manual"


def test_persisted_record_includes_source_field(tmp_path: Path) -> None:
    """JSONL audit log carries source so downstream readers can filter
    'all controller promotes in the last 24h' without guessing."""
    reg = SkillRegistry(history_dir=tmp_path)
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.promote("s", 2, evidence=["e"], source="controller")

    import json
    log = tmp_path / "s.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["source"] == "controller"


# ── B-174: replay_history restores HEAD across daemon restart ──────────


def test_replay_restores_head_after_promote(tmp_path: Path) -> None:
    """Daemon-1 promotes to v2; daemon-2 boots with same history dir
    and replay must lift HEAD back to v2 (not the implicit v1 from
    register order)."""
    # Daemon 1 — write history.
    reg1 = SkillRegistry(history_dir=tmp_path)
    reg1.register(_skill("s", 1), _manifest("s", 1))
    reg1.register(_skill("s", 2), _manifest("s", 2))
    reg1.promote("s", 2, evidence=["e"])
    assert reg1.active_version("s") == 2

    # Daemon 2 — fresh registry, same history dir, re-register skills.
    reg2 = SkillRegistry(history_dir=tmp_path)
    reg2.register(_skill("s", 1), _manifest("s", 1))
    reg2.register(_skill("s", 2), _manifest("s", 2))
    assert reg2.active_version("s") == 1  # set_head=True default → v1

    replayed = reg2.replay_history()
    assert replayed == {"s": 2}
    assert reg2.active_version("s") == 2


def test_replay_applies_chronologically(tmp_path: Path) -> None:
    """Multiple records: promote v2 → rollback v1 → promote v3.
    Final HEAD must be v3."""
    reg1 = SkillRegistry(history_dir=tmp_path)
    reg1.register(_skill("s", 1), _manifest("s", 1))
    reg1.register(_skill("s", 2), _manifest("s", 2))
    reg1.register(_skill("s", 3), _manifest("s", 3))
    reg1.promote("s", 2, evidence=["a"])
    reg1.rollback("s", 1, reason="bad")
    reg1.promote("s", 3, evidence=["c"])

    reg2 = SkillRegistry(history_dir=tmp_path)
    reg2.register(_skill("s", 1), _manifest("s", 1))
    reg2.register(_skill("s", 2), _manifest("s", 2))
    reg2.register(_skill("s", 3), _manifest("s", 3))
    reg2.replay_history()
    assert reg2.active_version("s") == 3
    # History list is also re-populated so audit calls work.
    assert [r.kind for r in reg2.history("s")] == [
        "promote", "rollback", "promote",
    ]


def test_replay_skips_records_for_unregistered_versions(
    tmp_path: Path,
) -> None:
    """Original session promoted v3 → daemon restart finds only v1, v2
    (someone deleted v3 between sessions) → replay must skip v3 and
    leave HEAD at whatever the surviving record points to (v2 here)."""
    reg1 = SkillRegistry(history_dir=tmp_path)
    reg1.register(_skill("s", 1), _manifest("s", 1))
    reg1.register(_skill("s", 2), _manifest("s", 2))
    reg1.register(_skill("s", 3), _manifest("s", 3))
    reg1.promote("s", 2, evidence=["a"])
    reg1.promote("s", 3, evidence=["b"])

    reg2 = SkillRegistry(history_dir=tmp_path)
    reg2.register(_skill("s", 1), _manifest("s", 1))
    reg2.register(_skill("s", 2), _manifest("s", 2))
    # NOTE: v3 NOT re-registered in this boot.

    replayed = reg2.replay_history()
    assert replayed == {"s": 2}  # v3 record skipped
    assert reg2.active_version("s") == 2


def test_replay_with_no_history_dir_returns_empty() -> None:
    """No history_dir configured → replay is a graceful no-op."""
    reg = SkillRegistry()
    reg.register(_skill("s", 1), _manifest("s", 1))
    assert reg.replay_history() == {}


def test_replay_with_empty_history_dir_returns_empty(tmp_path: Path) -> None:
    reg = SkillRegistry(history_dir=tmp_path)
    reg.register(_skill("s", 1), _manifest("s", 1))
    assert reg.replay_history() == {}


def test_replay_tolerates_corrupt_jsonl_lines(tmp_path: Path) -> None:
    """One malformed line shouldn't lose the file's surviving records."""
    # Write good record, garbage line, good record manually.
    log = tmp_path / "s.jsonl"
    log.write_text(
        '{"kind": "promote", "skill_id": "s", "from_version": 0, '
        '"to_version": 2, "ts": 1.0, "evidence": ["a"], "source": "manual"}\n'
        'NOT JSON\n'
        '{"kind": "promote", "skill_id": "s", "from_version": 2, '
        '"to_version": 3, "ts": 2.0, "evidence": ["b"], "source": "manual"}\n',
        encoding="utf-8",
    )
    reg = SkillRegistry(history_dir=tmp_path)
    reg.register(_skill("s", 1), _manifest("s", 1))
    reg.register(_skill("s", 2), _manifest("s", 2))
    reg.register(_skill("s", 3), _manifest("s", 3))
    replayed = reg.replay_history()
    assert replayed == {"s": 3}


def test_replay_per_skill_id_filter(tmp_path: Path) -> None:
    """``replay_history(skill_id="x")`` only touches one skill's log."""
    reg1 = SkillRegistry(history_dir=tmp_path)
    reg1.register(_skill("a", 1), _manifest("a", 1))
    reg1.register(_skill("a", 2), _manifest("a", 2))
    reg1.register(_skill("b", 1), _manifest("b", 1))
    reg1.register(_skill("b", 2), _manifest("b", 2))
    reg1.promote("a", 2, evidence=["x"])
    reg1.promote("b", 2, evidence=["y"])

    reg2 = SkillRegistry(history_dir=tmp_path)
    reg2.register(_skill("a", 1), _manifest("a", 1))
    reg2.register(_skill("a", 2), _manifest("a", 2))
    reg2.register(_skill("b", 1), _manifest("b", 1))
    reg2.register(_skill("b", 2), _manifest("b", 2))
    replayed = reg2.replay_history(skill_id="a")
    assert replayed == {"a": 2}
    assert reg2.active_version("a") == 2
    assert reg2.active_version("b") == 1  # b's history NOT replayed
