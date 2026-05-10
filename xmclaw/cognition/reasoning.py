"""ReasoningEngine — Jarvis Phase 6.2.

Four reasoning modes plus an ``auto`` meta-router:

* **causal** — A → B?  Pulls ``CAUSED_BY`` edges out of the graph and asks
  the LLM to validate / quantify.
* **analogical** — Find historical Events similar to a current
  situation.  Dual-path recall: graph neighbours + StrategyBank.
* **counterfactual** — If we'd done X instead of Y, what would have
  happened?  LLM reasoning grounded in similar historical events.
* **meta** — Do I know enough?  Where's the knowledge gap?  Returns
  ``suggested_goals`` describing what to perceive / experiment next.

Iron Rule #3 (tier-gated reasoning).  The constructor accepts an
``evolution_tier`` string.  When ``tier == "weak"`` the LLM-heavy
branches (``causal`` / ``counterfactual`` / ``meta``) short-circuit and
return an empty :class:`ReasoningResult` with ``confidence = 0.0`` —
strong-LLM reasoning is too unreliable on weak models to risk
acting on.  ``analogical`` keeps running because the graph-only path is
still informative without an LLM ranker.

Iron Rule #2 (confidence cap).  Every result returned is passed through
``_cap`` which clamps ``confidence`` to ``confidence_cap`` (default
0.6).  Even when the LLM swears it's certain we refuse to claim more.

The engine is **provider-free**.  Callers pass an LLM, a graph and an
optional StrategyBank — all duck-typed (``Any``) so this module stays
import-direction-clean (no edge from ``xmclaw.cognition`` into
``xmclaw.providers``).

The required duck-typed contracts:

* ``llm.complete(prompt: str) -> Awaitable[str]`` (returns text the
  engine will try to JSON-parse).
* ``graph.query_by_type(type, *, limit) -> Awaitable[list[GraphNode]]``
  *(GraphNode has ``id``, ``content``, ``type`` attributes)*.
* ``graph.get_neighbors(node_id, *, relation, depth, min_strength)``
  ``-> Awaitable[list[tuple[GraphEdge, GraphNode]]]``.
* ``bank.retrieve(query_text: str, limit: int) -> Awaitable[list[Strategy]]``
  *(Strategy has ``when_pattern`` + ``then_action``)*.

Anything that satisfies the duck shape works; the production
implementations live in ``xmclaw.cognition.memory_graph`` and
``xmclaw.core.journal.strategy_bank``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal


_log = logging.getLogger(__name__)


ReasoningMode = Literal["auto", "causal", "analogical", "counterfactual", "meta"]


CONFIDENCE_CAP_DEFAULT = 0.6
"""Anti-overclaim cap.  Even when the LLM says 0.95 we return at most
this.  Mirrors the StrategyBank Iron Rule #2 cap (0.60); reasoning is a
*hint*, not policy."""

WEAK_TIER = "weak"
"""Iron Rule #3 — tier name that short-circuits LLM-heavy reasoning."""


@dataclass(frozen=True)
class ReasoningResult:
    """Output of every reasoning call.

    All four branches return the same shape so callers (Planner,
    GoalGenerator, CognitiveDaemon) can dispatch uniformly.

    * ``mode`` — the reasoning kind that produced this.  ``auto`` is
      replaced with the routed mode before returning.
    * ``conclusion`` — one-paragraph natural-language answer.
    * ``confidence`` — already clamped to ``[0.0, confidence_cap]``.
    * ``evidence`` — the raw supporting points used to reach the
      conclusion (graph node contents, strategies, etc.).
    * ``suggested_goals`` — populated mainly by ``meta`` (knowledge-gap
      goals).  Other modes can leave it empty.
    * ``metadata`` — engine bookkeeping (routed_from, tier_skipped,
      llm_raw_response, ...).  Free-form, callers shouldn't depend on
      specific keys.
    """

    mode: ReasoningMode
    conclusion: str
    confidence: float
    evidence: tuple[str, ...]
    suggested_goals: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


