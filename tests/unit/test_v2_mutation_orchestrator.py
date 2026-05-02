"""B-172 — MutationOrchestrator unit tests.

Pins:
  * GRADER_VERDICT events update per-(skill_id, version) EWMA
  * Trigger requires: ewma < threshold AND samples >= min_samples
    AND cooldown elapsed
  * Successful mutation:
      - writes ``<skills_root>/<id>/versions/v<N>.md``
      - registers v<N> with set_head=False
      - emits SKILL_CANDIDATE_PROPOSED(decision=promote, evidence=[…])
  * Failed mutation (no candidate / score below delta) registers
    NOTHING and writes NOTHING.
  * In-flight short-circuit prevents re-entry while a mutation
    is already running.
  * Cooldown is honoured between successive triggers.
  * Non-string skill_id / non-numeric score → no crash, no trigger.
  * Lifecycle: start/stop idempotent, disabled = no subscription.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.evolution.dataset import EvalDataset
from xmclaw.core.evolution.mutator import MutationResult
from xmclaw.daemon.mutation_orchestrator import MutationOrchestrator
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


# ── stubs ────────────────────────────────────────────────────────


@dataclass
class _StubMutator:
    """Minimal SkillMutator stand-in. Returns scripted MutationResults."""

    next_result: MutationResult | None = None
    is_available_value: bool = True
    call_count: int = 0
    last_baseline_text: str | None = None

    @property
    def is_available(self) -> bool:
        return self.is_available_value

    async def mutate(
        self, *, skill_id: str, baseline_text: str, dataset: EvalDataset,
        constraint_overrides=None,
    ) -> MutationResult:
        self.call_count += 1
        self.last_baseline_text = baseline_text
        if self.next_result is None:
            return MutationResult(
                ok=False, skill_id=skill_id, candidate_text=None,
                baseline_score=0.0, candidate_holdout_score=0.0,
                constraint_report=None, duration_s=0.0,
                reason="stubbed_no_result",
            )
        return self.next_result


def _success(
    skill_id: str, *, candidate_text: str = "evolved body\n",
    baseline: float = 0.4, candidate: float = 0.7,
) -> MutationResult:
    return MutationResult(
        ok=True, skill_id=skill_id, candidate_text=candidate_text,
        baseline_score=baseline, candidate_holdout_score=candidate,
        constraint_report=None, duration_s=0.1, reason=None,
    )


def _verdict_event(
    skill_id: str, score: float, *, version: int = 1,
):
    return make_event(
        session_id="s1", agent_id="a",
        type=EventType.GRADER_VERDICT,
        payload={
            "skill_id": skill_id, "version": version,
            "score": score, "tool_name": f"skill_{skill_id}",
        },
    )


def _seed_v1(reg: SkillRegistry, skill_id: str, body: str = "v1 body\n") -> None:
    reg.register(
        MarkdownProcedureSkill(id=skill_id, body=body, version=1),
        SkillManifest(id=skill_id, version=1, created_by="user"),
        set_head=True,
    )


# ── EWMA / trigger conditions ────────────────────────────────────


@pytest.mark.asyncio
async def test_below_min_samples_no_trigger(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success("auto-foo"))
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=5, threshold=0.9,
        cooldown_s=0.0,
    )
    await mo.start()

    # 4 verdicts at 0.1 → ewma well below threshold but samples < 5.
    for _ in range(4):
        await bus.publish(_verdict_event("auto-foo", 0.1))
    await bus.drain()

    assert mutator.call_count == 0
    await mo.stop()


@pytest.mark.asyncio
async def test_above_threshold_no_trigger(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success("auto-foo"))
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=0.0,
    )
    await mo.start()

    # 5 verdicts at 0.9 → ewma above threshold, no trigger.
    for _ in range(5):
        await bus.publish(_verdict_event("auto-foo", 0.9))
    await bus.drain()

    assert mutator.call_count == 0
    await mo.stop()


@pytest.mark.asyncio
async def test_trigger_runs_mutator_when_conditions_met(
    tmp_path: Path,
) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success("auto-foo"))
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=0.0, score_delta=0.05,
    )
    await mo.start()

    # 3 low-score verdicts → ewma below 0.5 → trigger.
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()

    assert mutator.call_count == 1
    assert mutator.last_baseline_text == "v1 body\n"
    await mo.stop()


# ── successful mutation pipeline ────────────────────────────────


@pytest.mark.asyncio
async def test_successful_mutation_writes_versions_file(
    tmp_path: Path,
) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success(
        "auto-foo", candidate_text="v2 improved body\n",
        baseline=0.3, candidate=0.7,
    ))
    skills_root = tmp_path / "skills_user"
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=skills_root,
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=0.0,
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()

    versions_file = skills_root / "auto-foo" / "versions" / "v2.md"
    assert versions_file.is_file()
    assert versions_file.read_text(encoding="utf-8") == "v2 improved body\n"

    # v2 registered, set_head=False (head stays v1).
    assert reg.list_versions("auto-foo") == [1, 2]
    assert reg.active_version("auto-foo") == 1

    # Decision recorded.
    assert len(mo.decisions) == 1
    d = mo.decisions[0]
    assert d.promoted is True
    assert d.new_version == 2
    assert d.candidate_score == pytest.approx(0.7)
    await mo.stop()


@pytest.mark.asyncio
async def test_successful_mutation_emits_promote_event(
    tmp_path: Path,
) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success(
        "auto-foo", baseline=0.3, candidate=0.7,
    ))

    captured: list = []
    bus.subscribe(
        lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED,
        lambda e: captured.append(e) or None,
    )

    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=0.0,
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()

    assert len(captured) == 1
    p = captured[0].payload
    assert p["decision"] == "promote"
    assert p["winner_candidate_id"] == "auto-foo"
    assert p["winner_version"] == 2
    assert any("delta=+0.4" in str(e) for e in p["evidence"])
    await mo.stop()


# ── failure paths ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutator_no_candidate_no_register(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(
        next_result=MutationResult(
            ok=False, skill_id="auto-foo", candidate_text=None,
            baseline_score=0.0, candidate_holdout_score=0.0,
            constraint_report=None, duration_s=0.0,
            reason="dspy_not_installed",
        ),
    )
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=0.0,
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()

    assert reg.list_versions("auto-foo") == [1]
    assert mo.decisions and mo.decisions[0].promoted is False
    assert "dspy_not_installed" in mo.decisions[0].reason
    await mo.stop()


@pytest.mark.asyncio
async def test_score_delta_below_threshold_no_promote(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    # Candidate beats baseline by only 0.02 — below default delta 0.05.
    mutator = _StubMutator(next_result=_success(
        "auto-foo", baseline=0.5, candidate=0.52,
    ))
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.6,
        cooldown_s=0.0, score_delta=0.05,
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.4))
    await bus.drain()

    assert reg.list_versions("auto-foo") == [1]
    assert mo.decisions[0].promoted is False
    assert "score_delta_below_threshold" in mo.decisions[0].reason
    await mo.stop()


@pytest.mark.asyncio
async def test_in_flight_short_circuits_re_entry(tmp_path: Path) -> None:
    """While a mutation is awaiting, a fresh GRADER_VERDICT must not
    spawn a parallel mutation for the same (skill_id, version)."""
    import asyncio

    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")

    started_event = asyncio.Event()
    release_event = asyncio.Event()

    class _SlowMutator:
        is_available = True
        call_count = 0

        async def mutate(self, **kwargs):  # noqa: ANN003
            self.call_count += 1
            started_event.set()
            await release_event.wait()
            return _success(kwargs["skill_id"], baseline=0.3, candidate=0.7)

    slow = _SlowMutator()
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=slow, min_samples=3, threshold=0.5,
        cooldown_s=0.0,
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await asyncio.wait_for(started_event.wait(), timeout=2.0)

    # Mutator is suspended. Fire a fresh wave of verdicts — they must
    # NOT spawn a second mutate call.
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await asyncio.sleep(0.05)
    assert slow.call_count == 1, "in_flight gate must prevent re-entry"

    release_event.set()
    await bus.drain()
    await mo.stop()


@pytest.mark.asyncio
async def test_cooldown_blocks_successive_triggers(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    _seed_v1(reg, "auto-foo")
    mutator = _StubMutator(next_result=_success(
        "auto-foo", baseline=0.3, candidate=0.7,
    ))
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=3, threshold=0.5,
        cooldown_s=3600.0,  # huge cooldown
    )
    await mo.start()
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()
    assert mutator.call_count == 1

    # Reset registry so v2 isn't already there blocking us.
    # Send another wave — cooldown blocks.
    for _ in range(3):
        await bus.publish(_verdict_event("auto-foo", 0.2))
    await bus.drain()
    assert mutator.call_count == 1
    await mo.stop()


# ── malformed payloads ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_payload_no_crash(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    mutator = _StubMutator()
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=mutator, min_samples=1, threshold=1.0,
        cooldown_s=0.0,
    )
    await mo.start()
    bad_events = [
        # missing skill_id
        make_event(
            session_id="s", agent_id="a",
            type=EventType.GRADER_VERDICT,
            payload={"score": 0.1},
        ),
        # non-string skill_id
        make_event(
            session_id="s", agent_id="a",
            type=EventType.GRADER_VERDICT,
            payload={"skill_id": 42, "score": 0.1},
        ),
        # non-numeric score
        make_event(
            session_id="s", agent_id="a",
            type=EventType.GRADER_VERDICT,
            payload={"skill_id": "x", "score": "bad"},
        ),
    ]
    for ev in bad_events:
        await bus.publish(ev)
    await bus.drain()
    assert mutator.call_count == 0
    await mo.stop()


# ── lifecycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_start_no_op(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=_StubMutator(), enabled=False,
    )
    await mo.start()
    assert not mo.is_active


@pytest.mark.asyncio
async def test_start_stop_idempotent(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    reg = SkillRegistry()
    mo = MutationOrchestrator(
        reg, bus,
        skills_root=tmp_path / "sk",
        events_db_path=tmp_path / "events.db",
        mutator=_StubMutator(),
    )
    await mo.start()
    await mo.start()
    assert mo.is_active
    await mo.stop()
    await mo.stop()
    assert not mo.is_active


# ── B-172 user_loader extension: versions/ subdir ────────────────


def test_user_loader_reads_versions_subdir(tmp_path: Path) -> None:
    """v2 archived under versions/v2.md must register as v2 with HEAD
    staying at v1 — survives daemon restart."""
    from xmclaw.skills.user_loader import UserSkillsLoader

    sd = tmp_path / "auto-foo"
    (sd).mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: auto-foo\ndescription: 'baseline'\n---\n\nv1 body\n",
        encoding="utf-8",
    )
    (sd / "versions").mkdir()
    (sd / "versions" / "v2.md").write_text(
        "---\nname: auto-foo\ndescription: 'mutated'\n---\n\nv2 body\n",
        encoding="utf-8",
    )
    (sd / "versions" / "v3.md").write_text(
        "---\nname: auto-foo\ndescription: 'mutated again'\n---\n\nv3 body\n",
        encoding="utf-8",
    )
    reg = SkillRegistry()
    UserSkillsLoader(reg, tmp_path).load_all()

    assert reg.list_versions("auto-foo") == [1, 2, 3]
    assert reg.active_version("auto-foo") == 1  # HEAD untouched
    # v2 / v3 carry created_by="evolved" so UI badges them correctly.
    assert reg.ref("auto-foo", 2).manifest.created_by == "evolved"
    assert reg.ref("auto-foo", 3).manifest.created_by == "evolved"
