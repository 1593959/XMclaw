"""Phase 3.5 — autonomous evolution on a real LLM.

The end-to-end test from tests/integration/test_v2_autonomous_evolution.py,
but with a real Anthropic-compatible LLM in place of the MockLLM. This
is the validation point for all of Phase 1–3.3: if the MockLLM two-
session improvement doesn't hold up under real-LLM noise, the entire
self-evolution thesis needs re-examination.

Opt-in via env vars (same pattern as Phase 1/2 live benches):

    export XMC_ANTHROPIC_API_KEY=sk-...                (required)
    export XMC_BENCH_MODEL=claude-haiku-4-5-20251001   (optional)
    export XMC_BENCH_BASE_URL=https://api.anthropic.com (optional)
    python -m pytest tests/bench/phase3_autonomous_evolution_live.py -v -s

The flow:

  Session 1 (40 turns, 6-arm bandit):
    * Scheduler explores 6 prompt variants on real LLM.
    * HonestGrader + SummaryQualityGrader score each turn.
    * EvolutionController consumes per-variant observed means.

  Evolution step:
    * Controller decides PROMOTE or NO_CHANGE based on real data.
    * If PROMOTE: register a new skill version whose prompt is frozen
      to the winner, move HEAD, persist evidence in history.
    * If NO_CHANGE: test still passes IF session 1's best arm is not
      lowball (the structural sanity gate) — i.e. the controller was
      right to refuse because data didn't warrant promotion. Only the
      gate-triggering test case below asserts a promotion happened.

  Session 2 (15 turns, HEAD only):
    * Bypasses the bandit. Runs the promoted skill directly.
    * Target: session-2 mean reward ≥ 1.05 × session-1 mean reward.
      (With real-LLM noise, 1.05× is a realistic signal; the MockLLM
      two-session test achieves 1.22×.)

This bench skips cleanly on CI when no API key is set.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.evolution import EvolutionController, EvolutionDecision
from xmclaw.core.evolution.controller import CandidateEvaluation, PromotionThresholds
from xmclaw.core.grader import HonestGrader
from xmclaw.core.grader.domain import SummaryQualityGrader
from xmclaw.core.grader.domain.summary import SummaryTask
from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import LLMProvider, Message
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.demo.read_and_summarize import DEMO_VARIANTS, Variant
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


_SKIP_REASON = "Set XMC_ANTHROPIC_API_KEY to run the live Phase 3.5 autonomous-evolution bench."


_LIVE_CORPUS: tuple[tuple[str, SummaryTask], ...] = (
    (
        "Photosynthesis is the process by which green plants use sunlight, "
        "water, and carbon dioxide to produce oxygen and glucose. It "
        "occurs primarily in chloroplasts. This process is critical to "
        "the planet's oxygen cycle and to all food chains that depend "
        "on plant biomass.",
        SummaryTask(
            file_id="photosynthesis",
            reference_keywords=("sunlight", "oxygen", "glucose", "chloroplast"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "The Roman aqueducts delivered water to cities across the empire. "
        "Using gravity and gradient engineering, they transported clean "
        "water over dozens of miles, enabling public baths, fountains, "
        "and sanitation infrastructure that supported large urban "
        "populations.",
        SummaryTask(
            file_id="aqueducts",
            reference_keywords=("water", "Roman", "gravity", "cities"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "In 1965, Gordon Moore observed that the number of transistors on "
        "a chip doubled roughly every two years. This empirical trend, "
        "later called Moore's Law, drove decades of semiconductor "
        "industry planning and shaped computing economics.",
        SummaryTask(
            file_id="moore",
            reference_keywords=("Moore", "transistors", "doubling", "semiconductor"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
)


class _FrozenVariantSkill(Skill):
    """Skill pinned to a single prompt variant — what evolution freezes into."""

    def __init__(
        self, *, skill_id: str, version: int, variant: Variant,
        llm: LLMProvider,
    ) -> None:
        self.id = skill_id
        self.version = version
        self._variant = variant
        self._llm = llm

    async def run(self, inp: SkillInput) -> SkillOutput:
        doc = inp.args.get("file_content", "")
        resp = await self._llm.complete([
            Message(role="system",
                    content="You are a precise summarization assistant."),
            Message(role="user", content=(
                f"Document:\n\n{doc}\n\n"
                f"Instruction: {self._variant.prompt_suffix}"
            )),
        ])
        return SkillOutput(
            ok=True,
            result={"summary": resp.content, "variant": self._variant.id},
            side_effects=[],
        )


@dataclass
class _Session1Result:
    scores: list[float]
    plays_per_variant: dict[str, int]
    scores_per_variant: dict[str, list[float]]


async def _run_session1(
    llm: LLMProvider, log_path: Path, *, turns: int = 40,
) -> _Session1Result:
    bus = InProcessEventBus()
    quality = SummaryQualityGrader(require_structure=True)
    structural = HonestGrader()

    candidates = [
        Candidate(
            skill_id="demo.sum", version=1,
            prompt_delta={"variant": v.id}, evidence=[],
        )
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
    per_variant: dict[str, list[float]] = {v.id: [] for v in DEMO_VARIANTS}

    for turn in range(turns):
        idx = scheduler.pick()
        v = DEMO_VARIANTS[idx]
        plays[v.id] += 1
        doc, task = _LIVE_CORPUS[turn % len(_LIVE_CORPUS)]

        skill = _FrozenVariantSkill(
            skill_id="demo.sum", version=1, variant=v, llm=llm,
        )
        out = await skill.run(SkillInput(args={"file_content": doc}))

        finished = make_event(
            session_id="s1", agent_id="evo",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": f"s1-{turn}",
                "result": out.result,
                "error": None,
                "expected_type": "dict",
                "expected_side_effects": [],
                "candidate_idx": idx,
            },
        )
        s_verdict = await structural.grade(finished)
        summary_text = out.result.get("summary", "")
        q_verdict = quality.grade(summary_text, task, variant_id=v.id)
        reward = q_verdict.score if (s_verdict.ran and s_verdict.returned) else 0.0
        scores.append(reward)
        per_variant[v.id].append(reward)

        await bus.publish(make_event(
            session_id="s1", agent_id="evo",
            type=EventType.GRADER_VERDICT,
            payload={"candidate_idx": idx, "score": reward},
        ))
        await bus.drain()

        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "session": "s1",
                "turn": turn,
                "variant": v.id,
                "reward": reward,
                "summary_preview": summary_text[:100],
            }, ensure_ascii=False) + "\n")

    return _Session1Result(
        scores=scores, plays_per_variant=plays, scores_per_variant=per_variant,
    )


async def _run_session2(
    llm: LLMProvider, registry: SkillRegistry,
    log_path: Path, *, turns: int = 15,
) -> list[float]:
    quality = SummaryQualityGrader(require_structure=True)
    scores: list[float] = []
    skill = registry.get("demo.sum")

    for turn in range(turns):
        doc, task = _LIVE_CORPUS[turn % len(_LIVE_CORPUS)]
        out = await skill.run(SkillInput(args={"file_content": doc}))
        summary_text = out.result.get("summary", "")
        q_verdict = quality.grade(
            summary_text, task,
            variant_id=out.result.get("variant"),
        )
        scores.append(q_verdict.score)

        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "session": "s2",
                "turn": turn,
                "variant": out.result.get("variant"),
                "reward": q_verdict.score,
                "summary_preview": summary_text[:100],
            }, ensure_ascii=False) + "\n")

    return scores


@pytest.mark.skipif(
    not os.environ.get("XMC_ANTHROPIC_API_KEY"),
    reason=_SKIP_REASON,
)
@pytest.mark.asyncio
async def test_autonomous_evolution_live() -> None:
    api_key = os.environ["XMC_ANTHROPIC_API_KEY"]
    model = os.environ.get("XMC_BENCH_MODEL", "claude-haiku-4-5-20251001")
    base_url = os.environ.get("XMC_BENCH_BASE_URL")
    llm = AnthropicLLM(api_key=api_key, model=model, base_url=base_url)

    log_path = Path(__file__).parent / "_phase3_live_log.jsonl"
    log_path.write_text("", encoding="utf-8")

    # Start: registry with baseline v1 pinned to a mediocre variant.
    registry = SkillRegistry()
    baseline = next(v for v in DEMO_VARIANTS if v.id == "terse")
    registry.register(
        _FrozenVariantSkill(
            skill_id="demo.sum", version=1, variant=baseline, llm=llm,
        ),
        SkillManifest(id="demo.sum", version=1),
    )

    # ── Session 1 ──
    s1 = await _run_session1(llm, log_path, turns=40)
    s1_mean = statistics.mean(s1.scores)

    # EvolutionController reads observed per-variant means.
    # Relaxed thresholds for live noise: live benches rarely produce the
    # 0.05 gap_over_second the unit defaults require.
    ctrl = EvolutionController(PromotionThresholds(
        min_plays=8,
        min_mean=0.60,
        min_gap_over_head=0.03,
        min_gap_over_second=0.015,
    ))
    evaluations = [
        CandidateEvaluation(
            candidate_id=v.id, version=2, plays=s1.plays_per_variant[v.id],
            mean_score=statistics.mean(s1.scores_per_variant[v.id]),
        )
        for v in DEMO_VARIANTS
        if s1.plays_per_variant[v.id] > 0
    ]
    report = ctrl.consider_promotion(evaluations, head_version=1)

    best_observed = max(evaluations, key=lambda e: e.mean_score)

    print(
        f"\nLIVE PHASE 3.5 — SESSION 1 (40 turns, 6-arm bandit):\n"
        f"  session 1 mean reward:  {s1_mean:.3f}\n"
        f"  arm plays:              {s1.plays_per_variant}\n"
        f"  arm means:              "
        f"{ {v.id: round(statistics.mean(s1.scores_per_variant[v.id]), 3) if s1.scores_per_variant[v.id] else None for v in DEMO_VARIANTS} }\n"
        f"  best observed:          {best_observed.candidate_id!r} "
        f"@ mean={best_observed.mean_score:.3f} plays={best_observed.plays}\n"
        f"  controller decision:    {report.decision.value}\n"
        f"  controller reason:      {report.reason}"
    )

    # Structural sanity: even if controller refuses to promote, the
    # observed-best must NOT be lowball. That would mean the grader
    # couldn't distinguish the bad arm under live noise — a deeper bug.
    assert best_observed.candidate_id != "lowball", (
        f"LIVE FAIL (structural): observed-best is lowball with mean "
        f"{best_observed.mean_score:.3f}. The grader cannot distinguish "
        f"the deliberately-bad arm from legitimate ones under real-LLM "
        f"noise. See {log_path}."
    )

    if report.decision != EvolutionDecision.PROMOTE:
        # Controller refused. That's a valid outcome — live noise might
        # not produce the gap thresholds. Report what we learned and
        # stop; the assertion above already verified the structural
        # sanity (lowball not the "best"). We don't assert session-2
        # improvement because there was no evolution step.
        print(
            f"\nLIVE PHASE 3.5 — controller refused promotion. "
            f"No session-2 run. Test passes on structural gate only "
            f"(best ≠ lowball). See {log_path}."
        )
        return

    # ── Evolution step ──
    winner = next(
        v for v in DEMO_VARIANTS if v.id == report.winner_candidate_id
    )
    registry.register(
        _FrozenVariantSkill(
            skill_id="demo.sum", version=2, variant=winner, llm=llm,
        ),
        SkillManifest(id="demo.sum", version=2),
    )
    registry.promote("demo.sum", 2, evidence=list(report.evidence))
    assert registry.active_version("demo.sum") == 2

    # ── Session 2 ──
    s2_scores = await _run_session2(llm, registry, log_path, turns=15)
    s2_mean = statistics.mean(s2_scores)
    improvement = s2_mean - s1_mean
    ratio = s2_mean / s1_mean if s1_mean else 0.0

    print(
        f"\nLIVE PHASE 3.5 — EVOLUTION + SESSION 2 (15 turns on HEAD):\n"
        f"  winner promoted:        {report.winner_candidate_id!r}\n"
        f"  session 2 mean reward:  {s2_mean:.3f}\n"
        f"  improvement:            {improvement:+.3f} absolute, "
        f"{ratio:.3f}x relative\n"
        f"  promotion evidence:     {list(report.evidence)}\n"
        f"  per-turn log:           {log_path}"
    )

    # Gate: session-2 mean must be at least 1.05× session-1 mean.
    # Chosen conservatively for live noise. The MockLLM test achieves
    # 1.22× (all deterministic), so a real LLM hitting 1.05× is the
    # minimum claim of "evolution delivered non-trivial lift".
    assert ratio >= 1.05, (
        f"LIVE FAIL (improvement): session 2 {s2_mean:.3f} / session 1 "
        f"{s1_mean:.3f} = {ratio:.3f}x, below the 1.05x gate. See {log_path}."
    )
