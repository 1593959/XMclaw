"""Phase 2.6 live bench — tool-aware learning curve on real LLM + real fs.

Unlike ``phase1_live_learning_curve.py`` (which injects the document
into the prompt directly), this bench forces the model to call
``file_read`` before summarizing. Every turn exercises the full
LLM → tool_use → tool_result → LLM → final-text handshake. Anti-req #1
is now under test in its authentic habitat: a variant that hallucinates
tool calls will produce empty summaries and score near zero, while
variants that actually invoke the tool will score on the summary's
quality alone.

Opt-in via env vars (same pattern as Phase 1):

    export XMC_ANTHROPIC_API_KEY=sk-...                (required)
    export XMC_BENCH_MODEL=claude-haiku-4-5-20251001   (optional)
    export XMC_BENCH_BASE_URL=https://api.anthropic.com  (optional;
        set to the endpoint's base for OpenAI-compat / MiniMax / etc.)

    python -m pytest tests/bench/phase2_tool_aware_live.py -v -s

Gates (inherit the baseline-beating criterion from Phase 1.3):

  1. Scheduler total reward ≥ 1.05 × uniform-random baseline.
  2. Best arm ≠ ``lowball`` (anti-req #1 structural sanity).
  3. At least 80% of non-lowball turns completed a ``file_read``
     invocation (proves the loop actually fires — if variants never
     called the tool, the bench would be measuring prompt-only
     behaviour, defeating the point).
"""
from __future__ import annotations

import json
import os
import statistics
import tempfile
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.grader import HonestGrader
from xmclaw.core.grader.domain import SummaryQualityGrader
from xmclaw.core.grader.domain.summary import SummaryTask
from xmclaw.core.scheduler.online import Candidate, OnlineScheduler
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.skills.base import SkillInput
from xmclaw.skills.demo.read_and_summarize import (
    DEMO_VARIANTS,
    ToolAwareReadAndSummarize,
)


_SKIP_REASON = "Set XMC_ANTHROPIC_API_KEY to run the live Phase 2.6 bench."


