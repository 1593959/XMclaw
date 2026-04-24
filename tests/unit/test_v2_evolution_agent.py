"""EvolutionAgent — headless observer workspace (Epic #17 Phase 7).

Locks in the contracts that the controller + manager rely on:

* Bus subscription: started/stopped idempotently, only consumes
  ``grader_verdict`` events, ingest never crashes on malformed payloads.
* Aggregation: per (skill_id, version) running mean + plays, with the
  bench ``candidate_idx`` fallback so pre-Phase-7 emitters still land.
* Decision: ``evaluate()`` produces a controller report, writes one
  JSONL line per call, and publishes a ``SKILL_CANDIDATE_PROPOSED``
  event only on PROMOTE.

These tests drive the observer synchronously over an
``InProcessEventBus``; the bus fans handlers out into asyncio tasks
that ``bus.drain()`` joins before assertions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.evolution.controller import (
    EvolutionDecision,
    PromotionThresholds,
)
from xmclaw.daemon.evolution_agent import EvolutionAgent


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "evo"


async def _publish_verdict(
    bus: InProcessEventBus,
    *,
    skill_id: str | None,
    version: int,
    score: float,
    candidate_idx: int | None = None,
) -> None:
    payload: dict[str, object] = {"score": score, "version": version}
    if skill_id is not None:
        payload["skill_id"] = skill_id
    if candidate_idx is not None:
        payload["candidate_idx"] = candidate_idx
    event = make_event(
        session_id="s-1", agent_id="observer",
        type=EventType.GRADER_VERDICT, payload=payload,
    )
    await bus.publish(event)


# ── lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_idempotent(bus: InProcessEventBus, audit_dir: Path) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    first = agent._subscription
    await agent.start()
    # Second start must not install a new subscription (would double-count).
    assert agent._subscription is first
    assert agent.is_running() is True


@pytest.mark.asyncio
async def test_stop_before_start_is_noop(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.stop()
    assert agent.is_running() is False


@pytest.mark.asyncio
async def test_stop_cancels_subscription(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    await agent.stop()
    assert agent.is_running() is False
    # After stop, events no longer update the aggregate.
    await _publish_verdict(bus, skill_id="s", version=1, score=0.9)
    await bus.drain()
    assert agent.snapshot() == []


# ── aggregation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_aggregates_per_skill_version(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    await _publish_verdict(bus, skill_id="summary", version=1, score=0.8)
    await _publish_verdict(bus, skill_id="summary", version=1, score=0.6)
    await _publish_verdict(bus, skill_id="summary", version=2, score=1.0)
    await bus.drain()

    evals = {(e.candidate_id, e.version): e for e in agent.snapshot()}
    assert evals[("summary", 1)].plays == 2
    assert evals[("summary", 1)].mean_score == pytest.approx(0.7)
    assert evals[("summary", 2)].plays == 1
    assert evals[("summary", 2)].mean_score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_ingest_falls_back_to_candidate_idx(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    # Bench emitters only stamp candidate_idx — the observer must still
    # aggregate rather than silently drop.
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    await _publish_verdict(bus, skill_id=None, version=0, score=0.5, candidate_idx=2)
    await bus.drain()
    evals = agent.snapshot()
    assert len(evals) == 1
    assert evals[0].candidate_id == "candidate_idx:2"


@pytest.mark.asyncio
async def test_ingest_ignores_events_without_score(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    event = make_event(
        session_id="s", agent_id="o", type=EventType.GRADER_VERDICT,
        payload={"skill_id": "summary", "version": 1},  # no score
    )
    await bus.publish(event)
    await bus.drain()
    assert agent.snapshot() == []


@pytest.mark.asyncio
async def test_ingest_ignores_unrelated_event_types(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    event = make_event(
        session_id="s", agent_id="o", type=EventType.TOOL_INVOCATION_FINISHED,
        payload={"skill_id": "summary", "version": 1, "score": 0.9},
    )
    await bus.publish(event)
    await bus.drain()
    assert agent.snapshot() == []


# ── decision + audit ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_no_change_when_under_min_plays(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    # Controller defaults require min_plays=10; 3 verdicts is below gate.
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    for _ in range(3):
        await _publish_verdict(bus, skill_id="summary", version=2, score=0.9)
    await bus.drain()

    report = await agent.evaluate(head_version=1, head_mean=0.5)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert agent.audit_path.exists()
    line = agent.audit_path.read_text(encoding="utf-8").splitlines()
    assert len(line) == 1
    rec = json.loads(line[0])
    assert rec["decision"] == "no_change"
    assert rec["head_version"] == 1


@pytest.mark.asyncio
async def test_evaluate_promote_publishes_candidate_proposed(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    # Tight thresholds so 5 high-score verdicts clear every gate.
    thresholds = PromotionThresholds(
        min_plays=3, min_mean=0.5, min_gap_over_head=0.05,
        min_gap_over_second=0.01,
    )
    agent = EvolutionAgent(
        "evo-1", bus, thresholds=thresholds, audit_dir=audit_dir,
    )
    await agent.start()

    proposals: list[object] = []

    async def _collect(ev: object) -> None:
        proposals.append(ev)

    bus.subscribe(
        lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED, _collect,
    )

    for _ in range(5):
        await _publish_verdict(bus, skill_id="summary", version=2, score=0.95)
    await bus.drain()

    report = await agent.evaluate(head_version=1, head_mean=0.5)
    await bus.drain()
    assert report.decision == EvolutionDecision.PROMOTE
    assert report.winner_candidate_id == "summary"
    assert report.winner_version == 2
    # Audit log captured the decision.
    recs = [
        json.loads(line)
        for line in agent.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert recs[-1]["decision"] == "promote"
    assert recs[-1]["winner_candidate_id"] == "summary"
    # Bus carried a proposal event.
    assert len(proposals) == 1


@pytest.mark.asyncio
async def test_evaluate_no_promote_emits_no_event(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    proposals: list[object] = []

    async def _collect(ev: object) -> None:
        proposals.append(ev)

    bus.subscribe(
        lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED, _collect,
    )
    for _ in range(2):
        await _publish_verdict(bus, skill_id="summary", version=2, score=0.6)
    await bus.drain()
    report = await agent.evaluate(head_version=1, head_mean=0.5)
    await bus.drain()
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert proposals == []


@pytest.mark.asyncio
async def test_reset_clears_aggregate(
    bus: InProcessEventBus, audit_dir: Path,
) -> None:
    agent = EvolutionAgent("evo-1", bus, audit_dir=audit_dir)
    await agent.start()
    await _publish_verdict(bus, skill_id="summary", version=1, score=0.9)
    await bus.drain()
    assert agent.snapshot()
    agent.reset()
    assert agent.snapshot() == []
