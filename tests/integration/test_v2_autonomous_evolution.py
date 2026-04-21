"""Autonomous evolution loop — end-to-end two-session demo.

This is the "self-evolving agent" claim collapsed into one mechanical
assertion: session 2, using the HEAD version chosen by session 1's
evolution controller, earns more reward per turn than session 1 did.

    Session 1 (cold start):
      * Registry has skill demo.sum v1 (baseline uniform-variant).
      * Scheduler bandit explores 6 prompt variants.
      * Grader scores each turn.
      * EvolutionController reads per-arm evidence at session end and
        returns PROMOTE(best_variant).
      * Orchestrator registers a new skill version whose prompt is
        the winning variant; ``registry.promote`` with that evidence.

    Session 2 (inherits HEAD):
      * Registry's HEAD now points at v2 (the evolved skill).
      * Orchestrator does NOT run the bandit. It simply runs the HEAD
        skill directly against the same corpus.
      * Asserts session-2 mean reward > session-1 mean reward by a
        non-trivial margin.

No LLM API needed — the ``_PredictableMockLLM`` returns deterministic
canned summaries per variant so the whole run is hermetic. The real-
LLM version layers on top with no architectural changes; Phase 4 wires
it up inside the daemon.
"""
from __future__ import annotations

import asyncio
import statistics
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.evolution import EvolutionController, EvolutionDecision
from xmclaw.core.evolution.controller import CandidateEvaluation
from xmclaw.core.grader import HonestGrader
from xmclaw.core.grader.domain import SummaryQualityGrader
from xmclaw.core.grader.domain.summary import SummaryTask
from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.demo.read_and_summarize import DEMO_VARIANTS, Variant
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


# ── scripted mock LLM (one canned summary per variant) ──────────────────


