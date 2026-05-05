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
    assert "step A" in out.result["instructions"]
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


# ── B-201: near-duplicate dedup ───────────────────────────────────


@pytest.mark.asyncio
async def test_b201_near_duplicate_skipped(tmp_path: Path) -> None:
    """B-201: when a candidate is semantically near an already-
    registered procedure (vec distance below threshold), the
    materializer must skip the second registration. The probe-b200
    dogfood produced 11 auto-* skills in 72min, of which 4-5 were
    near-dups — this guard caps that bleed."""

    class _StubMem:
        """Minimal SqliteVecMemory-shaped stub. _find_near_neighbour
        always returns a hit so the materializer treats every
        candidate as a duplicate. Production providers do the actual
        vec search; we just verify the materializer respects the
        return value."""
        def __init__(self) -> None:
            self.queries: list[dict] = []
            self.put_calls: list = []

        async def _find_near_neighbour(self, *, embedding, text, kind,
                                        distance_threshold):  # noqa: ANN001
            self.queries.append({"kind": kind, "threshold": distance_threshold})
            return "procedure:already-exists"  # always-near-dup

        async def query(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

        async def put(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.put_calls.append((args, kwargs))
            return ""

    class _StubEmb:
        async def embed(self, texts):  # noqa: ANN001
            return [[0.1] * 1024 for _ in texts]

    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    mem = _StubMem()
    pm = ProposalMaterializer(
        reg, bus, skills_root=tmp_path / "skills",
        memory_provider=mem, embedder=_StubEmb(),
    )
    await pm.start()

    await bus.publish(_propose_event(skill_id="auto.dup_candidate"))
    await bus.drain()

    # Skill must NOT have been registered (near-dup rejected).
    # SkillRegistry.get() raises UnknownSkillError when no HEAD —
    # that's the desired state: the skill never got promoted.
    from xmclaw.skills.registry import UnknownSkillError
    with pytest.raises(UnknownSkillError):
        reg.get("auto.dup_candidate")
    # The dedup query did fire.
    assert mem.queries and mem.queries[0]["kind"] == "procedure"
    # No row was put (write_procedure_to_memory never called for skipped).
    assert mem.put_calls == []
    # Skipped count incremented for observability.
    assert pm.skipped_count == 1

    await pm.stop()


@pytest.mark.asyncio
async def test_b201_no_dedup_when_no_existing_procedures(
    tmp_path: Path,
) -> None:
    """First-of-its-kind candidate (no near-neighbour found) must
    materialize normally."""
    class _StubMem:
        async def _find_near_neighbour(self, **kwargs):  # noqa: ANN003
            return None  # no match

        async def query(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

        async def put(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return ""

    class _StubEmb:
        async def embed(self, texts):  # noqa: ANN001
            return [[0.1] * 1024 for _ in texts]

    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(
        reg, bus, skills_root=tmp_path / "skills",
        memory_provider=_StubMem(), embedder=_StubEmb(),
    )
    await pm.start()

    await bus.publish(_propose_event(skill_id="auto.unique_candidate"))
    await bus.drain()

    # Materialized successfully — no dup hit.
    skill = reg.get("auto.unique_candidate")
    assert skill is not None
    assert pm.materialized_count == 1
    assert pm.skipped_count == 0

    await pm.stop()


@pytest.mark.asyncio
async def test_b201_dedup_disabled_without_memory_provider(
    tmp_path: Path,
) -> None:
    """Tests that wire only the registry (no memory_provider /
    embedder) MUST still materialize — dedup is best-effort, not a
    blocking dependency. Back-compat with the 12 existing
    proposal_materializer tests."""
    bus = InProcessEventBus()
    reg = SkillRegistry(history_dir=tmp_path / "history")
    pm = ProposalMaterializer(reg, bus, skills_root=tmp_path / "skills")
    # No memory_provider / no embedder.
    await pm.start()

    await bus.publish(_propose_event(skill_id="auto.no_dedup_path"))
    await bus.drain()

    skill = reg.get("auto.no_dedup_path")
    assert skill is not None  # materialized despite no dedup wiring
    assert pm.materialized_count == 1

    await pm.stop()
