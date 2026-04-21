"""Cross-session memory integration — anti-req #2 + #9 end-to-end.

Proves the v2 pipeline remembers across sessions:

  Session A:  run N turns. After each, write a MemoryItem to ``long``
              recording (variant, score, summary preview, task id).
              Close Session A.

  Session B:  open the SAME SqliteVecMemory db (different scheduler,
              different grader, different bus). Query ``long`` layer
              by metadata filter. Assert Session A's writes are found.

The mental model: "long" is persistent across agent runs, so when the
scheduler in Session B starts cold, it can bootstrap its arm priors
from the grader evidence it wrote in Session A. This bench doesn't
wire the bootstrap path (that's Phase 2.5); it proves the memory
substrate actually preserves data across scheduler instances.
"""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
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
from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory
from xmclaw.skills.base import SkillInput
from xmclaw.skills.demo.read_and_summarize import DEMO_VARIANTS, LiveReadAndSummarize


_SAMPLE_DOC = (
    "Photosynthesis is the process by which green plants use sunlight, "
    "water, and carbon dioxide to produce oxygen and glucose. It occurs "
    "primarily in chloroplasts."
)

_TASK = SummaryTask(
    file_id="photosynthesis",
    reference_keywords=("sunlight", "oxygen", "glucose", "chloroplast"),
    target_words=25,
    target_words_tol=0.6,
)


