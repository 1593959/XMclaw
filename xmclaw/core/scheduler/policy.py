"""Selection policies used by ``OnlineScheduler``.

Phase 1: UCB1 (upper confidence bound) — well-understood, converges reliably
on bounded-reward bandits, needs no hyperparameter tuning beyond the
exploration constant. The reward-model assumption matches what the grader
produces (score ∈ [0, 1]).

Phase 4 swaps to a learned policy using cross-session signal (anti-req #2:
continuous memory feeds into decision, not just the active turn).
"""
from __future__ import annotations

import math


def ucb1(mean_rewards: list[float], plays: list[int], c: float = 2.0) -> int:
    """Return index of the arm maximizing the UCB1 statistic.

    UCB1(i) = mean_rewards[i] + sqrt(c * ln(N) / plays[i])

    where N = total plays across all arms. Unplayed arms are picked first
    (returned in index order) so we never miss an arm.
    """
    if len(mean_rewards) != len(plays):
        raise ValueError(
            f"mean_rewards and plays must have same length, "
            f"got {len(mean_rewards)} and {len(plays)}"
        )
    if not mean_rewards:
        raise ValueError("empty arm set")

    # Pick any unplayed arm first (exploration priority).
    for i, n in enumerate(plays):
        if n == 0:
            return i

    total = sum(plays)
    best_idx = 0
    best_score = float("-inf")
    for i, (mean, n) in enumerate(zip(mean_rewards, plays, strict=True)):
        bonus = math.sqrt(c * math.log(total) / n)
        score = mean + bonus
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def best_of_n(mean_rewards: list[float]) -> int:
    """Return index of the arm with highest mean reward (ties broken by index).

    Pure-exploit greedy — used for final candidate selection after the
    bandit has converged, or for deterministic benches.
    """
    if not mean_rewards:
        raise ValueError("empty arm set")
    best_idx = 0
    for i, r in enumerate(mean_rewards):
        if r > mean_rewards[best_idx]:
            best_idx = i
    return best_idx
