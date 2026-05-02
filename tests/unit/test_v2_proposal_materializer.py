"""B-167 — ProposalMaterializer unit tests.

Pins:
  * SKILL_CANDIDATE_PROPOSED with decision="propose" → SKILL.md
    written + skill registered + HEAD set.
  * decision="promote" / "rollback" / missing → ignored (orchestrator's job).
  * Already-registered skill_id → skipped silently (idempotent re-emit).
  * Empty body → skipped.
  * Empty evidence → skipped (anti-req #12 spirit).
  * Manifest carries created_by="evolved" + evidence list.
  * SKILL.md frontmatter has description + triggers + evidence.
  * start/stop are idempotent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.daemon.proposal_materializer import ProposalMaterializer
from xmclaw.skills.registry import SkillRegistry


def _propose_event(
    *,
    skill_id: str = "auto.bash_review",
    body: str = "1. read the file\n2. summarise\n",
    evidence: list[str] | None = None,
    decision: str = "propose",
    triggers: list[str] | None = None,
    description: str = "Review a bash script and summarise risks.",
    confidence: float = 0.78,
    source_pattern: str = "tool 'bash' in 4 sessions",
):
    return make_event(
        session_id="skill-dream:default",
        agent_id="skill-dream",
        type=EventType.SKILL_CANDIDATE_PROPOSED,
        payload={
            "decision": decision,
            "winner_candidate_id": skill_id,
            "winner_version": 0,
            "evidence": evidence if evidence is not None else ["sess-1", "sess-2"],
            "reason": source_pattern,
            "draft": {
                "title": "Bash review",
                "description": description,
                "body": body,
                "triggers": triggers or ["bash", "review"],
                "confidence": confidence,
            },
        },
    )


# ── happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_writes_skill_md_and_registers(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    await bus.publish(_propose_event())
    await bus.drain()

    # Skill is registered.
    assert "auto.bash_review" in reg.list_skill_ids()
    ref = reg.ref("auto.bash_review")
    assert ref.version == 1
    assert ref.manifest.created_by == "evolved"
    assert ref.manifest.evidence == ("sess-1", "sess-2")

    # HEAD set so SkillToolProvider can pick it up immediately.
    assert reg.active_version("auto.bash_review") == 1

    # SKILL.md written with frontmatter.
    skill_path = tmp_path / "skills" / "auto.bash_review" / "SKILL.md"
    assert skill_path.is_file()
    text = skill_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "description: Review a bash script" in text
    assert "created_by: evolved" in text
    assert "'sess-1'" in text and "'sess-2'" in text
    assert "1. read the file" in text  # body preserved

    assert pm.materialized_count == 1
    assert pm.skipped_count == 0
    await pm.stop()


@pytest.mark.asyncio
async def test_skill_runs_after_materialization(tmp_path: Path) -> None:
    """End-to-end: after materialization, registry.get(id).run() returns
    the body text — proving the agent's tool invocation will work."""
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    await bus.publish(_propose_event(body="step A\nstep B\n"))
    await bus.drain()

    skill = reg.get("auto.bash_review")
    from xmclaw.skills.base import SkillInput
    out = await skill.run(SkillInput(args={}))
    assert out.ok
    assert "step A" in out.result["body"]
    await pm.stop()


# ── filtering ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["promote", "rollback", "", None])
async def test_non_propose_decisions_ignored(
    tmp_path: Path, decision,
) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    payload_decision = decision if decision is not None else None
    if payload_decision is None:
        ev = make_event(
            session_id="x", agent_id="y",
            type=EventType.SKILL_CANDIDATE_PROPOSED,
            payload={
                "winner_candidate_id": "x",
                "winner_version": 0,
                "evidence": ["s1"],
                "draft": {"body": "..."},
            },
        )
    else:
        ev = _propose_event(decision=payload_decision)
    await bus.publish(ev)
    await bus.drain()

    assert pm.materialized_count == 0
    assert reg.list_skill_ids() == []
    await pm.stop()


# ── idempotence + safety ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_registered_skipped(tmp_path: Path) -> None:
    """Same skill_id arriving a second time must not double-register
    or clobber the v1 we just wrote — the proposer can re-emit the
    same draft on the next dream tick."""
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    await bus.publish(_propose_event())
    await bus.drain()
    assert pm.materialized_count == 1

    await bus.publish(_propose_event(body="DIFFERENT BODY"))
    await bus.drain()
    assert pm.materialized_count == 1
    assert pm.skipped_count == 1

    # On-disk file must still hold the FIRST body (the one already
    # registered), not the second draft's body.
    text = (tmp_path / "skills" / "auto.bash_review" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    assert "1. read the file" in text
    assert "DIFFERENT BODY" not in text
    await pm.stop()


@pytest.mark.asyncio
async def test_empty_body_skipped(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    await bus.publish(_propose_event(body=""))
    await bus.drain()
    assert pm.materialized_count == 0
    assert reg.list_skill_ids() == []
    await pm.stop()


@pytest.mark.asyncio
async def test_no_evidence_refused(tmp_path: Path) -> None:
    """Anti-req #12 spirit: a 'propose' with no evidence is a malformed
    proposal. Materializer refuses to write a phantom skill rather
    than registering one with manifest.evidence=()."""
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    await bus.publish(_propose_event(evidence=[]))
    await bus.drain()
    assert pm.materialized_count == 0
    assert reg.list_skill_ids() == []
    await pm.stop()


@pytest.mark.asyncio
async def test_malformed_skill_id_skipped(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()

    ev = make_event(
        session_id="x", agent_id="y",
        type=EventType.SKILL_CANDIDATE_PROPOSED,
        payload={
            "decision": "propose",
            "winner_candidate_id": None,  # bad
            "winner_version": 0,
            "evidence": ["s1"],
            "draft": {"body": "..."},
        },
    )
    await bus.publish(ev)
    await bus.drain()
    assert pm.materialized_count == 0
    await pm.stop()


# ── lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_start_no_op(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(
        reg, bus, skills_root=tmp_path / "skills", enabled=False,
    )
    await pm.start()
    assert not pm.is_active

    await bus.publish(_propose_event())
    await bus.drain()
    assert pm.materialized_count == 0
    await pm.stop()


@pytest.mark.asyncio
async def test_start_stop_idempotent(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    await pm.start()
    await pm.start()  # second call no-op
    assert pm.is_active

    await pm.stop()
    await pm.stop()  # second call no-op
    assert not pm.is_active
