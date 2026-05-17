"""Unit tests for Jarvis Phase 6.2: ReasoningEngine.

Covers:
* ReasoningResult shape + confidence cap (Iron Rule #2 mirror).
* Each mode invokes the LLM with the correct prompt anchors.
* causal: graph CAUSED_BY edges flow into evidence.
* analogical: graph + StrategyBank dual-path retrieval, both mocked.
* counterfactual: prompt contains both actual + alternative.
* meta: emits suggested_goals when the LLM reports insufficient knowledge.
* Iron Rule #3 weak tier: causal/counterfactual/meta short-circuit; analogical
  still runs but skips LLM ranking.
* reason(mode='auto') routes via mocked meta hint.
* Confidence cap enforced even when LLM returns 0.95.
* Bad LLM JSON → empty result (never crashes).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.cognition.reasoning import (
    CONFIDENCE_CAP_DEFAULT,
    ReasoningEngine,
    ReasoningResult,
)


# -------------------------------------------------------------------- fakes


class FakeLLM:
    """Minimal duck-typed LLM.

    ``responses`` is consumed FIFO; if exhausted we return ``default``.
    ``calls`` records each prompt seen, useful for asserting prompt
    structure.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        default: str = "",
    ) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[str] = []

    async def complete(self, prompt) -> str:
        # 2026-05-17: real LLMProvider.complete takes
        # ``messages: list[Message]`` post-Wave-27. ReasoningEngine
        # was updated in the same commit; this fake accepts both
        # shapes so tests assert on the string prompt the way they
        # always did.
        if isinstance(prompt, str):
            text = prompt
        elif isinstance(prompt, list) and prompt:
            content = getattr(prompt[-1], "content", None)
            text = content if isinstance(content, str) else str(content)
        else:
            text = ""
        self.calls.append(text)
        if self.responses:
            return self.responses.pop(0)
        return self.default


@dataclass(frozen=True)
class FakeNode:
    id: str
    type: str
    content: str


@dataclass(frozen=True)
class FakeEdge:
    id: str
    source_id: str
    target_id: str
    relation: str


