"""Sprint 3 #5 follow-up — Iron Rule #3 wiring into distiller + mutator.

The base #5 commit (`e19fc8d`) shipped ``classify_model_tier()``.
This file pins the wiring at the actual decision points: when
``evolution_tier == "weak"``, both StrategyDistiller and
ReflectiveMutator return ``[]`` immediately without an LLM call.
The caller (daemon factory) is responsible for classifying the
LLM's model id and passing the tier through the constructor —
``core/`` stays free of ``providers/`` imports.

Iron Rule #3 (`docs/EVOLUTION_HONEST_STATE.md`):

    "Per-model capability profile: Strong models (Claude / Opus) get
    self-extension prompts; weak models (GPT-5 / 7B) get template-fill
    mode. Don't silently downgrade the loop because the user picked
    GPT-5 instead of Claude."
"""
from __future__ import annotations

from typing import Any

import pytest

from xmclaw.core.evolution.reflective_mutator import ReflectiveMutator
from xmclaw.core.journal.strategy_distiller import StrategyDistiller


# ── shared fakes ──────────────────────────────────────────────────


class _CountingLLM:
    """LLM that records every prompt it sees so tests can assert
    whether the distiller / mutator actually called the LLM (proof
    that the gate either fired or didn't)."""

    def __init__(self, response: str = "[]") -> None:
        self.calls: list[str] = []
        self._response = response

    async def complete(self, prompt: str, **_kw: Any) -> str:
        self.calls.append(prompt)
        return self._response


# ── StrategyDistiller × tier ──────────────────────────────────────


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_weak_tier_skips_llm() -> None:
    """``evolution_tier="weak"`` short-circuits before any LLM call."""
    llm = _CountingLLM(response='[{"when_pattern":"x","then_action":"y","evidence_count":3,"evidence_session_ids":["s1","s2","s3"],"confidence":0.5}]')
    distiller = StrategyDistiller(llm, evolution_tier="weak")
    out = await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}] * 5,
    )
    assert out == []
    assert llm.calls == [], (
        f"weak-tier distiller called LLM {len(llm.calls)} times — "
        "Iron Rule #3 wiring isn't gating"
    )


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_strong_tier_calls_llm() -> None:
    """``evolution_tier="strong"`` runs the LLM call (existing behavior)."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm, evolution_tier="strong")
    await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_medium_tier_calls_llm() -> None:
    """``evolution_tier="medium"`` runs the LLM call (only weak skips)."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm, evolution_tier="medium")
    await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_unknown_tier_calls_llm() -> None:
    """``evolution_tier="unknown"`` is treated as medium (run the LLM)."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm, evolution_tier="unknown")
    await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_default_tier_is_unknown() -> None:
    """When the constructor doesn't get evolution_tier, default to
    ``"unknown"`` — caller hasn't classified yet, so don't gate."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm)  # no evolution_tier
    await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_invalid_tier_falls_to_unknown() -> None:
    """A typo / bogus tier value falls back to ``"unknown"`` (run LLM)
    rather than silently defaulting to ``"weak"`` (which would be a
    nasty hidden behavior change)."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm, evolution_tier="bogus")
    await distiller.distill_from_journal(
        [{"session_id": "a", "summary": "did stuff"}],
    )
    assert len(llm.calls) == 1


# ── ReflectiveMutator × tier ──────────────────────────────────────


@pytest.mark.asyncio
async def test_iron_rule_3_mutator_weak_tier_skips_llm() -> None:
    """``evolution_tier="weak"`` short-circuits propose_mutations."""
    llm = _CountingLLM(response='[]')
    mutator = ReflectiveMutator(llm, evolution_tier="weak")
    out = await mutator.propose_mutations(
        skill_id="my_skill",
        head_source="def run(): return 1\n",
        recent_failures=[
            {"session_id": "a", "error": "boom"},
            {"session_id": "b", "error": "boom"},
        ],
    )
    assert out == []
    assert llm.calls == [], (
        "weak-tier mutator called LLM — Iron Rule #3 wiring isn't gating"
    )


@pytest.mark.asyncio
async def test_iron_rule_3_mutator_strong_tier_calls_llm() -> None:
    llm = _CountingLLM(response='[]')
    mutator = ReflectiveMutator(llm, evolution_tier="strong")
    await mutator.propose_mutations(
        skill_id="my_skill",
        head_source="def run(): return 1\n",
        recent_failures=[{"session_id": "a", "error": "boom"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_mutator_medium_tier_calls_llm() -> None:
    llm = _CountingLLM(response='[]')
    mutator = ReflectiveMutator(llm, evolution_tier="medium")
    await mutator.propose_mutations(
        skill_id="my_skill",
        head_source="def run(): return 1\n",
        recent_failures=[{"session_id": "a", "error": "boom"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_mutator_default_tier_is_unknown() -> None:
    """Default constructor (no evolution_tier) → unknown → run LLM."""
    llm = _CountingLLM(response='[]')
    mutator = ReflectiveMutator(llm)
    await mutator.propose_mutations(
        skill_id="my_skill",
        head_source="def run(): return 1\n",
        recent_failures=[{"session_id": "a", "error": "boom"}],
    )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_iron_rule_3_mutator_empty_failures_still_short_circuits() -> None:
    """Empty failure list short-circuits BEFORE the tier gate — no
    LLM call regardless of tier. (Existing behavior; tier gate is
    additive, not a replacement.)"""
    llm = _CountingLLM(response='[]')
    mutator = ReflectiveMutator(llm, evolution_tier="strong")
    out = await mutator.propose_mutations(
        skill_id="my_skill",
        head_source="def run(): return 1\n",
        recent_failures=[],   # empty
    )
    assert out == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_iron_rule_3_distiller_empty_journal_still_short_circuits() -> None:
    """Empty journal window short-circuits BEFORE the tier gate."""
    llm = _CountingLLM(response='[]')
    distiller = StrategyDistiller(llm, evolution_tier="strong")
    out = await distiller.distill_from_journal([])  # empty
    assert out == []
    assert llm.calls == []