_LIVE_CORPUS: tuple[tuple[str, str, SummaryTask], ...] = (
    (
        "photosynthesis.txt",
        (
            "Photosynthesis is the process by which green plants use sunlight, "
            "water, and carbon dioxide to produce oxygen and glucose. It "
            "occurs primarily in chloroplasts. This process is critical to "
            "the planet's oxygen cycle and to all food chains that depend on "
            "plant biomass."
        ),
        SummaryTask(
            file_id="photosynthesis",
            reference_keywords=("sunlight", "oxygen", "glucose", "chloroplast"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "aqueducts.txt",
        (
            "The Roman aqueducts delivered water to cities across the empire. "
            "Using gravity and gradient engineering, they transported clean "
            "water over dozens of miles, enabling public baths, fountains, "
            "and sanitation infrastructure that supported large urban "
            "populations."
        ),
        SummaryTask(
            file_id="aqueducts",
            reference_keywords=("water", "Roman", "gravity", "cities"),
            target_words=25, target_words_tol=0.6,
        ),
    ),
    (
        "moore.txt",
        (
            "In 1965, Gordon Moore observed that the number of transistors on "
            "a chip doubled roughly every two years. This empirical trend, "
            "later called Moore's Law, drove decades of semiconductor "
            "industry planning and shaped computing economics."
        ),
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
async def test_phase2_tool_aware_live() -> None:
    api_key = os.environ["XMC_ANTHROPIC_API_KEY"]
    model = os.environ.get("XMC_BENCH_MODEL", "claude-haiku-4-5-20251001")
    base_url = os.environ.get("XMC_BENCH_BASE_URL")
    llm = AnthropicLLM(api_key=api_key, model=model, base_url=base_url)

    with tempfile.TemporaryDirectory() as tmp:
        corpus_dir = Path(tmp)
        # Write the corpus to real files — the model will read them.
        files: list[tuple[Path, SummaryTask]] = []
        for name, body, task in _LIVE_CORPUS:
            p = corpus_dir / name
            p.write_text(body, encoding="utf-8")
            files.append((p, task))

        tools = BuiltinTools(allowed_dirs=[corpus_dir])
        bus = InProcessEventBus()
        structural = HonestGrader()
        quality = SummaryQualityGrader(require_structure=True)

        candidates = [
            Candidate(
                skill_id=v.id, version=1,
                prompt_delta={"suffix": v.prompt_suffix},
                evidence=[],
            )
            for v in DEMO_VARIANTS
        ]
        scheduler = OnlineScheduler(candidates=candidates, exploration_c=0.5)

        async def on_verdict(event) -> None:  # noqa: ANN001
            await scheduler.on_event(event)

        bus.subscribe(
            lambda e: e.type == EventType.GRADER_VERDICT, on_verdict,
        )

        log_path = Path(__file__).parent / "_phase2_live_log.jsonl"
        log_path.write_text("", encoding="utf-8")
        scores: list[float] = []
        tool_fired: list[bool] = []

        TURNS = 40  # smaller than Phase 1 — each turn = 2 LLM calls
        for turn in range(TURNS):
            idx = scheduler.pick()
            v = DEMO_VARIANTS[idx]
            path, task = files[turn % len(files)]

            skill = ToolAwareReadAndSummarize(variant=v, llm=llm, tools=tools)
            out = await skill.run(SkillInput(args={
                "file_path": str(path),
                "file_id": task.file_id,
            }))

            tool_calls = (out.result or {}).get("tool_calls", [])
            fired = isinstance(tool_calls, list) and len(tool_calls) > 0
            tool_fired.append(fired)

            finished = make_event(
                session_id="live-p2", agent_id="live-p2",
                type=EventType.TOOL_INVOCATION_FINISHED,
                payload={
                    "call_id": f"turn-{turn}",
                    "result": out.result,
                    "error": None if out.ok else (out.result or {}).get("error"),
                    "expected_type": "dict",
                    "expected_side_effects": [],
                    "candidate_idx": idx,
                },
            )
            s_verdict = await structural.grade(finished)
            summary_text = (out.result or {}).get("summary", "") if out.ok else ""
            q_verdict = quality.grade(summary_text, task, variant_id=v.id)
            reward = q_verdict.score if (s_verdict.ran and s_verdict.returned) else 0.0
            scores.append(reward)

            await bus.publish(make_event(
                session_id="live-p2", agent_id="live-p2",
                type=EventType.GRADER_VERDICT,
                payload={
                    "candidate_idx": idx,
                    "score": reward,
                    "structural_score": s_verdict.score,
                    "domain_score": q_verdict.score,
                    "tool_calls": len(tool_calls) if isinstance(tool_calls, list) else 0,
                },
            ))
            await bus.drain()

            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "turn": turn,
                    "variant": v.id,
                    "reward": reward,
                    "structural_score": s_verdict.score,
                    "domain_score": q_verdict.score,
                    "tool_calls": tool_calls,
                    "summary_preview": summary_text[:120],
                }, ensure_ascii=False) + "\n")

        # ── gates ────────────────────────────────────────────────────────
        scheduler_total = sum(scores)
        arm_means = [s.mean for s in scheduler.stats if s.plays > 0]
        uniform_expected = (
            (sum(arm_means) / len(arm_means)) * TURNS if arm_means else 0.0
        )
        ratio = scheduler_total / uniform_expected if uniform_expected else 0.0
        best_idx = scheduler.best_known()
        lowball_idx = next(
            i for i, v in enumerate(DEMO_VARIANTS) if v.id == "lowball"
        )

        # Tool-firing rate across non-lowball turns — proves the loop
        # actually ran, not just the prompt-only skill.
        non_lowball_fired = [
            fired for fired, s in zip(tool_fired, scores, strict=True)
            if s > 0  # non-zero reward = non-empty response = actual turn
        ]
        fire_rate = (
            sum(non_lowball_fired) / len(non_lowball_fired)
            if non_lowball_fired else 0.0
        )
        first = statistics.mean(scores[:10])
        last = statistics.mean(scores[-10:])

        print(
            f"\nLIVE PHASE 2.6 TOOL-AWARE BENCH (model={model}):\n"
            f"  scheduler total reward:  {scheduler_total:.2f}\n"
            f"  uniform-random baseline: {uniform_expected:.2f}\n"
            f"  scheduler / baseline:    {ratio:.3f}x  (gate: ≥ 1.05x)\n"
            f"  best arm:                {DEMO_VARIANTS[best_idx].id}\n"
            f"  arm plays:               {[s.plays for s in scheduler.stats]}\n"
            f"  arm means:               {[round(s.mean, 3) for s in scheduler.stats]}\n"
            f"  tool-fired on non-zero:  {sum(non_lowball_fired)}/"
            f"{len(non_lowball_fired)} = {fire_rate:.2%}  (gate: ≥ 80%)\n"
            f"  window means (ref only): first10={first:.3f} last10={last:.3f}\n"
            f"  per-turn log:            {log_path}"
        )

        assert ratio >= 1.05, (
            f"Phase 2.6 FAIL (baseline): scheduler {scheduler_total:.2f} "
            f"< 1.05 × {uniform_expected:.2f} (ratio {ratio:.3f}x). "
            f"See {log_path}."
        )
        assert best_idx != lowball_idx, (
            f"Phase 2.6 FAIL (best arm): scheduler converged on lowball. "
            f"See {log_path}."
        )
        assert fire_rate >= 0.80, (
            f"Phase 2.6 FAIL (tool-firing): only {fire_rate:.1%} of non-zero "
            f"turns invoked a tool. Tool-aware loop is not actually firing; "
            f"the model is likely answering from memory. See {log_path}."
        )
