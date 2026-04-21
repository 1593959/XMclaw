"""HonestGrader — verdict aggregator.

Weights (anti-req #4):
  ran                       → 0.30
  returned                  → 0.20
  type_matched              → 0.25
  side_effect_observable    → 0.15  (None = not applicable, skip+reweight)
  llm_judge_opinion         → 0.20 ceiling

Score is weighted sum ∈ [0, 1]. ``evidence`` contains replay-able pointers
(file paths, exit codes, URLs). An event with no evidence must get score 0
when its checks require evidence — the grader refuses to pass on trust.

Phase 1: stub. First functional impl lands with Phase 1 demo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from xmclaw.core.bus.events import BehavioralEvent


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


class HonestGrader:
    """Grade events with ground-truth checks first, LLM opinion last."""

    WEIGHTS = {
        "ran": 0.30,
        "returned": 0.20,
        "type_matched": 0.25,
        "side_effect_observable": 0.15,
        "llm_judge_opinion": 0.20,
    }

    async def grade(self, event: BehavioralEvent) -> GraderVerdict:  # noqa: ARG002
        raise NotImplementedError(
            "HonestGrader.grade lands in Phase 1; see V2_DEVELOPMENT.md §8.1"
        )