_AUTO_ROUTE_PROMPT = """Pick the reasoning mode best suited to this query.
Return strict JSON: {{"mode": "causal" | "analogical" | "counterfactual" | "meta", "rationale": "..."}}

QUERY:
{query}

Hints:
- causal:           "did X cause Y", "why did", "because of"
- analogical:       "similar to", "like the time", "have I seen this"
- counterfactual:   "what if I had", "if instead", "would have"
- meta:             "do I know enough", "what should I learn", "what's missing"
"""


_CAUSAL_PROMPT = """You are validating a causal hypothesis using
historical evidence from a memory graph.

HYPOTHESIS:
{hypothesis}

EVIDENCE (most-relevant first):
{evidence_block}

Decide if the evidence supports the hypothesis.  Return strict JSON:
{{
  "supports": true | false,
  "confidence": <float 0..1>,
  "conclusion": "<one paragraph>",
  "key_points": ["...", "..."]
}}
"""


_ANALOGICAL_RANK_PROMPT = """Rank these historical situations by
similarity to the CURRENT_SITUATION.  Return strict JSON list:
[{{"index": <int>, "score": <float 0..1>, "why": "..."}}]

CURRENT_SITUATION:
{current_situation}

CANDIDATES:
{candidates_block}
"""


_COUNTERFACTUAL_PROMPT = """Reason about a counterfactual.  We did
ACTUAL.  What would have happened if we'd done ALTERNATIVE instead?
Use SIMILAR_HISTORY as grounding — do not invent facts unsupported by
it.

ACTUAL (what we did):
{actual}

ALTERNATIVE (what we might have done):
{alternative}

SIMILAR_HISTORY (events that resemble this decision point):
{history_block}

Return strict JSON:
{{
  "outcome": "<one paragraph describing the counterfactual outcome>",
  "confidence": <float 0..1>,
  "key_differences": ["...", "..."]
}}
"""


_META_PROMPT = """Self-assess: given this query, does the system have
enough information to answer with confidence?  If not, what should it
perceive or experiment with next?  Return strict JSON:
{{
  "sufficient": true | false,
  "gap": "<what's missing, one sentence>",
  "confidence": <float 0..1>,
  "suggested_goals": ["<short imperative goal>", ...]
}}

QUERY:
{query}
"""


def _cap(value: float, cap: float) -> float:
    """Clamp ``value`` to ``[0.0, cap]``.  Iron Rule #2 enforcement."""
    if value != value:  # NaN check
        return 0.0
    if value < 0.0:
        return 0.0
    if value > cap:
        return cap
    return value


