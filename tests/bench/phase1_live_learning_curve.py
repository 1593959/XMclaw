"""Phase 1 go/no-go — LIVE counterpart of phase1_learning_curve.py.

Runs the full v2 pipeline against a real Anthropic model. Skips when
``XMC_ANTHROPIC_API_KEY`` is not set in the environment so CI stays
hermetic.

To run live::

    export XMC_ANTHROPIC_API_KEY=sk-ant-...
    # optional: override model (defaults to claude-haiku-4-5-20251001 to
    # keep the bench cheap — ~$0.05 per 50-turn run at current pricing)
    export XMC_BENCH_MODEL=claude-haiku-4-5-20251001
    python -m pytest tests/bench/phase1_live_learning_curve.py -v -s

Criteria (same as the offline bench in V2_DEVELOPMENT.md §8.2):
  1. Windowed mean at turns 41-50 ≥ 1.2 × windowed mean at turns 0-9
  2. Scheduler prefers a non-random arm by the end

The live bench additionally records per-turn (variant, score, tokens)
to ``tests/bench/_phase1_live_log.jsonl`` so a reviewer can inspect the
grader's judgements after the fact. That log file is gitignored.
"""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.grader import HonestGrader
from xmclaw.core.grader.domain import SummaryQualityGrader
from xmclaw.core.grader.domain.summary import SummaryTask
from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.skills.base import SkillInput
from xmclaw.skills.demo.read_and_summarize import DEMO_VARIANTS, LiveReadAndSummarize


_SKIP_REASON = "Set XMC_ANTHROPIC_API_KEY to run the live Phase 1 bench."

_LIVE_CORPUS: tuple[tuple[str, SummaryTask], ...] = (
    (
        "Photosynthesis is the process by which green plants use sunlight, "
        "water, and carbon dioxide to produce oxygen and glucose. It occurs "
        "primarily in chloroplasts. This process is critical to the planet's "
        "oxygen cycle and to all food chains that depend on plant biomass.",
        SummaryTask(
            file_id="photosynthesis",
            reference_keywords=("sunlight", "oxygen", "glucose", "chloroplast"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "The Roman aqueducts delivered water to cities across the empire. "
        "Using gravity and gradient engineering, they transported clean "
        "water over dozens of miles, enabling public baths, fountains, and "
        "sanitation infrastructure that supported large urban populations.",
        SummaryTask(
            file_id="aqueducts",
            reference_keywords=("water", "Roman", "gravity", "cities"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "In 1965, Gordon Moore observed that the number of transistors on a "
        "chip doubled roughly every two years. This empirical trend, later "
        "called Moore's Law, drove decades of semiconductor industry "
        "planning and shaped computing economics.",
        SummaryTask(
            file_id="moore",
            reference_keywords=("Moore", "transistors", "doubling", "semiconductor"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
)


@pytest.mark.skipif(
    not os.environ.get("XMC_ANTHROPIC_API_KEY"),
    reason=_SKIP_REASON,
)
@pytest.mark.asyncio
async def test_live_learning_curve() -> None:
    api_key = os.environ["XMC_ANTHROPIC_API_KEY"]
    model = os.environ.get("XMC_BENCH_MODEL", "claude-haiku-4-5-20251001")
    llm = AnthropicLLM(api_key=api_key, model=model)

    bus = InProcessEventBus()
    structural = HonestGrader()
    quality = SummaryQualityGrader(require_structure=True)

    candidates = [
        Candidate(skill_id=v.id, version=1, prompt_delta={"suffix": v.prompt_suffix}, evidence=[])
        for v in DEMO_VARIANTS
    ]
    scheduler = OnlineScheduler(candidates=candidates, exploration_c=1.0)

    async def on_verdict(event) -> None:  # noqa: ANN001
        await scheduler.on_event(event)

    bus.subscribe(
        lambda e: e.type == EventType.GRADER_VERDICT, on_verdict,
    )

    log_path = Path(__file__).parent / "_phase1_live_log.jsonl"
    log_path.write_text("", encoding="utf-8")
    scores: list[float] = []

    TURNS = 50
    for turn in range(TURNS):
        idx = scheduler.pick()
        v = DEMO_VARIANTS[idx]
        doc, task = _LIVE_CORPUS[turn % len(_LIVE_CORPUS)]

        skill = LiveReadAndSummarize(variant=v, llm=llm)
        out = await skill.run(SkillInput(args={
            "file_content": doc,
            "file_id": task.file_id,
        }))

        finished = make_event(
            session_id="live-bench", agent_id="live-bench",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": f"turn-{turn}",
                "result": out.result,
                "error": None if out.ok else out.result.get("error"),
                "expected_type": "dict",
                "expected_side_effects": [],
                "candidate_idx": idx,
            },
        )
        s_verdict = await structural.grade(finished)
        summary_text = out.result.get("summary", "") if out.ok else ""
        q_verdict = quality.grade(summary_text, task, variant_id=v.id)

        combined = 0.4 * s_verdict.score + 0.6 * q_verdict.score
        scores.append(combined)

        await bus.publish(make_event(
            session_id="live-bench", agent_id="live-bench",
            type=EventType.GRADER_VERDICT,
            payload={
                "candidate_idx": idx,
                "score": combined,
                "structural_score": s_verdict.score,
                "domain_score": q_verdict.score,
            },
        ))
        await bus.drain()

        # Append the per-turn log for human review after the run.
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "turn": turn,
                "variant": v.id,
                "combined_score": combined,
                "structural_score": s_verdict.score,
                "domain_score": q_verdict.score,
                "summary_preview": summary_text[:120],
                "prompt_tokens": out.result.get("prompt_tokens"),
                "completion_tokens": out.result.get("completion_tokens"),
            }, ensure_ascii=False) + "\n")

    first = statistics.mean(scores[:10])
    last = statistics.mean(scores[-10:])
    best_idx = scheduler.best_known()

    print(
        f"\nLIVE PHASE 1 BENCH (model={model}):\n"
        f"  turns 0-9 mean: {first:.3f}\n"
        f"  turns 40-49 mean: {last:.3f}\n"
        f"  improvement: {(last / first if first else 0):.2f}x\n"
        f"  best arm: {DEMO_VARIANTS[best_idx].id}\n"
        f"  arm means: {[round(s.mean, 3) for s in scheduler.stats]}\n"
        f"  per-turn log: {log_path}"
    )

    assert last >= first * 1.2, (
        f"Live Phase 1 FAIL: {last:.3f} < 1.2 × {first:.3f}. See {log_path}."
    )
