"""Sprint 3 #2 — GEPA-style reflective mutator + per-context Pareto frontier.

Coverage targets the public contract documented in
``xmclaw/core/evolution/reflective_mutator.py`` and
``xmclaw/core/evolution/pareto_frontier.py``. Integration into the
EvolutionController / MutationOrchestrator is *deferred* — these tests
exercise the modules in isolation, which is the level Sprint 3 #2
ships.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from xmclaw.core.evolution.pareto_frontier import (
    FrontierEntry,
    ParetoFrontier,
)
from xmclaw.core.evolution.reflective_mutator import (
    MutationCandidate,
    ReflectiveMutator,
)


# ── Helpers ────────────────────────────────────────────────────────────


class _FakeLLM:
    """Minimal LLM-shaped mock with a fixed response and call counter."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.last_prompt: str | None = None

    async def acomplete(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self.response


def _failure(task: str = "do x", note: str = "missed") -> dict:
    return {
        "task_input": task,
        "grader_verdict": "fail",
        "note": note,
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────
# MutationCandidate field shape + immutability
# ─────────────────────────────────────────────────────────────────────


def test_mutation_candidate_has_required_fields():
    c = MutationCandidate(
        skill_id="s1",
        parent_version=2,
        proposed_source="body",
        reflection_summary="why",
        confidence=0.4,
        created_at=time.time(),
    )
    assert c.skill_id == "s1"
    assert c.parent_version == 2
    assert c.proposed_source == "body"
    assert c.reflection_summary == "why"
    assert c.confidence == 0.4
    assert c.created_at > 0


def test_mutation_candidate_is_frozen():
    c = MutationCandidate(
        skill_id="s",
        parent_version=0,
        proposed_source="b",
        reflection_summary="r",
        confidence=0.1,
        created_at=1.0,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        c.confidence = 0.9  # type: ignore[misc]


def test_confidence_cap_capped_at_0_6_via_mutator():
    """Even when the LLM claims 0.99, the mutator clamps to <= 0.6."""
    payload = json.dumps(
        [
            {
                "proposed_source": "new body",
                "reflection_summary": "tighter wording",
                "confidence": 0.99,
            }
        ]
    )
    mut = ReflectiveMutator(_FakeLLM(payload))
    cands = _run(mut.propose_mutations("s1", "old", [_failure()]))
    assert len(cands) == 1
    assert cands[0].confidence <= 0.6
    # Should saturate exactly at the cap.
    assert cands[0].confidence == pytest.approx(0.6)


# ─────────────────────────────────────────────────────────────────────
# ReflectiveMutator: malformed JSON, single round-trip, drop rules
# ─────────────────────────────────────────────────────────────────────


def test_mutator_drops_malformed_json_entries_cleanly():
    """Mix of valid + invalid entries: invalid silently dropped."""
    payload = json.dumps(
        [
            {"proposed_source": "good", "reflection_summary": "x", "confidence": 0.3},
            {"reflection_summary": "no source field"},  # missing proposed_source
            {"proposed_source": 12345, "confidence": 0.5},  # wrong type
            {"proposed_source": "", "confidence": 0.5},  # empty
            {"proposed_source": "ok2", "reflection_summary": "y", "confidence": "not_a_number"},
            {"proposed_source": "ok3", "reflection_summary": "z", "confidence": 0.2},
        ]
    )
    mut = ReflectiveMutator(_FakeLLM(payload), max_per_skill=10)
    cands = _run(mut.propose_mutations("s", "head", [_failure()]))
    sources = {c.proposed_source for c in cands}
    assert "good" in sources
    assert "ok3" in sources
    # The 12345 / empty / not_a_number entries are dropped.
    assert "" not in sources


def test_mutator_recovers_json_from_code_fence():
    """Real LLMs often wrap output in ```json fences; we handle that."""
    payload = (
        "```json\n"
        '[{"proposed_source": "fenced", "reflection_summary": "r", "confidence": 0.2}]'
        "\n```"
    )
    mut = ReflectiveMutator(_FakeLLM(payload))
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    assert len(cands) == 1
    assert cands[0].proposed_source == "fenced"


def test_mutator_recovers_json_from_prose_preamble():
    """Some LLMs add a paragraph before the array; we strip it."""
    payload = (
        "Sure, here's my analysis: the issue is X.\n\n"
        '[{"proposed_source": "after_prose", "reflection_summary": "r", "confidence": 0.1}]'
    )
    mut = ReflectiveMutator(_FakeLLM(payload))
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    assert len(cands) == 1
    assert cands[0].proposed_source == "after_prose"


def test_mutator_returns_empty_on_unparseable_response():
    """Total garbage in → empty list out, no exception."""
    mut = ReflectiveMutator(_FakeLLM("totally not json at all"))
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    assert cands == []


def test_mutator_caps_confidence_at_0_6_uniformly():
    """A range of overshoots all clamp to <= 0.6; in-range pass through."""
    payload = json.dumps(
        [
            {"proposed_source": "a", "reflection_summary": "", "confidence": 1.0},
            {"proposed_source": "b", "reflection_summary": "", "confidence": 0.61},
            {"proposed_source": "c", "reflection_summary": "", "confidence": 0.5},
        ]
    )
    mut = ReflectiveMutator(_FakeLLM(payload), max_per_skill=10)
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    confs = sorted(c.confidence for c in cands)
    assert all(c <= 0.6 for c in confs)
    assert pytest.approx(0.5) in confs


def test_mutator_uses_single_llm_round_trip():
    """One propose_mutations call → exactly one LLM call."""
    payload = json.dumps(
        [{"proposed_source": "a", "reflection_summary": "r", "confidence": 0.3}]
    )
    fake = _FakeLLM(payload)
    mut = ReflectiveMutator(fake)
    _run(mut.propose_mutations("s", "h", [_failure(), _failure(), _failure()]))
    assert fake.calls == 1


def test_mutator_swallows_llm_exceptions():
    """LLM raises → empty list, never propagates."""

    class _ExplodingLLM:
        async def acomplete(self, prompt: str) -> str:  # noqa: ARG002
            raise RuntimeError("boom")

    mut = ReflectiveMutator(_ExplodingLLM())
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    assert cands == []


def test_mutator_supports_sync_complete_method():
    """Plain (non-async) complete() also works — we adapt."""

    class _SyncLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str) -> str:  # noqa: ARG002
            self.calls += 1
            return json.dumps(
                [
                    {
                        "proposed_source": "sync_ok",
                        "reflection_summary": "r",
                        "confidence": 0.2,
                    }
                ]
            )

    llm = _SyncLLM()
    cands = _run(ReflectiveMutator(llm).propose_mutations("s", "h", [_failure()]))
    assert llm.calls == 1
    assert len(cands) == 1
    assert cands[0].proposed_source == "sync_ok"


# ─────────────────────────────────────────────────────────────────────
# Empty/edge inputs
# ─────────────────────────────────────────────────────────────────────


def test_empty_failure_list_short_circuits_to_empty():
    """No failures → no LLM call, no candidates."""
    fake = _FakeLLM("[]")
    mut = ReflectiveMutator(fake)
    cands = _run(mut.propose_mutations("s", "h", []))
    assert cands == []
    assert fake.calls == 0  # didn't burn a call


# ─────────────────────────────────────────────────────────────────────
# Per-skill cap respected
# ─────────────────────────────────────────────────────────────────────


def test_per_skill_cap_respected():
    """Even if LLM hallucinates 5 entries, max_per_skill=2 yields 2."""
    payload = json.dumps(
        [
            {"proposed_source": f"v{i}", "reflection_summary": "r", "confidence": 0.1}
            for i in range(5)
        ]
    )
    mut = ReflectiveMutator(_FakeLLM(payload), max_per_skill=2)
    cands = _run(mut.propose_mutations("s", "h", [_failure()]))
    assert len(cands) == 2


# ─────────────────────────────────────────────────────────────────────
# ParetoFrontier.add — non-dominated vs dominated
# ─────────────────────────────────────────────────────────────────────


def _entry(
    skill: str = "s1",
    version: int = 1,
    ctx: str = "default",
    score: float = 0.5,
    evidence: int = 10,
    t: float | None = None,
) -> FrontierEntry:
    return FrontierEntry(
        skill_id=skill,
        version=version,
        context_signature=ctx,
        grader_score=score,
        evidence_count=evidence,
        added_at=t if t is not None else time.time(),
    )


def test_frontier_add_returns_true_for_non_dominated_first_entry():
    pf = ParetoFrontier()
    e = _entry()
    assert pf.add(e) is True
    assert pf.all_for("s1") == [e]


def test_frontier_add_returns_false_for_dominated_entry():
    """Strictly worse on both axes is dominated."""
    pf = ParetoFrontier()
    strong = _entry(version=1, score=0.9, evidence=50, t=1.0)
    weak = _entry(version=2, score=0.5, evidence=10, t=2.0)
    assert pf.add(strong) is True
    assert pf.add(weak) is False
    # The weak entry is NOT in the frontier.
    versions = [x.version for x in pf.all_for("s1")]
    assert 1 in versions
    assert 2 not in versions


def test_frontier_add_displaces_dominated_incumbent():
    """A new entry that dominates an incumbent should evict it."""
    pf = ParetoFrontier(max_per_context=5)
    weak = _entry(version=1, score=0.4, evidence=5, t=1.0)
    strong = _entry(version=2, score=0.9, evidence=50, t=2.0)
    assert pf.add(weak) is True
    assert pf.add(strong) is True
    versions = {x.version for x in pf.all_for("s1")}
    assert versions == {2}


# ─────────────────────────────────────────────────────────────────────
# select_for — per-context wins, fallback to global
# ─────────────────────────────────────────────────────────────────────


def test_select_for_returns_context_specific_winner():
    pf = ParetoFrontier()
    # context A best
    pf.add(_entry(version=1, ctx="A", score=0.9, evidence=20, t=1.0))
    # context B has a different best
    pf.add(_entry(version=2, ctx="B", score=0.7, evidence=30, t=2.0))
    # asking for A returns the A-specific winner, NOT the global B one
    sel = pf.select_for("s1", "A")
    assert sel is not None
    assert sel.version == 1
    assert sel.context_signature == "A"


def test_select_for_falls_back_to_global_best_when_context_absent():
    """Unknown context for a skill → best entry across that skill's contexts."""
    pf = ParetoFrontier()
    pf.add(_entry(version=1, ctx="A", score=0.6, evidence=10, t=1.0))
    pf.add(_entry(version=2, ctx="B", score=0.95, evidence=40, t=2.0))
    # We've never seen context Z; fall back to the global best (version 2).
    sel = pf.select_for("s1", "Z")
    assert sel is not None
    assert sel.version == 2


def test_select_for_unknown_skill_returns_none():
    pf = ParetoFrontier()
    pf.add(_entry(skill="s1"))
    assert pf.select_for("does_not_exist", "default") is None


# ─────────────────────────────────────────────────────────────────────
# all_for — every entry across contexts
# ─────────────────────────────────────────────────────────────────────


def test_all_for_returns_entries_across_contexts():
    pf = ParetoFrontier()
    pf.add(_entry(version=1, ctx="A", score=0.7, evidence=5, t=1.0))
    pf.add(_entry(version=2, ctx="B", score=0.6, evidence=8, t=2.0))
    pf.add(_entry(version=3, ctx="A", score=0.5, evidence=3, t=3.0))  # not dominated by v1 (lower evidence is OK against lower score? — actually weaker on both, dominated)
    out = pf.all_for("s1")
    versions = {e.version for e in out}
    # v3 IS dominated by v1 (0.5<0.7 score, 3<5 evidence) → admitted? Let's check rule.
    # In our implementation, v3 is dominated → add returns False → not in frontier.
    assert {1, 2}.issubset(versions)
    # Ordering is deterministic by (context, version, added_at).
    contexts = [e.context_signature for e in out]
    assert contexts == sorted(contexts)


# ─────────────────────────────────────────────────────────────────────
# evict_dominated
# ─────────────────────────────────────────────────────────────────────


def test_evict_dominated_removes_strict_losers():
    """Force-load a frontier (bypass add's check) then evict."""
    pf = ParetoFrontier(max_per_context=10)
    # Manually populate to simulate stale state.
    bucket = pf._buckets.setdefault(("s1", "A"), [])  # type: ignore[attr-defined]
    bucket.extend(
        [
            _entry(version=1, ctx="A", score=0.9, evidence=50, t=1.0),
            _entry(version=2, ctx="A", score=0.5, evidence=10, t=2.0),
            _entry(version=3, ctx="A", score=0.3, evidence=5, t=3.0),
        ]
    )
    removed = pf.evict_dominated()
    assert removed == 2
    survivors = {e.version for e in pf.all_for("s1")}
    assert survivors == {1}


def test_evict_dominated_zero_when_nothing_dominated():
    """All entries non-dominated → evict_dominated removes nothing."""
    pf = ParetoFrontier(max_per_context=10)
    pf.add(_entry(version=1, score=0.8, evidence=10, t=1.0))
    pf.add(_entry(version=2, score=0.6, evidence=50, t=2.0))  # higher evidence
    # Neither dominates the other (one beats on score, other on evidence).
    assert pf.evict_dominated() == 0
    assert len(pf.all_for("s1")) == 2


# ─────────────────────────────────────────────────────────────────────
# max_per_context cap honored
# ─────────────────────────────────────────────────────────────────────


def test_max_per_context_cap_honored():
    pf = ParetoFrontier(max_per_context=2)
    # Three entries that don't dominate each other (different score/evid trade-off).
    e1 = _entry(version=1, score=0.9, evidence=5, t=1.0)
    e2 = _entry(version=2, score=0.7, evidence=20, t=2.0)
    e3 = _entry(version=3, score=0.8, evidence=10, t=3.0)
    pf.add(e1)
    pf.add(e2)
    pf.add(e3)
    entries = pf.all_for("s1")
    assert len(entries) == 2  # capped


# ─────────────────────────────────────────────────────────────────────
# (skill_id, context) independence
# ─────────────────────────────────────────────────────────────────────


def test_different_skills_are_independent():
    pf = ParetoFrontier()
    pf.add(_entry(skill="alpha", version=1, score=0.9, evidence=10, t=1.0))
    pf.add(_entry(skill="beta", version=1, score=0.4, evidence=2, t=2.0))
    # The beta entry exists despite being weaker — different skill.
    assert pf.select_for("beta", "default") is not None
    assert pf.select_for("alpha", "default").version == 1  # type: ignore[union-attr]
    assert pf.all_for("alpha") and pf.all_for("beta")


def test_different_contexts_within_same_skill_are_independent():
    pf = ParetoFrontier()
    # Context A admits a weaker entry; context B admits a stronger one.
    # Across contexts, neither dominates the other.
    pf.add(_entry(skill="s", version=1, ctx="A", score=0.4, evidence=2, t=1.0))
    pf.add(_entry(skill="s", version=2, ctx="B", score=0.9, evidence=20, t=2.0))
    a = pf.select_for("s", "A")
    b = pf.select_for("s", "B")
    assert a is not None and a.context_signature == "A"
    assert b is not None and b.context_signature == "B"
    assert a.version != b.version


# ─────────────────────────────────────────────────────────────────────
# Determinism
# ─────────────────────────────────────────────────────────────────────


def test_frontier_deterministic_across_same_input_sequence():
    seq = [
        _entry(version=1, score=0.7, evidence=5, t=1.0),
        _entry(version=2, score=0.5, evidence=10, t=2.0),
        _entry(version=3, score=0.8, evidence=20, t=3.0),
        _entry(version=4, ctx="other", score=0.6, evidence=3, t=4.0),
    ]
    pf_a = ParetoFrontier(max_per_context=3)
    pf_b = ParetoFrontier(max_per_context=3)
    for e in seq:
        pf_a.add(e)
    for e in seq:
        pf_b.add(e)
    assert pf_a.all_for("s1") == pf_b.all_for("s1")
    sel_a = pf_a.select_for("s1", "default")
    sel_b = pf_b.select_for("s1", "default")
    assert sel_a == sel_b