@dataclass
class _PredictableMockLLM(LLMProvider):
    """Returns a fixed canned text per variant — deterministic, no network."""

    canned: dict[str, str] = field(default_factory=dict)

    async def stream(  # pragma: no cover
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
        suffix = messages[-1].content
        variant_id = "terse"
        for v in DEMO_VARIANTS:
            if v.prompt_suffix in suffix:
                variant_id = v.id
                break
        return LLMResponse(
            content=self.canned.get(variant_id, f"<{variant_id} canned>"),
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


def _canned() -> dict[str, str]:
    return {
        "bullets": (
            "- Plants use sunlight to make glucose.\n"
            "- Oxygen is a byproduct.\n"
            "- The reaction lives in chloroplasts."
        ),
        "exec":    "Plants use sunlight, water, and CO2 in chloroplasts to produce glucose and release oxygen as a byproduct.",
        "terse":   "Photosynthesis converts sunlight, water and CO2 into glucose and oxygen in chloroplasts.",
        "tl;dr":   "TL;DR: plants eat sunlight to make oxygen and glucose.",
        "verbose": "Photosynthesis is a biochemical cascade occurring in chloroplasts. " * 15,
        "lowball": "ok",
    }


async def _run_session(
    *,
    mem: SqliteVecMemory,
    session_id: str,
    turns: int,
    seed_arm_priors: dict[str, float] | None = None,
) -> dict[str, int]:
    """Run ``turns`` learning-loop turns, writing a MemoryItem each turn.

    Returns a dict: variant_id -> plays in this session.
    """
    bus = InProcessEventBus()
    structural = HonestGrader()
    quality = SummaryQualityGrader(require_structure=True)
    llm = _PredictableMockLLM(canned=_canned())

    candidates = [
        Candidate(skill_id=v.id, version=1, prompt_delta={}, evidence=[])
        for v in DEMO_VARIANTS
    ]
    scheduler = OnlineScheduler(candidates=candidates, exploration_c=0.5)

    # If seeded with priors from a previous session, warm the arm stats.
    if seed_arm_priors:
        for i, v in enumerate(DEMO_VARIANTS):
            prior = seed_arm_priors.get(v.id)
            if prior is not None:
                scheduler._stats[i].plays = 1
                scheduler._stats[i].total_reward = prior

    async def on_verdict(event) -> None:  # noqa: ANN001
        await scheduler.on_event(event)

    bus.subscribe(
        lambda e: e.type == EventType.GRADER_VERDICT, on_verdict,
    )

    plays: dict[str, int] = {v.id: 0 for v in DEMO_VARIANTS}
    for turn in range(turns):
        idx = scheduler.pick()
        v = DEMO_VARIANTS[idx]
        plays[v.id] += 1

        skill = LiveReadAndSummarize(variant=v, llm=llm)
        out = await skill.run(SkillInput(args={"file_content": _SAMPLE_DOC}))

        finished = make_event(
            session_id=session_id, agent_id="cross-session",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "call_id": f"{session_id}-t{turn}",
                "result": out.result,
                "error": None,
                "expected_type": "dict",
                "expected_side_effects": [],
                "candidate_idx": idx,
            },
        )
        s_verdict = await structural.grade(finished)
        summary_text = out.result.get("summary", "") if out.ok else ""
        q_verdict = quality.grade(summary_text, _TASK, variant_id=v.id)
        reward = q_verdict.score if (s_verdict.ran and s_verdict.returned) else 0.0

        await bus.publish(make_event(
            session_id=session_id, agent_id="cross-session",
            type=EventType.GRADER_VERDICT,
            payload={"candidate_idx": idx, "score": reward},
        ))
        await bus.drain()

        # === the new bit: write what we just learned to long-memory ===
        await mem.put("long", MemoryItem(
            id="",  # generate
            layer="long",
            text=f"variant={v.id} reward={reward:.3f} summary={summary_text[:80]}",
            metadata={
                "session_id": session_id,
                "turn": turn,
                "variant": v.id,
                "reward": reward,
                "task_file_id": _TASK.file_id,
            },
        ))
    return plays


@pytest.mark.asyncio
async def test_session_writes_survive_into_a_second_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "xmc_long.db"

        # ── Session A ────────────────────────────────────────────────
        mem_a = SqliteVecMemory(db_path)
        plays_a = await _run_session(mem=mem_a, session_id="A", turns=10)
        total_a = sum(plays_a.values())
        assert total_a == 10

        # Verify A wrote exactly 10 entries with session_id=A
        from_a = await mem_a.query("long", filters={"session_id": "A"}, k=100)
        assert len(from_a) == 10
        mem_a.close()

        # ── Session B opens the SAME db ──────────────────────────────
        mem_b = SqliteVecMemory(db_path)

        # B can query A's writes — anti-req #2 cross-session memory.
        still_from_a = await mem_b.query("long", filters={"session_id": "A"}, k=100)
        assert len(still_from_a) == 10

        # B can ALSO filter by variant — retrieving only "bullets" entries
        bullets_only = await mem_b.query(
            "long", filters={"variant": "bullets"}, k=100,
        )
        # At least one bullets entry must have been written in Session A
        assert all(r.metadata["variant"] == "bullets" for r in bullets_only)
        assert len(bullets_only) >= 1

        # And B's own writes are isolated by session_id
        await _run_session(mem=mem_b, session_id="B", turns=5)
        from_b_only = await mem_b.query("long", filters={"session_id": "B"}, k=100)
        assert len(from_b_only) == 5
        # A's entries are still there, untouched
        from_a_after = await mem_b.query("long", filters={"session_id": "A"}, k=100)
        assert len(from_a_after) == 10

        mem_b.close()


@pytest.mark.asyncio
async def test_seeding_from_memory_accelerates_convergence() -> None:
    """Proves the memory substrate carries real signal, not just rows.

    Session A: 20 turns cold-start. Measure final arm means.
    Session B: cold-start scheduler SEEDED with per-arm means from A.
    Run 10 turns. Best arm should already be well-identified at turn 10.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "xmc_seeded.db"

        # Session A: cold start
        mem_a = SqliteVecMemory(db_path)
        await _run_session(mem=mem_a, session_id="A", turns=20)

        # Recover arm-level means from memory (averaging reward per variant)
        priors: dict[str, list[float]] = {}
        rows = await mem_a.query("long", filters={"session_id": "A"}, k=100)
        for r in rows:
            vid = r.metadata["variant"]
            priors.setdefault(vid, []).append(float(r.metadata["reward"]))
        prior_means = {
            k: sum(v) / len(v) for k, v in priors.items() if v
        }
        mem_a.close()

        # Session B: fresh scheduler, but seed arm_stats from prior_means
        mem_b = SqliteVecMemory(db_path)
        plays_b = await _run_session(
            mem=mem_b, session_id="B", turns=10,
            seed_arm_priors=prior_means,
        )
        mem_b.close()

        # The seeded scheduler's ``bullets`` and ``exec`` arms get picked
        # more than ``lowball`` or ``verbose`` — exploitation kicks in
        # immediately because of the prior. A cold-start 10-turn run would
        # spend most of the budget on forced exploration.
        good_plays = plays_b.get("bullets", 0) + plays_b.get("exec", 0)
        bad_plays = plays_b.get("lowball", 0) + plays_b.get("verbose", 0)
        assert good_plays >= bad_plays, (
            f"seeded scheduler failed to exploit prior — good={good_plays} "
            f"bad={bad_plays} all={plays_b} priors={prior_means}"
        )
