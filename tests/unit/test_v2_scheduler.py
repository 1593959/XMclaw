"""OnlineScheduler + policy unit tests."""
from __future__ import annotations

import pytest

from xmclaw.core.bus import EventType, make_event
from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.core.scheduler.policy import best_of_n, ucb1


# ── policy ────────────────────────────────────────────────────────────────

def test_ucb1_picks_unplayed_arm_first() -> None:
    # Two arms; one played with high reward, one unplayed — UCB1 picks unplayed.
    idx = ucb1(mean_rewards=[0.9, 0.0], plays=[5, 0])
    assert idx == 1


def test_ucb1_exploit_when_all_played() -> None:
    # All arms played many times; UCB1 picks the highest mean.
    idx = ucb1(mean_rewards=[0.2, 0.8, 0.5], plays=[10, 10, 10])
    assert idx == 1


def test_ucb1_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        ucb1(mean_rewards=[0.1, 0.2], plays=[1])


def test_ucb1_empty_raises() -> None:
    with pytest.raises(ValueError):
        ucb1(mean_rewards=[], plays=[])


def test_best_of_n_picks_max() -> None:
    assert best_of_n([0.1, 0.5, 0.3]) == 1


def test_best_of_n_tie_breaks_by_index() -> None:
    assert best_of_n([0.5, 0.5, 0.5]) == 0


# ── OnlineScheduler ───────────────────────────────────────────────────────

def _candidates(n: int) -> list[Candidate]:
    return [
        Candidate(skill_id=f"c{i}", version=1, prompt_delta={}, evidence=[])
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_scheduler_pick_visits_every_candidate_first() -> None:
    sch = OnlineScheduler(candidates=_candidates(3))
    picks: set[int] = set()
    for _ in range(3):
        idx = sch.pick()
        picks.add(idx)
        # Feed a play back so UCB1 sees it next iteration.
        await sch.on_event(make_event(
            session_id="t", agent_id="t", type=EventType.GRADER_VERDICT,
            payload={"candidate_idx": idx, "score": 0.5},
        ))
    # Each of the 3 arms should be picked at least once before any repeats.
    assert picks == {0, 1, 2}


def test_scheduler_empty_raises() -> None:
    sch = OnlineScheduler()
    with pytest.raises(RuntimeError):
        sch.pick()


def test_scheduler_add_candidate() -> None:
    sch = OnlineScheduler(candidates=_candidates(1))
    idx = sch.add_candidate(
        Candidate(skill_id="new", version=1, prompt_delta={}, evidence=["e1"]),
    )
    assert idx == 1
    assert len(sch.candidates) == 2


@pytest.mark.asyncio
async def test_scheduler_on_grader_verdict_updates_stats() -> None:
    sch = OnlineScheduler(candidates=_candidates(2))
    sch.pick()  # sets last_chosen_idx
    ev = make_event(
        session_id="t", agent_id="t", type=EventType.GRADER_VERDICT,
        payload={"candidate_idx": 0, "score": 0.7},
    )
    await sch.on_event(ev)
    assert sch.stats[0].plays == 1
    assert abs(sch.stats[0].mean - 0.7) < 1e-9


@pytest.mark.asyncio
async def test_scheduler_ignores_non_grader_events() -> None:
    sch = OnlineScheduler(candidates=_candidates(2))
    ev = make_event(
        session_id="t", agent_id="t", type=EventType.USER_MESSAGE,
        payload={"content": "hello"},
    )
    await sch.on_event(ev)
    assert all(s.plays == 0 for s in sch.stats)


@pytest.mark.asyncio
async def test_scheduler_ignores_out_of_range_candidate_idx() -> None:
    sch = OnlineScheduler(candidates=_candidates(2))
    ev = make_event(
        session_id="t", agent_id="t", type=EventType.GRADER_VERDICT,
        payload={"candidate_idx": 99, "score": 1.0},
    )
    await sch.on_event(ev)
    assert all(s.plays == 0 for s in sch.stats)


@pytest.mark.asyncio
async def test_promote_refuses_candidate_without_evidence() -> None:
    sch = OnlineScheduler(candidates=_candidates(1))
    no_evidence = Candidate(skill_id="x", version=2, prompt_delta={}, evidence=[])
    result = await sch.promote_candidate(no_evidence)
    assert not result.accepted
    assert "anti-req #12" in result.reason


@pytest.mark.asyncio
async def test_promote_accepts_candidate_with_evidence() -> None:
    sch = OnlineScheduler(candidates=_candidates(1))
    with_evidence = Candidate(
        skill_id="x", version=2, prompt_delta={},
        evidence=["grader_verdict=0.85 over 20 runs"],
    )
    result = await sch.promote_candidate(with_evidence)
    assert result.accepted
    assert result.reason == "ok"
