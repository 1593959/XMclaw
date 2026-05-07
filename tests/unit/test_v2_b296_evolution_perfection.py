"""B-296 + B-297: pin the per-skill iteration + state persistence
fixes that took the evolution chain from "structurally broken in
multi-skill setup" to correct.

* B-296: evaluate() iterates per skill_id. Pre-B-296 mixed all skills
  into one controller call → cross-skill comparisons → mis-promotion.
* B-297: _arms persists across daemon restarts. Pre-B-297 every
  restart wiped EWMA → controller's min_plays threshold never cleared.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.evolution.controller import (
    EvolutionDecision, PromotionThresholds,
)
from xmclaw.daemon.evolution_agent import EvolutionAgent


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


async def _publish(bus, skill_id: str, version: int, score: float) -> None:
    await bus.publish(make_event(
        session_id="t",
        agent_id="main",
        type=EventType.GRADER_VERDICT,
        payload={"skill_id": skill_id, "version": version, "score": score},
    ))


# ── B-296: per-skill iteration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_b296_returns_list_of_reports_one_per_skill(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """When _arms holds arms across multiple skills, evaluate() returns
    one report per skill_id. Pre-B-296 returned a single report that
    mixed skills."""
    agent = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    await agent.start()
    # Skill A: mostly low scores
    for _ in range(5):
        await _publish(bus, "skill_a", 1, 0.3)
    # Skill B: mostly high scores
    for _ in range(5):
        await _publish(bus, "skill_b", 1, 0.9)
    await bus.drain()

    reports = await agent.evaluate()
    assert isinstance(reports, list)
    assert len(reports) == 2  # one per skill_id


@pytest.mark.asyncio
async def test_b296_no_cross_skill_promotion(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """Pre-B-296 controller could "promote" skill_a's candidate
    because it ranked higher than skill_b's HEAD across the mixed
    pool. Now each skill is evaluated independently."""
    thresholds = PromotionThresholds(
        min_plays=3, min_mean=0.5, min_gap_over_head=0.05,
        min_gap_over_second=0.01,
    )
    agent = EvolutionAgent(
        "evo", bus, thresholds=thresholds, audit_dir=tmp_path,
    )
    await agent.start()

    # skill_a v1: HEAD baseline at mean 0.5
    for _ in range(5):
        await _publish(bus, "skill_a", 1, 0.5)
    # skill_a v2: better candidate (should propose promote for skill_a)
    for _ in range(5):
        await _publish(bus, "skill_a", 2, 0.95)
    # skill_b v1: HEAD baseline at mean 0.4
    for _ in range(5):
        await _publish(bus, "skill_b", 1, 0.4)
    # skill_b v2: a candidate that's WORSE than skill_b v1 but better
    # than skill_a v1 (would have been mis-promoted pre-B-296 if mixed).
    for _ in range(5):
        await _publish(bus, "skill_b", 2, 0.30)
    await bus.drain()

    # Provide HEAD overrides via call args (in-prod registry handles this).
    reports_a = await agent.evaluate(head_version=1, head_mean=0.5)
    decisions_by_skill = {}
    for r in reports_a:
        # winner_candidate_id contains the skill_id when proposing.
        sid = r.winner_candidate_id or "?"
        decisions_by_skill.setdefault(sid, r.decision)

    # skill_a should propose promote (v2 beats v1).
    assert decisions_by_skill.get("skill_a") == EvolutionDecision.PROMOTE
    # skill_b should NOT propose anything — its v2 is worse than its v1.
    # Either NO_CHANGE or absent from decisions_by_skill (winner=None).
    sb = decisions_by_skill.get("skill_b")
    assert sb in (None, EvolutionDecision.NO_CHANGE)


@pytest.mark.asyncio
async def test_b296_empty_arms_returns_single_no_change(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """No verdicts ingested yet — evaluate() returns exactly one
    NO_CHANGE report so the trigger has something to log + audit."""
    agent = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    await agent.start()
    reports = await agent.evaluate()
    assert len(reports) == 1
    assert reports[0].decision == EvolutionDecision.NO_CHANGE


# ── B-297: state persistence ───────────────────────────────────────


@pytest.mark.asyncio
async def test_b297_state_persists_across_instances(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """Build an agent, ingest verdicts, dispose. New agent in same
    audit_dir should rehydrate the EWMA stats from disk."""
    agent1 = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    await agent1.start()
    for _ in range(7):
        await _publish(bus, "skill_x", 1, 0.8)
    await bus.drain()
    await agent1.stop()

    # Disk artefact must exist.
    state_path = tmp_path / "evo" / "state.json"
    assert state_path.exists()
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["arms"]
    assert raw["arms"][0]["plays"] == 7

    # New agent on same path picks up the state.
    bus2 = InProcessEventBus()
    agent2 = EvolutionAgent("evo", bus2, audit_dir=tmp_path)
    snap = agent2.snapshot()
    assert len(snap) == 1
    assert snap[0].plays == 7


@pytest.mark.asyncio
async def test_b297_corrupt_state_ignored_silently(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """Corrupt JSON on disk should produce a warning + start fresh,
    not raise."""
    state_dir = tmp_path / "evo"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{not json", encoding="utf-8")

    agent = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    assert agent.snapshot() == []  # started empty, no exception


@pytest.mark.asyncio
async def test_b297_atomic_write_no_torn_state(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """Ingest 50 verdicts; each save_state writes atomically via
    os.replace. After the run, the .tmp sidecar must be gone."""
    agent = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    await agent.start()
    for i in range(50):
        await _publish(bus, "skill_y", 1, 0.5 + i * 0.01)
    await bus.drain()

    state_dir = tmp_path / "evo"
    assert (state_dir / "state.json").exists()
    # No leftover tmp file (os.replace cleaned up).
    assert not (state_dir / "state.json.tmp").exists()


# ── B-296 + B-297 together: restart-resilient per-skill ────────────


@pytest.mark.asyncio
async def test_restart_keeps_per_skill_arms_separate(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    agent1 = EvolutionAgent("evo", bus, audit_dir=tmp_path)
    await agent1.start()
    # Different skills get their own arms.
    for _ in range(3):
        await _publish(bus, "skill_alpha", 1, 0.7)
    for _ in range(3):
        await _publish(bus, "skill_beta", 1, 0.4)
    await bus.drain()
    await agent1.stop()

    agent2 = EvolutionAgent("evo", InProcessEventBus(), audit_dir=tmp_path)
    snap = agent2.snapshot()
    by_skill = {e.candidate_id: e for e in snap}
    assert "skill_alpha" in by_skill
    assert "skill_beta" in by_skill
    assert by_skill["skill_alpha"].plays == 3
    assert by_skill["skill_beta"].plays == 3
