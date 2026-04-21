"""HonestGrader — compose ground-truth checks into a weighted verdict.

Weights (anti-req #4):
  ran                       → 0.30
  returned                  → 0.20
  type_matched              → 0.25
  side_effect_observable    → 0.15  (None = not applicable; redistribute)
  llm_judge_opinion         → 0.20  (ceiling)

``side_effect_observable == None`` means the tool declared no side effect,
so that slot is not applicable. Its weight is redistributed proportionally
across the other hard checks — the LLM opinion never benefits from a
missing hard check (anti-req #4 keeps it strictly capped at 0.20).

``score`` is the weighted sum ∈ [0, 1]. Evidence from every check is
concatenated into the verdict's evidence list.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.core.grader.checks import (
    check_ran,
    check_returned,
    check_side_effect_observable,
    check_type_matched,
)


@dataclass(frozen=True, slots=True)
class GraderVerdict:
    event_id: str
    ran: bool
    returned: bool
    type_matched: bool
    side_effect_observable: bool | None
    llm_judge_opinion: str | None
    score: float
    evidence: list[str] = field(default_factory=list)


# Hard checks (evidence-based, cannot be faked by the model).
# Weights MUST sum to 1.0 — they fill the ``1 - _LLM_OPINION_CAP`` slice.
_HARD_WEIGHTS: dict[str, float] = {
    "ran": 0.30,
    "returned": 0.20,
    "type_matched": 0.25,
    "side_effect_observable": 0.25,
}
# Soft: ceiling only, never redistributed when hard checks opt out.
_LLM_OPINION_CAP: float = 0.20
assert abs(sum(_HARD_WEIGHTS.values()) - 1.0) < 1e-9, "hard weights must sum to 1.0"


class HonestGrader:
    """Grade events with ground-truth checks first; LLM opinion strictly capped.

    Design is intentionally simple: pure function over the event payload. No
    hidden state, no LLM call inside ``grade`` — the LLM's own opinion only
    enters via whatever the caller already stored in the event payload. This
    keeps anti-requirement #4 enforceable by inspection: the LLM cannot give
    itself a high score because its opinion is capped here.
    """

    async def grade(self, event: BehavioralEvent) -> GraderVerdict:
        ran, ev_ran = check_ran(event)
        returned, ev_ret = check_returned(event)
        type_matched, ev_type = check_type_matched(event)
        side_ok, ev_side = check_side_effect_observable(event)

        evidence: list[str] = []
        evidence.extend(f"ran: {x}" for x in ev_ran)
        evidence.extend(f"returned: {x}" for x in ev_ret)
        evidence.extend(f"type: {x}" for x in ev_type)
        evidence.extend(f"side: {x}" for x in ev_side)

        # Compute hard-check sub-score.
        if side_ok is None:
            # Not applicable; redistribute side_effect_observable's weight
            # proportionally across the other hard checks.
            weight_other = 1.0 - _HARD_WEIGHTS["side_effect_observable"]
            hard_weights = {
                k: w / weight_other
                for k, w in _HARD_WEIGHTS.items()
                if k != "side_effect_observable"
            }
            hard_values = {
                "ran": ran, "returned": returned, "type_matched": type_matched,
            }
        else:
            hard_weights = _HARD_WEIGHTS
            hard_values = {
                "ran": ran, "returned": returned, "type_matched": type_matched,
                "side_effect_observable": side_ok,
            }

        hard_score = sum(
            hard_weights[k] * (1.0 if hard_values[k] else 0.0) for k in hard_weights
        )
        # Hard checks fill (1 - LLM_OPINION_CAP) of the total; LLM opinion fills
        # up to the cap. This guarantees LLM opinion cannot exceed 0.20.
        hard_share = 1.0 - _LLM_OPINION_CAP
        total = hard_score * hard_share

        opinion_text = event.payload.get("llm_judge_opinion")
        opinion_score = event.payload.get("llm_judge_score")
        if opinion_score is not None:
            opinion_score = max(0.0, min(1.0, float(opinion_score)))
            total += opinion_score * _LLM_OPINION_CAP
            evidence.append(f"llm_opinion_score={opinion_score:.2f} (cap={_LLM_OPINION_CAP})")

        # Clamp to [0, 1] (paranoia — shouldn't overflow but keep invariant).
        total = max(0.0, min(1.0, total))

        return GraderVerdict(
            event_id=event.id,
            ran=ran,
            returned=returned,
            type_matched=type_matched,
            side_effect_observable=side_ok,
            llm_judge_opinion=opinion_text,
            score=total,
            evidence=evidence,
        )
