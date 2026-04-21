"""Demo "skill": simulated read-and-summarize target for the Phase 1 bench.

This is NOT the final production skill. It's a deterministic oracle that
lets the Phase 1 learning-curve bench run without an LLM API key and
without network access, so the bandit+grader wiring can be verified on
every CI run.

Each ``Variant`` has a hidden ``true_mean`` reward. ``run_variant`` draws a
reward from ``Beta(a, b)`` centered on that mean, deterministic per seed.
The scheduler's job: discover the best variant by score.

The real LLM-backed skill lands in Phase 1.2 once the provider layer is
wired up (see V2_DEVELOPMENT.md §8 non-Phase-1 callouts).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from xmclaw.skills.base import Skill, SkillInput, SkillOutput


@dataclass(frozen=True, slots=True)
class Variant:
    """A prompt variant plus a hidden "true" quality for the simulated oracle."""

    id: str
    prompt_suffix: str
    true_mean: float  # hidden — the oracle uses this; the scheduler never sees it


@dataclass
class SimulatedOracle:
    """Reward-producing oracle for the learning-curve bench.

    Seeded so each bench run is reproducible. Reward is drawn so mean
    matches ``variant.true_mean`` asymptotically.
    """

    seed: int = 42

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def score(self, variant: Variant) -> float:
        # Beta-distributed reward centered on variant.true_mean with modest
        # concentration (a+b = 8). Picks out the true ordering in ~50 draws.
        mean = max(0.01, min(0.99, variant.true_mean))
        concentration = 8.0
        a = mean * concentration
        b = (1.0 - mean) * concentration
        return self._rng.betavariate(a, b)


DEMO_VARIANTS: tuple[Variant, ...] = (
    # Wide spread so the bench can show clear convergence in ~50 turns.
    Variant("terse",    "Summarize in one sentence.",                        true_mean=0.40),
    Variant("bullets",  "List 3 bullet points of the key claims.",           true_mean=0.55),
    Variant("exec",     "Give an executive summary under 100 words.",        true_mean=0.85),
    Variant("tl;dr",    "Prefix your summary with 'TL;DR:' and be concise.", true_mean=0.30),
    Variant("verbose",  "Write a thorough 500-word summary.",                true_mean=0.20),
)


class ReadAndSummarize(Skill):
    """Phase 1 simulated skill — wraps the oracle behind the Skill interface.

    Phase 1.2 replaces this class with a real LLM-backed implementation.
    """

    id = "demo.read_and_summarize"
    version = 1

    def __init__(self, variant: Variant, oracle: SimulatedOracle) -> None:
        self._variant = variant
        self._oracle = oracle

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        reward = self._oracle.score(self._variant)
        # The "result" here is the simulated summary. In real use this would
        # be the LLM output text; the grader scores what it sees.
        return SkillOutput(
            ok=True,
            result={
                "summary": f"<{self._variant.id}> oracle_reward={reward:.3f}",
                "_reward": reward,  # used by the bench; grader ignores it
            },
            side_effects=[],
        )
