"""Scheduler ↔ Registry integration — anti-req #12 end-to-end.

The scheduler's ``promote_candidate`` returns ``PromotionResult``. When
it returns accepted=True, the orchestrator passes the candidate's
evidence into ``registry.promote()``. Both gate on ``evidence`` being
non-empty. An attempt to bypass either one raises.

This test exercises the full path:
  candidate (with evidence)
    → scheduler.promote_candidate        (accept)
      → registry.promote                 (HEAD moves, record persisted)
        → registry.get                   (returns new HEAD)

And the negative path:
  candidate (no evidence)
    → scheduler.promote_candidate        (refuse + cite anti-req #12)
      → registry NOT touched
"""
from __future__ import annotations

import pytest

from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


class _NoopSkill(Skill):
    def __init__(self, skill_id: str, version: int) -> None:
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"v": self.version}, side_effects=[])


@pytest.mark.asyncio
async def test_happy_path_promote_lands_in_registry() -> None:
    reg = SkillRegistry()
    reg.register(_NoopSkill("demo", 1), SkillManifest(id="demo", version=1))
    reg.register(_NoopSkill("demo", 2), SkillManifest(id="demo", version=2))
    assert reg.active_version("demo") == 1

    sch = OnlineScheduler(candidates=[])
    candidate = Candidate(
        skill_id="demo", version=2, prompt_delta={},
        evidence=["bench.ratio=1.12 over 40 turns"],
    )
    result = await sch.promote_candidate(candidate)
    assert result.accepted

    # Orchestrator step: on accept, promote in the registry.
    record = reg.promote(
        candidate.skill_id, candidate.version,
        evidence=candidate.evidence,
    )
    assert reg.active_version("demo") == 2
    assert record.kind == "promote"
    assert record.evidence == ("bench.ratio=1.12 over 40 turns",)


@pytest.mark.asyncio
async def test_no_evidence_refused_by_both_layers() -> None:
    """Defense in depth — scheduler refuses first (cites anti-req #12),
    and if a buggy orchestrator somehow tried registry.promote directly
    the registry also refuses. Both layers enforce the same invariant."""
    reg = SkillRegistry()
    reg.register(_NoopSkill("demo", 1), SkillManifest(id="demo", version=1))
    reg.register(_NoopSkill("demo", 2), SkillManifest(id="demo", version=2))

    sch = OnlineScheduler(candidates=[])
    candidate = Candidate(
        skill_id="demo", version=2, prompt_delta={}, evidence=[],
    )
    # Scheduler path
    result = await sch.promote_candidate(candidate)
    assert not result.accepted
    assert "anti-req #12" in result.reason
    # HEAD unchanged
    assert reg.active_version("demo") == 1

    # Direct registry path
    with pytest.raises(ValueError, match="anti-req #12"):
        reg.promote("demo", 2, evidence=[])
    assert reg.active_version("demo") == 1


@pytest.mark.asyncio
async def test_rollback_after_bad_promotion() -> None:
    """A promotion lands; later evidence shows the new version is worse;
    orchestrator rolls back. History retains both events."""
    reg = SkillRegistry()
    reg.register(_NoopSkill("demo", 1), SkillManifest(id="demo", version=1))
    reg.register(_NoopSkill("demo", 2), SkillManifest(id="demo", version=2))

    sch = OnlineScheduler(candidates=[])
    good_candidate = Candidate(
        skill_id="demo", version=2, prompt_delta={},
        evidence=["initial bench 1.15x"],
    )
    (await sch.promote_candidate(good_candidate))
    reg.promote("demo", 2, evidence=good_candidate.evidence)

    # Later: production run shows regression.
    reg.rollback(
        "demo", 1,
        reason="prod bench dropped to 0.92x over weekend; reverting until grader re-tuned",
    )
    assert reg.active_version("demo") == 1

    # History shows the full story
    h = reg.history("demo")
    assert len(h) == 2
    assert h[0].kind == "promote"
    assert h[0].evidence == ("initial bench 1.15x",)
    assert h[1].kind == "rollback"
    assert "weekend" in h[1].reason
