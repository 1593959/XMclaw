"""Reformer — turns Pattern findings into actionable proposals.

The boundary between Pattern and ReformProposal is intentional:
* ``Pattern`` is descriptive ("agent declines too eagerly").
* ``ReformProposal`` is prescriptive ("add this curriculum hint" /
  "propose this skill" / "update user preference").

This separation lets a future operator review patterns BEFORE
proposals materialise — avoiding silent agent self-modification.
The Reformer is **fail-closed**: when in doubt it does nothing.

Three proposal kinds:

* ``curriculum_edit`` — patches the agent's guidance prompt with a
  one-liner addendum. Example: "When the user says 'help', prefer
  to attempt rather than decline." Routed via the existing
  ``propose_curriculum_edit`` pipeline (Sprint 3).
* ``skill_propose`` — drafts a new skill body solving a missed
  opportunity. Routed through ``SkillProposer`` so the existing
  EvolutionController grader gate can promote-or-reject.
* ``preference_update`` — appends a fact to USER.md (persona file).
  Mirrors what the existing ``ExtractFactsHook`` does on per-turn
  basis but at the pattern-level.

All proposals get emitted as ``METACOGNITION_PROPOSAL`` events for
the UI to surface; routing is best-effort.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


ReformKind = Literal[
    "curriculum_edit",
    "skill_propose",
    "preference_update",
    "no_op",
]


@dataclass(frozen=True, slots=True)
class ReformProposal:
    """One concrete change the Reformer recommends.

    Frozen so a downstream "approve" handler can't mutate it before
    persistence. Materialisation is the next step (the Reformer
    only constructs proposals; routing them through Evolution /
    PersonaStore is the daemon's job).
    """
    kind: ReformKind
    pattern_summary: str
    payload: dict[str, Any]
    confidence: float
    why: str  # one-line "why this proposal address that pattern"


class Reformer:
    """Stateless mapper Pattern → ReformProposal.

    Each Pattern.kind maps to a default ReformProposal.kind:

        Pattern.kind                     →  ReformProposal.kind
        ──────────────────────────────────────────────────────────
        repeated_failure                 →  curriculum_edit
        decline_overuse                  →  curriculum_edit
        missed_opportunity               →  skill_propose
        user_pushback_pattern            →  preference_update
        answer_style_mismatch            →  preference_update

    Returns ``no_op`` if the pattern's confidence is below
    ``min_confidence`` (default 0.3) — too uncertain to act on.
    """

    def __init__(self, *, min_confidence: float = 0.3) -> None:
        self._min_confidence = max(0.0, min(1.0, float(min_confidence)))

    def propose(self, pattern: Any) -> ReformProposal:
        """Map a Pattern to a ReformProposal. Callers feed in the
        full ``Pattern`` (duck-typed: ``.kind``, ``.summary``,
        ``.confidence``, ``.suggestion``, ``.evidence``)."""
        confidence = float(getattr(pattern, "confidence", 0.0))
        summary = str(getattr(pattern, "summary", ""))
        suggestion = str(getattr(pattern, "suggestion", ""))
        kind = str(getattr(pattern, "kind", ""))

        if confidence < self._min_confidence:
            return ReformProposal(
                kind="no_op",
                pattern_summary=summary,
                payload={"reason": "confidence_below_threshold"},
                confidence=confidence,
                why=(
                    f"confidence={confidence:.2f} < "
                    f"min={self._min_confidence:.2f}"
                ),
            )

        if kind in ("repeated_failure", "decline_overuse"):
            return ReformProposal(
                kind="curriculum_edit",
                pattern_summary=summary,
                payload={
                    "addendum": suggestion or summary,
                    "tag": kind,
                    "evidence_count": len(
                        getattr(pattern, "evidence", []) or [],
                    ),
                },
                confidence=confidence,
                why=(
                    f"reform via curriculum_edit because "
                    f"pattern.kind={kind}"
                ),
            )

        if kind == "missed_opportunity":
            return ReformProposal(
                kind="skill_propose",
                pattern_summary=summary,
                payload={
                    "draft_intent": suggestion or summary,
                    "tag": kind,
                    "evidence_count": len(
                        getattr(pattern, "evidence", []) or [],
                    ),
                },
                confidence=confidence,
                why="missed_opportunity → propose new skill",
            )

        if kind in ("user_pushback_pattern", "answer_style_mismatch"):
            return ReformProposal(
                kind="preference_update",
                pattern_summary=summary,
                payload={
                    "fact": suggestion or summary,
                    "tag": kind,
                    "section": "USER.md",
                    "evidence_count": len(
                        getattr(pattern, "evidence", []) or [],
                    ),
                },
                confidence=confidence,
                why=f"persona update because pattern.kind={kind}",
            )

        # Unknown kind — fail closed.
        return ReformProposal(
            kind="no_op",
            pattern_summary=summary,
            payload={"reason": f"unrecognised_pattern_kind:{kind}"},
            confidence=confidence,
            why=f"unrecognised pattern.kind={kind!r}",
        )

    @staticmethod
    async def emit(
        proposal: ReformProposal,
        *,
        bus: Any | None,
        agent_id: str = "metacognition",
    ) -> None:
        """Publish the proposal as METACOGNITION_PROPOSAL for the UI
        to render. Best-effort. Materialisation (actually applying
        the change) is the daemon's responsibility — callers route
        approved proposals into the existing Evolution / Persona
        pipelines."""
        if bus is None or proposal.kind == "no_op":
            return
        try:
            from xmclaw.core.bus import EventType, make_event
            try:
                ev_type = EventType("metacognition_proposal")
            except ValueError:
                # Schema doesn't have it yet — skip silently. Lets
                # the Reformer ship before the EventType enum has
                # the new constant.
                return
            await bus.publish(make_event(
                session_id="_system",
                agent_id=agent_id,
                type=ev_type,
                payload={
                    "kind": proposal.kind,
                    "pattern_summary": proposal.pattern_summary,
                    "payload": proposal.payload,
                    "confidence": proposal.confidence,
                    "why": proposal.why,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reformer.emit_failed kind=%s err=%s",
                proposal.kind, exc,
            )


__all__ = ["Reformer", "ReformKind", "ReformProposal"]