def _try_parse_json(raw: str) -> Any | None:
    """Lenient JSON parser used on every LLM response.

    Returns ``None`` on failure rather than raising — every caller is
    expected to fall back to an empty result so a malformed LLM
    response can never crash the engine.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip common LLM Markdown fences before parsing.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _empty_result(
    mode: ReasoningMode,
    *,
    reason: str,
    suggested_goals: tuple[str, ...] = (),
    extra_metadata: dict[str, Any] | None = None,
) -> ReasoningResult:
    """Construct the canonical 'no-op' result.

    Used by:
    * Iron Rule #3 weak-tier short-circuits.
    * Bad LLM JSON.
    * Empty-evidence causal calls (we don't claim a conclusion without
      data).
    """
    metadata: dict[str, Any] = {"reason": reason}
    if extra_metadata:
        metadata.update(extra_metadata)
    return ReasoningResult(
        mode=mode,
        conclusion="",
        confidence=0.0,
        evidence=(),
        suggested_goals=suggested_goals,
        metadata=metadata,
    )


class ReasoningEngine:
    """4 reasoning modes + meta-router.  See module docstring."""

    def __init__(
        self,
        llm: Any,
        graph: Any | None = None,
        bank: Any | None = None,
        evolution_tier: str = "unknown",
        confidence_cap: float = CONFIDENCE_CAP_DEFAULT,
    ) -> None:
        self._llm = llm
        self._graph = graph
        self._bank = bank
        self._tier = evolution_tier
        self._cap_value = float(confidence_cap)

    # --- public API ---

    @property
    def evolution_tier(self) -> str:
        """Configured tier — exposed so callers can inspect tier-gating."""
        return self._tier

    @property
    def confidence_cap(self) -> float:
        """Currently configured anti-overclaim cap."""
        return self._cap_value

    def _is_weak_tier(self) -> bool:
        return self._tier == WEAK_TIER

    async def reason(
        self,
        query: str,
        mode: ReasoningMode = "auto",
    ) -> ReasoningResult:
        """Top-level entry.  ``mode='auto'`` runs ``meta`` first to pick
        a downstream mode based on the LLM's hint, then dispatches.
        """
        if mode == "causal":
            return await self.causal(query, evidence=[])
        if mode == "analogical":
            return await self.analogical(query)
        if mode == "counterfactual":
            return await self.counterfactual(query, alternative="")
        if mode == "meta":
            return await self.meta(query)
        if mode == "auto":
            return await self._auto(query)
        # Future-proof: unknown mode strings fall through to meta.
        return await self.meta(query)

    async def causal(
        self,
        hypothesis: str,
        evidence: list[str],
    ) -> ReasoningResult:
        """A → B?  Query graph CAUSED_BY edges + LLM-validate."""
        if self._is_weak_tier():
            return _empty_result(
                "causal",
                reason="iron_rule_3_weak_tier_skipped",
                extra_metadata={"tier": self._tier},
            )

        # Augment user-supplied evidence with CAUSED_BY neighbours from
        # the graph so we ground LLM validation in stored facts.
        graph_evidence = await self._collect_causal_evidence(hypothesis)
        all_evidence: list[str] = list(evidence) + graph_evidence

        if not all_evidence:
            return _empty_result(
                "causal",
                reason="no_evidence_available",
            )

        evidence_block = self._format_evidence(all_evidence)
        prompt = _CAUSAL_PROMPT.format(
            hypothesis=hypothesis,
            evidence_block=evidence_block,
        )
        raw = await self._llm_complete(prompt)
        parsed = _try_parse_json(raw)
        if not isinstance(parsed, dict):
            return _empty_result(
                "causal",
                reason="bad_llm_json",
                extra_metadata={"llm_raw": (raw or "")[:200]},
            )

        conclusion = str(parsed.get("conclusion", "")).strip()
        confidence_raw = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        if not parsed.get("supports", True):
            # Hypothesis rejected — keep conclusion but cap confidence
            # in negative direction (still expressed as 0..cap).
            confidence = max(0.0, confidence)

        key_points = _coerce_str_tuple(parsed.get("key_points"))
        # Carry the actual evidence we sent to the LLM through to the
        # caller so the caller can render "why".
        evidence_tuple = tuple(all_evidence) + key_points

        return ReasoningResult(
            mode="causal",
            conclusion=conclusion,
            confidence=_cap(confidence, self._cap_value),
            evidence=evidence_tuple,
            metadata={
                "supports": bool(parsed.get("supports", False)),
                "graph_hits": len(graph_evidence),
            },
        )

    async def analogical(
        self,
        current_situation: str,
        top_k: int = 3,
    ) -> ReasoningResult:
        """Find historical Events similar to ``current_situation``.

        Dual-path retrieval:
        * graph: events + their LEADS_TO / RELATED_TO neighbours
        * strategy bank: ``when`` patterns matching the situation

        On weak tier we still run the graph + bank retrieval but skip
        the LLM ranker (Iron Rule #3).  We rank the candidates by their
        order of arrival from the stores instead.
        """
        graph_candidates = await self._collect_event_candidates(
            current_situation, top_k=top_k
        )
        bank_candidates = await self._collect_strategy_candidates(
            current_situation, top_k=top_k
        )

        candidates = graph_candidates + bank_candidates
        if not candidates:
            return _empty_result(
                "analogical",
                reason="no_candidates",
                extra_metadata={"tier": self._tier},
            )

        if self._is_weak_tier():
            top = candidates[: max(1, top_k)]
            return ReasoningResult(
                mode="analogical",
                conclusion=(
                    f"Found {len(top)} historical situation(s) resembling "
                    "the current one (graph + strategy bank, unranked)."
                ),
                confidence=_cap(0.4, self._cap_value),
                evidence=tuple(c[1] for c in top),
                metadata={
                    "tier": self._tier,
                    "skipped_llm_ranker": True,
                    "graph_hits": len(graph_candidates),
                    "bank_hits": len(bank_candidates),
                },
            )

        candidates_block = self._format_candidates(candidates)
        prompt = _ANALOGICAL_RANK_PROMPT.format(
            current_situation=current_situation,
            candidates_block=candidates_block,
        )
        raw = await self._llm_complete(prompt)
        parsed = _try_parse_json(raw)
        ranked: list[tuple[float, str, str]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                idx = item.get("index")
                if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                    continue
                try:
                    score = float(item.get("score", 0.0))
                except (TypeError, ValueError):
                    score = 0.0
                why = str(item.get("why", "")).strip()
                ranked.append((score, candidates[idx][1], why))

        if not ranked:
            # Bad LLM JSON — fall back to natural retrieval order with
            # a tagged-as-unranked metadata so the caller knows.
            ranked = [(0.4, c[1], "") for c in candidates[: top_k]]
            unranked = True
        else:
            ranked.sort(key=lambda t: t[0], reverse=True)
            ranked = ranked[: top_k]
            unranked = False

        evidence_tuple = tuple(text for _, text, _ in ranked)
        # Confidence: average score of returned items, capped.  With no
        # parsed scores we fall back to the candidate-count heuristic.
        if unranked:
            score_avg = 0.4
        else:
            score_avg = sum(s for s, _, _ in ranked) / max(1, len(ranked))

        return ReasoningResult(
            mode="analogical",
            conclusion=(
                f"Identified {len(evidence_tuple)} similar historical "
                "situation(s)."
            ),
            confidence=_cap(score_avg, self._cap_value),
            evidence=evidence_tuple,
            metadata={
                "graph_hits": len(graph_candidates),
                "bank_hits": len(bank_candidates),
                "unranked": unranked,
            },
        )

    async def counterfactual(
        self,
        decision_point: str,
        alternative: str,
    ) -> ReasoningResult:
        """If we'd done X (alternative) instead of Y (decision_point)…"""
        if self._is_weak_tier():
            return _empty_result(
                "counterfactual",
                reason="iron_rule_3_weak_tier_skipped",
                extra_metadata={"tier": self._tier},
            )

        # Pull similar historical events as grounding.  Critical anti-
        # hallucination move: the prompt explicitly tells the LLM not
        # to invent facts beyond this list.
        history = await self._collect_event_candidates(
            decision_point, top_k=3
        )
        history_block = (
            self._format_candidates(history)
            if history
            else "(no similar history found)"
        )

        prompt = _COUNTERFACTUAL_PROMPT.format(
            actual=decision_point,
            alternative=alternative,
            history_block=history_block,
        )
        raw = await self._llm_complete(prompt)
        parsed = _try_parse_json(raw)
        if not isinstance(parsed, dict):
            return _empty_result(
                "counterfactual",
                reason="bad_llm_json",
                extra_metadata={"llm_raw": (raw or "")[:200]},
            )

        outcome = str(parsed.get("outcome", "")).strip()
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        key_diffs = _coerce_str_tuple(parsed.get("key_differences"))
        history_texts = tuple(text for _, text in history)

        return ReasoningResult(
            mode="counterfactual",
            conclusion=outcome,
            confidence=_cap(confidence, self._cap_value),
            evidence=history_texts + key_diffs,
            metadata={
                "actual": decision_point,
                "alternative": alternative,
                "graph_hits": len(history),
            },
        )

    async def meta(self, query: str) -> ReasoningResult:
        """Self-assess knowledge sufficiency + propose perceive/experiment goals."""
        if self._is_weak_tier():
            return _empty_result(
                "meta",
                reason="iron_rule_3_weak_tier_skipped",
                extra_metadata={"tier": self._tier},
            )

        prompt = _META_PROMPT.format(query=query)
        raw = await self._llm_complete(prompt)
        parsed = _try_parse_json(raw)
        if not isinstance(parsed, dict):
            return _empty_result(
                "meta",
                reason="bad_llm_json",
                extra_metadata={"llm_raw": (raw or "")[:200]},
            )

        sufficient = bool(parsed.get("sufficient", False))
        gap = str(parsed.get("gap", "")).strip()
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        suggested = _coerce_str_tuple(parsed.get("suggested_goals"))

        # Only propose goals when the LLM admits insufficiency — we
        # don't want gratuitous goals churned out for trivial queries.
        if sufficient:
            suggested = ()

        conclusion = (
            "Sufficient knowledge to answer."
            if sufficient
            else f"Knowledge gap: {gap}" if gap else "Knowledge insufficient."
        )

        return ReasoningResult(
            mode="meta",
            conclusion=conclusion,
            confidence=_cap(confidence, self._cap_value),
            evidence=(gap,) if gap else (),
            suggested_goals=suggested,
            metadata={"sufficient": sufficient},
        )

    # --- internals ---

    async def _auto(self, query: str) -> ReasoningResult:
        """Meta-router: ask the LLM which mode best fits, then dispatch.

        On weak tier we cannot trust the meta hint, so we fall through
        to ``analogical`` (the only branch that survives weak-tier
        gating).
        """
        if self._is_weak_tier():
            result = await self.analogical(query)
            return _replace_metadata(
                result, routed_from="auto", tier=self._tier
            )

        prompt = _AUTO_ROUTE_PROMPT.format(query=query)
        raw = await self._llm_complete(prompt)
        parsed = _try_parse_json(raw)
        chosen: ReasoningMode = "meta"
        if isinstance(parsed, dict):
            mode_hint = str(parsed.get("mode", "")).strip().lower()
            if mode_hint in ("causal", "analogical", "counterfactual", "meta"):
                chosen = mode_hint  # type: ignore[assignment]

        if chosen == "causal":
            result = await self.causal(query, evidence=[])
        elif chosen == "analogical":
            result = await self.analogical(query)
        elif chosen == "counterfactual":
            result = await self.counterfactual(query, alternative="")
        else:
            result = await self.meta(query)

        return _replace_metadata(result, routed_from="auto")

    async def _llm_complete(self, prompt: str) -> str:
        """Wrap LLM call with logging.  Empty / exception → "" so the
        caller's JSON parse returns ``None`` and we fall through to
        :func:`_empty_result`."""
        if self._llm is None:
            return ""
        try:
            out = await self._llm.complete(prompt)
        except Exception as exc:  # noqa: BLE001 — never let LLM crash us
            _log.warning("ReasoningEngine LLM call failed: %s", exc)
            return ""
        if out is None:
            return ""
        return str(out)

    async def _collect_causal_evidence(self, hypothesis: str) -> list[str]:
        """Pull CAUSED_BY-edge neighbours of recent events whose
        content overlaps with the hypothesis.  Cheap heuristic; the
        LLM is what actually validates causality."""
        if self._graph is None:
            return []
        try:
            events = await self._graph.query_by_type("event", limit=10)
        except Exception as exc:  # noqa: BLE001
            _log.debug("graph.query_by_type failed: %s", exc)
            return []

        hits: list[str] = []
        hypo_terms = _tokenize(hypothesis)
        for event in events:
            content = getattr(event, "content", "") or ""
            if not _terms_overlap(content, hypo_terms):
                continue
            event_id = getattr(event, "id", None)
            if event_id is None:
                continue
            try:
                neighbours = await self._graph.get_neighbors(
                    event_id, relation="CAUSED_BY", depth=1, min_strength=0.0
                )
            except Exception as exc:  # noqa: BLE001
                _log.debug("graph.get_neighbors failed: %s", exc)
                continue
            for _edge, neighbour in neighbours:
                neighbour_text = getattr(neighbour, "content", "")
                if neighbour_text:
                    hits.append(f"{content} CAUSED_BY {neighbour_text}")
            if not neighbours:
                hits.append(content)
        return hits

    async def _collect_event_candidates(
        self, situation: str, *, top_k: int
    ) -> list[tuple[str, str]]:
        """Return ``[(node_id, content), ...]`` candidates from the
        graph.  We pull recent events first, then expand via 1-hop
        ``RELATED_TO`` to surface adjacent context."""
        if self._graph is None:
            return []
        try:
            events = await self._graph.query_by_type("event", limit=top_k * 2)
        except Exception as exc:  # noqa: BLE001
            _log.debug("graph.query_by_type failed: %s", exc)
            return []
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for event in events:
            event_id = getattr(event, "id", "") or ""
            content = getattr(event, "content", "") or ""
            if event_id in seen or not content:
                continue
            seen.add(event_id)
            out.append((event_id, content))
            if len(out) >= top_k:
                break
        return out

    async def _collect_strategy_candidates(
        self, situation: str, *, top_k: int
    ) -> list[tuple[str, str]]:
        """Return ``[(strategy_id, when→then text), ...]`` from the
        StrategyBank.  Empty if no bank wired in."""
        if self._bank is None:
            return []
        try:
            strategies = await self._bank.retrieve(situation, limit=top_k)
        except Exception as exc:  # noqa: BLE001
            _log.debug("bank.retrieve failed: %s", exc)
            return []
        out: list[tuple[str, str]] = []
        for s in strategies:
            sid = getattr(s, "id", "") or "?"
            when = getattr(s, "when_pattern", "") or ""
            then = getattr(s, "then_action", "") or ""
            text = f"WHEN {when} THEN {then}".strip()
            if text:
                out.append((sid, text))
        return out

    @staticmethod
    def _format_evidence(items: list[str]) -> str:
        return "\n".join(f"  - {it}" for it in items if it.strip()) or "  (none)"

    @staticmethod
    def _format_candidates(items: list[tuple[str, str]]) -> str:
        if not items:
            return "  (no candidates)"
        return "\n".join(
            f"  [{idx}] {text}" for idx, (_id, text) in enumerate(items)
        )


# --- helpers ---


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Best-effort: turn an arbitrary LLM-parsed value into a tuple of
    strings.  Used for ``key_points`` / ``suggested_goals`` etc."""
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return tuple(out)


def _replace_metadata(
    result: ReasoningResult, **extra: Any
) -> ReasoningResult:
    """Return a copy of ``result`` with ``extra`` merged into metadata.

    Used by ``auto`` to tag results with ``routed_from='auto'`` so the
    caller can tell the engine routed them.
    """
    merged = dict(result.metadata)
    merged.update(extra)
    return ReasoningResult(
        mode=result.mode,
        conclusion=result.conclusion,
        confidence=result.confidence,
        evidence=result.evidence,
        suggested_goals=result.suggested_goals,
        metadata=merged,
    )


def _tokenize(s: str) -> set[str]:
    """Cheap whitespace-split tokeniser for evidence overlap heuristic."""
    return {t for t in (w.strip(".,!?;:()[]{}\"'").lower() for w in s.split()) if len(t) > 2}


def _terms_overlap(content: str, hypo_terms: set[str]) -> bool:
    """True if any term in ``hypo_terms`` appears in ``content``."""
    if not hypo_terms:
        return True
    content_terms = _tokenize(content)
    return bool(content_terms & hypo_terms)
