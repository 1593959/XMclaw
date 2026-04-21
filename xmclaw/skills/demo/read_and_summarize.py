"""Demo skill: read-and-summarize, in two flavors.

* ``SimulatedReadAndSummarize`` — deterministic, no LLM needed. Used by
  the offline Phase 1 bench so CI can verify the scheduler+grader wiring
  on every push without spending API tokens.
* ``LiveReadAndSummarize`` — real LLM-backed implementation. Consumes an
  ``LLMProvider`` and returns the provider's actual summary text. Used by
  the opt-in live bench (``tests/bench/phase1_live_learning_curve.py``).

Both share ``DEMO_VARIANTS`` so the scheduler learns over the same arm
set in either mode.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from xmclaw.providers.llm.base import LLMProvider, Message
from xmclaw.skills.base import Skill, SkillInput, SkillOutput


@dataclass(frozen=True, slots=True)
class Variant:
    """A prompt variant."""

    id: str
    prompt_suffix: str
    true_mean: float = 0.0  # only meaningful for the simulated oracle


DEMO_VARIANTS: tuple[Variant, ...] = (
    Variant("terse",    "Summarize in one sentence.",                        true_mean=0.40),
    Variant("bullets",  "List 3 bullet points of the key claims.",           true_mean=0.55),
    Variant("exec",     "Give an executive summary under 100 words.",        true_mean=0.85),
    Variant("tl;dr",    "Prefix your summary with 'TL;DR:' and be concise.", true_mean=0.30),
    Variant("verbose",  "Write a thorough 500-word summary.",                true_mean=0.20),
    # ``lowball`` is an intentionally-bad arm (live bench: reliably produces
    # a single-word response that misses all reference keywords and is far
    # off the target word count). It exists so the 50-turn bandit has a
    # clearly-separable signal on capable models where the other five arms
    # are all "reasonably competent". See live bench docstring for the
    # reasoning behind this design choice.
    Variant("lowball",  "Reply with ONLY the single word: ok",               true_mean=0.10),
)


# ── simulated ──

@dataclass
class SimulatedOracle:
    """Reward-producing oracle for the offline learning-curve bench."""

    seed: int = 42

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def score(self, variant: Variant) -> float:
        mean = max(0.01, min(0.99, variant.true_mean))
        concentration = 8.0
        a = mean * concentration
        b = (1.0 - mean) * concentration
        return self._rng.betavariate(a, b)


class ReadAndSummarize(Skill):
    """Simulated skill — wraps the oracle behind the Skill interface.

    Kept under the original name so the offline bench import path remains
    stable. Live variant uses a different class to prevent confusion.
    """

    id = "demo.read_and_summarize"
    version = 1

    def __init__(self, variant: Variant, oracle: SimulatedOracle) -> None:
        self._variant = variant
        self._oracle = oracle

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        reward = self._oracle.score(self._variant)
        return SkillOutput(
            ok=True,
            result={
                "summary": f"<{self._variant.id}> oracle_reward={reward:.3f}",
                "_reward": reward,
            },
            side_effects=[],
        )


# ── live (LLM-backed) ──

_SYSTEM_PROMPT = (
    "You are a precise summarization assistant. Read the provided text "
    "and follow the user's formatting instruction exactly. Be accurate "
    "and concise. Do not add commentary beyond the summary itself."
)


class LiveReadAndSummarize(Skill):
    """LLM-backed skill. One call to the provider per ``run``.

    ``SkillInput.args`` expected keys:
      * ``file_content: str`` — the document to summarize (required)
      * ``file_id: str`` — identifier for logging (optional)

    The variant's ``prompt_suffix`` is appended to a fixed instruction so
    the scheduler's arms are ONLY different by that single suffix — clean
    A/B attribution.
    """

    id = "demo.live_read_and_summarize"
    version = 1

    def __init__(self, variant: Variant, llm: LLMProvider) -> None:
        self._variant = variant
        self._llm = llm

    async def run(self, inp: SkillInput) -> SkillOutput:
        content = inp.args.get("file_content", "")
        if not content:
            return SkillOutput(
                ok=False,
                result={"error": "missing file_content", "variant": self._variant.id},
                side_effects=[],
            )

        user_msg = (
            f"Document:\n\n{content}\n\n"
            f"Instruction: {self._variant.prompt_suffix}"
        )
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_msg),
        ]
        try:
            resp = await self._llm.complete(messages)
        except Exception as exc:  # noqa: BLE001 — surface as ok=False
            return SkillOutput(
                ok=False,
                result={"error": str(exc), "variant": self._variant.id},
                side_effects=[],
            )
        return SkillOutput(
            ok=True,
            result={
                "summary": resp.content,
                "variant": self._variant.id,
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "latency_ms": resp.latency_ms,
            },
            side_effects=[],
        )