class FakeGraph:
    """Dict-backed graph with the methods ReasoningEngine duck-calls."""

    def __init__(
        self,
        nodes: list[FakeNode] | None = None,
        edges: dict[str, list[tuple[FakeEdge, FakeNode]]] | None = None,
    ) -> None:
        # nodes keyed by type for query_by_type
        self._nodes_by_type: dict[str, list[FakeNode]] = {}
        for n in nodes or []:
            self._nodes_by_type.setdefault(n.type, []).append(n)
        # edges keyed by (source_id, relation) → list of (edge, target node)
        self._edges = edges or {}
        self.query_calls: list[tuple[str, int]] = []
        self.neighbor_calls: list[tuple[str, str | None]] = []

    async def query_by_type(self, type: str, *, limit: int = 10) -> list[FakeNode]:  # noqa: A002
        self.query_calls.append((type, limit))
        return list(self._nodes_by_type.get(type, []))[:limit]

    async def get_neighbors(
        self,
        node_id: str,
        *,
        relation: str | None = None,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[tuple[FakeEdge, FakeNode]]:
        self.neighbor_calls.append((node_id, relation))
        key = f"{node_id}|{relation}"
        return list(self._edges.get(key, []))


@dataclass(frozen=True)
class FakeStrategy:
    id: str
    when_pattern: str
    then_action: str


class FakeBank:
    def __init__(self, strategies: list[FakeStrategy] | None = None) -> None:
        self.strategies = list(strategies or [])
        self.calls: list[tuple[str, int]] = []

    async def retrieve(
        self, query_text: str, limit: int = 3
    ) -> list[FakeStrategy]:
        self.calls.append((query_text, limit))
        return list(self.strategies)[:limit]


# -------------------------------------------------------------------- helpers


def make_engine(
    *,
    llm: Any = None,
    graph: Any | None = None,
    bank: Any | None = None,
    tier: str = "strong",
    cap: float = CONFIDENCE_CAP_DEFAULT,
) -> ReasoningEngine:
    return ReasoningEngine(
        llm=llm if llm is not None else FakeLLM(),
        graph=graph,
        bank=bank,
        evolution_tier=tier,
        confidence_cap=cap,
    )


def _ok_causal(confidence: float = 0.5, supports: bool = True) -> str:
    return json.dumps(
        {
            "supports": supports,
            "confidence": confidence,
            "conclusion": "Yes, A causes B.",
            "key_points": ["p1", "p2"],
        }
    )


def _ok_analogical_rank() -> str:
    return json.dumps(
        [
            {"index": 0, "score": 0.8, "why": "matches"},
            {"index": 1, "score": 0.4, "why": "related"},
        ]
    )


def _ok_counterfactual(confidence: float = 0.5) -> str:
    return json.dumps(
        {
            "outcome": "We would have been late.",
            "confidence": confidence,
            "key_differences": ["d1"],
        }
    )


def _ok_meta(
    sufficient: bool = False,
    gap: str = "missing telemetry",
    goals: list[str] | None = None,
    confidence: float = 0.5,
) -> str:
    return json.dumps(
        {
            "sufficient": sufficient,
            "gap": gap,
            "confidence": confidence,
            "suggested_goals": goals if goals is not None else ["go fetch X"],
        }
    )


# -------------------------------------------------------------------- tests


# --- ReasoningResult shape -------------------------------------------------


def test_result_dataclass_is_frozen() -> None:
    r = ReasoningResult(
        mode="meta", conclusion="x", confidence=0.5, evidence=("a",)
    )
    with pytest.raises(Exception):
        r.confidence = 0.9  # type: ignore[misc]


def test_result_default_factories_yield_independent_instances() -> None:
    a = ReasoningResult(mode="meta", conclusion="", confidence=0.0, evidence=())
    b = ReasoningResult(mode="meta", conclusion="", confidence=0.0, evidence=())
    assert a.suggested_goals == ()
    assert a.metadata == {}
    # Should be different dict instances even with same default value.
    assert a.metadata is not b.metadata


def test_engine_default_cap_is_0_6() -> None:
    eng = make_engine()
    assert eng.confidence_cap == pytest.approx(0.6)


def test_engine_exposes_tier() -> None:
    eng = make_engine(tier="strong")
    assert eng.evolution_tier == "strong"


# --- causal ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_causal_invokes_llm_with_hypothesis_and_evidence_anchors() -> None:
    llm = FakeLLM(responses=[_ok_causal()])
    eng = make_engine(llm=llm)
    result = await eng.causal(
        hypothesis="Deploys on Friday cause incidents",
        evidence=["incident on 2024-01-05 after Friday deploy"],
    )
    assert result.mode == "causal"
    assert "HYPOTHESIS" in llm.calls[0]
    assert "Deploys on Friday" in llm.calls[0]
    assert "incident on 2024-01-05" in llm.calls[0]


@pytest.mark.asyncio
async def test_causal_pulls_caused_by_edges_into_evidence() -> None:
    a = FakeNode("e1", "event", "deploy on Friday")
    b = FakeNode("e2", "event", "incident at 23:00")
    edge_map = {
        "e1|CAUSED_BY": [(FakeEdge("ed1", "e1", "e2", "CAUSED_BY"), b)]
    }
    graph = FakeGraph(nodes=[a], edges=edge_map)
    llm = FakeLLM(responses=[_ok_causal()])
    eng = make_engine(llm=llm, graph=graph)

    result = await eng.causal(
        hypothesis="deploy on Friday causes incident", evidence=[]
    )
    assert any("CAUSED_BY" in e for e in result.evidence)
    assert result.metadata["graph_hits"] >= 1


@pytest.mark.asyncio
async def test_causal_returns_empty_when_no_evidence_and_no_graph() -> None:
    eng = make_engine(llm=FakeLLM())
    result = await eng.causal(hypothesis="A causes B", evidence=[])
    assert result.confidence == 0.0
    assert result.conclusion == ""
    assert result.metadata["reason"] == "no_evidence_available"


@pytest.mark.asyncio
async def test_causal_caps_runaway_llm_confidence() -> None:
    llm = FakeLLM(responses=[_ok_causal(confidence=0.95)])
    eng = make_engine(llm=llm)
    result = await eng.causal(hypothesis="A → B", evidence=["x supports A→B"])
    assert result.confidence <= CONFIDENCE_CAP_DEFAULT


@pytest.mark.asyncio
async def test_causal_bad_json_returns_empty_not_crash() -> None:
    llm = FakeLLM(responses=["not valid {json"])
    eng = make_engine(llm=llm)
    result = await eng.causal(hypothesis="A → B", evidence=["e"])
    assert result.confidence == 0.0
    assert result.metadata["reason"] == "bad_llm_json"


# --- analogical ------------------------------------------------------------


@pytest.mark.asyncio
async def test_analogical_queries_graph_and_bank() -> None:
    nodes = [
        FakeNode("e1", "event", "deploy went bad"),
        FakeNode("e2", "event", "bad PR review"),
    ]
    graph = FakeGraph(nodes=nodes)
    bank = FakeBank(
        strategies=[FakeStrategy("s1", "before deploy", "run smoke test")]
    )
    llm = FakeLLM(responses=[_ok_analogical_rank()])
    eng = make_engine(llm=llm, graph=graph, bank=bank)

    result = await eng.analogical("we just shipped a Friday deploy")
    assert graph.query_calls, "expected graph.query_by_type to be called"
    assert bank.calls, "expected bank.retrieve to be called"
    assert result.evidence  # at least something came through
    assert result.metadata["graph_hits"] == 2
    assert result.metadata["bank_hits"] == 1


@pytest.mark.asyncio
async def test_analogical_no_candidates_returns_empty() -> None:
    eng = make_engine(llm=FakeLLM(), graph=FakeGraph(), bank=FakeBank())
    result = await eng.analogical("nothing")
    assert result.confidence == 0.0
    assert result.metadata["reason"] == "no_candidates"


@pytest.mark.asyncio
async def test_analogical_handles_bad_rank_json_with_fallback() -> None:
    nodes = [FakeNode("e1", "event", "X")]
    graph = FakeGraph(nodes=nodes)
    llm = FakeLLM(responses=["{not json"])
    eng = make_engine(llm=llm, graph=graph)
    result = await eng.analogical("query")
    assert result.evidence  # we still surface candidates
    assert result.metadata["unranked"] is True


@pytest.mark.asyncio
async def test_analogical_caps_confidence_from_high_score_avg() -> None:
    nodes = [
        FakeNode("e1", "event", "hot incident"),
        FakeNode("e2", "event", "warm incident"),
    ]
    graph = FakeGraph(nodes=nodes)
    rank = json.dumps(
        [
            {"index": 0, "score": 0.99, "why": "exact"},
            {"index": 1, "score": 0.98, "why": "near"},
        ]
    )
    eng = make_engine(llm=FakeLLM(responses=[rank]), graph=graph)
    result = await eng.analogical("hot incident", top_k=2)
    assert result.confidence <= CONFIDENCE_CAP_DEFAULT


# --- counterfactual --------------------------------------------------------


@pytest.mark.asyncio
async def test_counterfactual_prompt_carries_actual_and_alternative() -> None:
    llm = FakeLLM(responses=[_ok_counterfactual()])
    eng = make_engine(llm=llm)
    result = await eng.counterfactual(
        decision_point="we deployed at 5pm Friday",
        alternative="we waited until Monday morning",
    )
    prompt = llm.calls[0]
    assert "we deployed at 5pm Friday" in prompt
    assert "we waited until Monday morning" in prompt
    assert "ACTUAL" in prompt
    assert "ALTERNATIVE" in prompt
    assert result.conclusion


@pytest.mark.asyncio
async def test_counterfactual_includes_history_grounding_when_graph_has_events() -> None:
    nodes = [FakeNode("e1", "event", "previous late friday deploy")]
    graph = FakeGraph(nodes=nodes)
    llm = FakeLLM(responses=[_ok_counterfactual()])
    eng = make_engine(llm=llm, graph=graph)
    result = await eng.counterfactual("late friday deploy", "monday deploy")
    assert any(
        "previous late friday deploy" in e for e in result.evidence
    )
    assert "previous late friday deploy" in llm.calls[0]


@pytest.mark.asyncio
async def test_counterfactual_caps_confidence() -> None:
    llm = FakeLLM(responses=[_ok_counterfactual(confidence=0.95)])
    eng = make_engine(llm=llm)
    result = await eng.counterfactual("y", "x")
    assert result.confidence <= CONFIDENCE_CAP_DEFAULT


@pytest.mark.asyncio
async def test_counterfactual_bad_json_returns_empty() -> None:
    llm = FakeLLM(responses=["???"])
    eng = make_engine(llm=llm)
    result = await eng.counterfactual("y", "x")
    assert result.confidence == 0.0
    assert result.metadata["reason"] == "bad_llm_json"


# --- meta ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_returns_suggested_goals_when_insufficient() -> None:
    llm = FakeLLM(
        responses=[
            _ok_meta(
                sufficient=False,
                gap="no telemetry from friday",
                goals=["fetch friday telemetry", "ask user for log path"],
            )
        ]
    )
    eng = make_engine(llm=llm)
    result = await eng.meta("did the friday deploy fail?")
    assert "no telemetry from friday" in result.conclusion
    assert "fetch friday telemetry" in result.suggested_goals
    assert result.metadata["sufficient"] is False


@pytest.mark.asyncio
async def test_meta_no_goals_when_sufficient() -> None:
    llm = FakeLLM(
        responses=[
            _ok_meta(sufficient=True, gap="", goals=["this should be dropped"])
        ]
    )
    eng = make_engine(llm=llm)
    result = await eng.meta("anything")
    assert result.suggested_goals == ()
    assert result.metadata["sufficient"] is True


@pytest.mark.asyncio
async def test_meta_caps_confidence() -> None:
    llm = FakeLLM(responses=[_ok_meta(confidence=0.92)])
    eng = make_engine(llm=llm)
    result = await eng.meta("q")
    assert result.confidence <= CONFIDENCE_CAP_DEFAULT


@pytest.mark.asyncio
async def test_meta_bad_json_returns_empty() -> None:
    llm = FakeLLM(responses=["nope"])
    eng = make_engine(llm=llm)
    result = await eng.meta("q")
    assert result.confidence == 0.0
    assert result.metadata["reason"] == "bad_llm_json"


# --- iron rule 3 (weak tier) -----------------------------------------------


@pytest.mark.asyncio
async def test_weak_tier_causal_short_circuits() -> None:
    llm = FakeLLM(responses=[_ok_causal()])
    eng = make_engine(llm=llm, tier="weak")
    result = await eng.causal("A → B", evidence=["x"])
    assert result.confidence == 0.0
    assert result.evidence == ()
    assert result.metadata["reason"] == "iron_rule_3_weak_tier_skipped"
    assert llm.calls == [], "weak tier must not call LLM for causal"


@pytest.mark.asyncio
async def test_weak_tier_counterfactual_short_circuits() -> None:
    llm = FakeLLM(responses=[_ok_counterfactual()])
    eng = make_engine(llm=llm, tier="weak")
    result = await eng.counterfactual("y", "x")
    assert result.confidence == 0.0
    assert llm.calls == []


@pytest.mark.asyncio
async def test_weak_tier_meta_short_circuits() -> None:
    llm = FakeLLM(responses=[_ok_meta()])
    eng = make_engine(llm=llm, tier="weak")
    result = await eng.meta("anything")
    assert result.confidence == 0.0
    assert result.suggested_goals == ()
    assert llm.calls == []


@pytest.mark.asyncio
async def test_weak_tier_analogical_still_runs_graph_path() -> None:
    nodes = [
        FakeNode("e1", "event", "deploy went bad"),
        FakeNode("e2", "event", "PR review missed"),
    ]
    graph = FakeGraph(nodes=nodes)
    bank = FakeBank(strategies=[FakeStrategy("s1", "deploy", "smoke test")])
    llm = FakeLLM(responses=["should not be called"])
    eng = make_engine(llm=llm, graph=graph, bank=bank, tier="weak")
    result = await eng.analogical("anything")
    assert result.evidence  # graph path still produced candidates
    assert result.metadata["skipped_llm_ranker"] is True
    assert result.metadata["graph_hits"] == 2
    assert result.metadata["bank_hits"] == 1
    assert llm.calls == [], "weak tier must skip LLM ranker"


# --- reason(mode='auto') routing ------------------------------------------


@pytest.mark.asyncio
async def test_reason_auto_routes_via_meta_hint() -> None:
    """LLM call 1 = router (returns mode), call 2 = the routed mode."""
    router_resp = json.dumps(
        {"mode": "causal", "rationale": "user asked why X happened"}
    )
    causal_resp = _ok_causal()
    llm = FakeLLM(responses=[router_resp, causal_resp])
    # Provide some evidence so causal doesn't short-circuit on empty.
    nodes = [FakeNode("e1", "event", "Friday deploy")]
    edges = {
        "e1|CAUSED_BY": [
            (
                FakeEdge("ed1", "e1", "e2", "CAUSED_BY"),
                FakeNode("e2", "event", "incident"),
            )
        ]
    }
    graph = FakeGraph(nodes=nodes, edges=edges)
    eng = make_engine(llm=llm, graph=graph)

    result = await eng.reason("why did friday deploy break?", mode="auto")
    assert result.mode == "causal"
    assert result.metadata.get("routed_from") == "auto"
    assert len(llm.calls) == 2  # router + causal


@pytest.mark.asyncio
async def test_reason_auto_unknown_mode_falls_back_to_meta() -> None:
    """Bad mode hint → still produces a result (via meta)."""
    router_resp = json.dumps({"mode": "telepathy"})
    meta_resp = _ok_meta(sufficient=True, gap="")
    llm = FakeLLM(responses=[router_resp, meta_resp])
    eng = make_engine(llm=llm)
    result = await eng.reason("?", mode="auto")
    assert result.mode == "meta"
    assert result.metadata.get("routed_from") == "auto"


@pytest.mark.asyncio
async def test_reason_explicit_mode_skips_router() -> None:
    llm = FakeLLM(responses=[_ok_meta()])
    eng = make_engine(llm=llm)
    result = await eng.reason("query", mode="meta")
    assert result.mode == "meta"
    assert len(llm.calls) == 1, "explicit mode should not call router"


@pytest.mark.asyncio
async def test_reason_auto_on_weak_tier_routes_to_analogical() -> None:
    nodes = [FakeNode("e1", "event", "thing happened")]
    graph = FakeGraph(nodes=nodes)
    llm = FakeLLM()  # should not be touched on weak tier
    eng = make_engine(llm=llm, graph=graph, tier="weak")
    result = await eng.reason("anything", mode="auto")
    assert result.mode == "analogical"
    assert llm.calls == []


# --- robustness ------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_returns_fenced_json_still_parses() -> None:
    fenced = "```json\n" + _ok_meta(sufficient=True, gap="") + "\n```"
    llm = FakeLLM(responses=[fenced])
    eng = make_engine(llm=llm)
    result = await eng.meta("q")
    assert result.metadata["sufficient"] is True


@pytest.mark.asyncio
async def test_llm_exception_returns_empty_not_crash() -> None:
    class ExplodingLLM:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("LLM offline")

    eng = make_engine(llm=ExplodingLLM())
    result = await eng.meta("q")
    assert result.confidence == 0.0
    assert result.metadata["reason"] == "bad_llm_json"


@pytest.mark.asyncio
async def test_confidence_cap_custom_value_respected() -> None:
    llm = FakeLLM(responses=[_ok_causal(confidence=0.9)])
    eng = make_engine(llm=llm, cap=0.3)
    result = await eng.causal("A→B", evidence=["e"])
    assert result.confidence <= 0.3


@pytest.mark.asyncio
async def test_confidence_cap_handles_negative_and_nan_inputs() -> None:
    nan_resp = json.dumps(
        {
            "supports": True,
            "confidence": -5.0,
            "conclusion": "x",
            "key_points": [],
        }
    )
    llm = FakeLLM(responses=[nan_resp])
    eng = make_engine(llm=llm)
    result = await eng.causal("A→B", evidence=["e"])
    assert result.confidence == 0.0