@dataclass
class _VariantAwareLLM(LLMProvider):
    """Returns a canned summary picked by sniffing the variant's suffix
    in the user message. Deterministic, no network."""

    canned: dict[str, str] = field(default_factory=dict)

    async def stream(  # pragma: no cover — not used in this test
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        *,
        cancel: asyncio.Event | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        text_in = messages[-1].content
        variant_id = "terse"
        for v in DEMO_VARIANTS:
            if v.prompt_suffix in text_in:
                variant_id = v.id
                break
        return LLMResponse(
            content=self.canned.get(variant_id, ""),
            tool_calls=(),
            prompt_tokens=10,
            completion_tokens=10,
            latency_ms=1.0,
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


def _canned_per_variant() -> dict[str, str]:
    """Same calibration as test_v2_live_pipeline.py but de-duplicated."""
    return {
        "bullets": (
            "- Plants use sunlight to make glucose.\n"
            "- Oxygen is a byproduct.\n"
            "- The reaction lives in chloroplasts."
        ),
        # 'exec' canned response intentionally omits 'chloroplast' so it
        # hits only 3/4 keywords — ensures 'bullets' (all 4 + bullet
        # structure) dominates clearly, clearing the controller's
        # gap_over_second gate.
        "exec": (
            "Plants use sunlight, water, and CO2 to produce glucose and "
            "release oxygen as a byproduct of the reaction."
        ),
        "terse": (
            "Photosynthesis converts sunlight, water and CO2 into glucose "
            "and oxygen in chloroplasts."
        ),
        "tl;dr": "TL;DR: plants eat sunlight to make oxygen and glucose.",
        "verbose": (
            "Photosynthesis is a biochemical cascade. " * 25
        ),
        "lowball": "ok",
    }


_DOC = (
    "Photosynthesis converts sunlight, water, and carbon dioxide into "
    "glucose and oxygen inside chloroplasts."
)
_TASK = SummaryTask(
    file_id="photo",
    reference_keywords=("sunlight", "oxygen", "glucose", "chloroplast"),
    target_words=25,
    target_words_tol=0.6,
)


# ── a versionable skill that freezes a single variant ────────────────────


class _FrozenVariantSkill(Skill):
    """Skill whose behaviour is pinned to ONE variant — no exploration.

    v1 = the "uniform" baseline (averages over all arms when stats
    summarize the bench). Subsequent versions freeze a single winning
    variant so session 2 bypasses the bandit entirely.
    """

    def __init__(self, *, skill_id: str, version: int, variant: Variant,
                 llm: LLMProvider) -> None:
        self.id = skill_id
        self.version = version
        self._variant = variant
        self._llm = llm

    async def run(self, inp: SkillInput) -> SkillOutput:
        doc = inp.args.get("file_content", "")
        user = (
            f"Document:\n\n{doc}\n\n"
            f"Instruction: {self._variant.prompt_suffix}"
        )
        resp = await self._llm.complete([
            Message(role="system", content="You are a summarization assistant."),
            Message(role="user", content=user),
        ])
        return SkillOutput(
            ok=True,
            result={"summary": resp.content, "variant": self._variant.id},
            side_effects=[],
        )


# ── session runners ──────────────────────────────────────────────────────


@dataclass
class _SessionResult:
    scores: list[float]
    plays_per_variant: dict[str, int]
    scores_per_variant: dict[str, list[float]]
    best_variant_id: str
    best_mean: float


async def _run_bandit_session(
    llm: LLMProvider, *, turns: int = 40,
) -> _SessionResult:
    """Session 1 — classic bandit over 6 variants, grading each turn."""
    bus = InProcessEventBus()
    quality = SummaryQualityGrader(require_structure=True)
    structural = HonestGrader()

    candidates = [
        Candidate(skill_id="demo.sum", version=1, prompt_delta={"variant": v.id}, evidence=[])
        for v in DEMO_VARIANTS
    ]
    scheduler = OnlineScheduler(candidates=candidates, exploration_c=0.5)

    async def on_verdict(event) -> None:  # noqa: ANN001
        await scheduler.on_event(event)

    bus.subscribe(
        lambda e: e.type == EventType.GRADER_VERDICT, on_verdict,
    )

    scores: list[float] = []
    plays: dict[str, int] = {v.id: 0 for v in DEMO_VARIANTS}
    per_variant_scores: dict[str, list[float]] = {v.id: [] for v in DEMO_VARIANTS}
    for turn in range(turns):
        idx = scheduler.pick()
        v = DEMO_VARIANTS[idx]
        plays[v.id] += 1
        skill = _FrozenVariantSkill(
            skill_id="demo.sum", version=1, variant=v, llm=llm,
        )
        out = await skill.run(SkillInput(args={"file_content": _DOC}))

        finished = make_event(
            session_id="s1", agent_id="a",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": f"t{turn}",
                "result": out.result,
                "error": None,
                "expected_type": "dict",
                "expected_side_effects": [],
                "candidate_idx": idx,
            },
        )
        s_verdict = await structural.grade(finished)
        q_verdict = quality.grade(
            out.result.get("summary", ""), _TASK, variant_id=v.id,
        )
        reward = q_verdict.score if (s_verdict.ran and s_verdict.returned) else 0.0
        scores.append(reward)
        per_variant_scores[v.id].append(reward)
        await bus.publish(make_event(
            session_id="s1", agent_id="a",
            type=EventType.GRADER_VERDICT,
            payload={"candidate_idx": idx, "score": reward},
        ))
        await bus.drain()

    # Best arm by mean (ignoring unplayed arms).
    best_idx = max(
        range(len(scheduler.stats)),
        key=lambda i: (scheduler.stats[i].mean, scheduler.stats[i].plays),
    )
    return _SessionResult(
        scores=scores,
        plays_per_variant=plays,
        scores_per_variant=per_variant_scores,
        best_variant_id=DEMO_VARIANTS[best_idx].id,
        best_mean=scheduler.stats[best_idx].mean,
    )


async def _run_head_session(
    llm: LLMProvider, registry: SkillRegistry, *, turns: int = 10,
) -> list[float]:
    """Session 2 — no bandit. Just run the HEAD skill directly."""
    quality = SummaryQualityGrader(require_structure=True)
    scores: list[float] = []
    skill = registry.get("demo.sum")  # HEAD
    for _ in range(turns):
        out = await skill.run(SkillInput(args={"file_content": _DOC}))
        q_verdict = quality.grade(
            out.result.get("summary", ""), _TASK,
            variant_id=out.result.get("variant"),
        )
        scores.append(q_verdict.score)
    return scores


# ── the end-to-end test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_two_session_evolution_improves_mean_reward() -> None:
    """Session 1 explores 6 variants, promotes the best, session 2 uses
    the promoted skill exclusively and averages a higher reward."""
    llm = _VariantAwareLLM(canned=_canned_per_variant())

    # Registry starts with a baseline v1 pinned to a MEDIOCRE variant —
    # we use "terse" since its canned response scores middle-of-pack.
    registry = SkillRegistry()
    baseline_variant = next(v for v in DEMO_VARIANTS if v.id == "terse")
    baseline_skill = _FrozenVariantSkill(
        skill_id="demo.sum", version=1, variant=baseline_variant, llm=llm,
    )
    registry.register(baseline_skill, SkillManifest(id="demo.sum", version=1))

    # ── Session 1 ──
    s1 = await _run_bandit_session(llm, turns=40)
    s1_mean = statistics.mean(s1.scores)

    # EvolutionController reads arm stats and proposes a promotion.
    ctrl = EvolutionController()
    evaluations = [
        CandidateEvaluation(
            candidate_id=v.id, version=2,   # promoted version if accepted
            plays=s1.plays_per_variant[v.id],
            mean_score=statistics.mean(s1.scores_per_variant[v.id]),
        )
        for v in DEMO_VARIANTS
        if s1.plays_per_variant[v.id] > 0
    ]
    # Baseline's "head_mean" is what baseline scored in this session.
    # For our baseline we didn't explicitly run it separately here, so
    # use the session mean (gap-over-head falls back to session mean
    # when head_mean is None).
    report = ctrl.consider_promotion(evaluations, head_version=1)
    assert report.decision == EvolutionDecision.PROMOTE, (
        f"expected a promotion — got {report.reason}"
    )

    # Orchestrator step: register new skill version with the winner's variant.
    winner_variant = next(
        v for v in DEMO_VARIANTS if v.id == report.winner_candidate_id
    )
    evolved_skill = _FrozenVariantSkill(
        skill_id="demo.sum", version=2, variant=winner_variant, llm=llm,
    )
    registry.register(evolved_skill, SkillManifest(id="demo.sum", version=2))
    registry.promote("demo.sum", 2, evidence=list(report.evidence))
    assert registry.active_version("demo.sum") == 2

    # ── Session 2 ──
    s2_scores = await _run_head_session(llm, registry, turns=10)
    s2_mean = statistics.mean(s2_scores)

    print(
        f"\nAutonomous evolution result:\n"
        f"  session 1 variant plays: {s1.plays_per_variant}\n"
        f"  session 1 mean reward:   {s1_mean:.3f}  (40 turns, 6-arm bandit)\n"
        f"  winner promoted:         {report.winner_candidate_id!r}\n"
        f"  session 2 mean reward:   {s2_mean:.3f}  (10 turns on HEAD)\n"
        f"  improvement:             {s2_mean - s1_mean:+.3f} absolute, "
        f"{(s2_mean / s1_mean if s1_mean else 0):.2f}x relative\n"
    )

    # The promoted variant is the best arm by construction, so session-2
    # mean reward (always running on that variant) must be higher than
    # session-1 mean reward (a mix of all variants).
    assert s2_mean > s1_mean + 0.05, (
        f"autonomous evolution did not lift session 2's mean reward by a "
        f"meaningful margin. s1={s1_mean:.3f} s2={s2_mean:.3f}. "
        f"winner={report.winner_candidate_id!r} "
        f"plays_s1={s1.plays_per_variant}"
    )


@pytest.mark.asyncio
async def test_autonomous_evolution_is_noop_when_head_already_optimal() -> None:
    """If the registry's HEAD is already pinned to the best variant,
    the controller must return NO_CHANGE — no spurious promotion."""
    llm = _VariantAwareLLM(canned=_canned_per_variant())
    registry = SkillRegistry()

    # HEAD already at the best variant. In the canned setup, "bullets"
    # is the designed-best.
    best = next(v for v in DEMO_VARIANTS if v.id == "bullets")
    registry.register(
        _FrozenVariantSkill(skill_id="demo.sum", version=1, variant=best, llm=llm),
        SkillManifest(id="demo.sum", version=1),
    )

    # Synthesize arm stats where HEAD (version 1) is the best candidate
    # too — no one should be promoted.
    ctrl = EvolutionController()
    evaluations = [
        CandidateEvaluation(candidate_id="bullets", version=1, plays=15, mean_score=0.90),
        CandidateEvaluation(candidate_id="exec",    version=2, plays=15, mean_score=0.70),
    ]
    report = ctrl.consider_promotion(evaluations, head_version=1, head_mean=0.90)
    assert report.decision == EvolutionDecision.NO_CHANGE
    assert registry.active_version("demo.sum") == 1


