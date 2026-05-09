"""HonestGrader — Sprint 3 Iron Rule #1 multi-signal rewrite.

Iron Rule #1: any promotion needs ≥2 INDEPENDENT signals; never
single LLM-judge. The audit identified the prior HonestGrader's #1
weakness: 70-80% of every score came from "tool didn't crash"
(``check_ran`` was trivially true; ``check_type_matched`` defaulted
True when no expected type was declared; ``check_side_effect`` only
checked fs writes), and the LLM self-rating was capped at 0.20 but
still gameable. Net effect: every "evolution" was just "tool returned
non-error", leading to spurious promotions. Hermes died on exactly
this pattern (single-signal echo chamber, 107 Reddit upvotes for "It
always thinks it did a good job. ALWAYS").

This rewrite splits the grader into TWO INDEPENDENT signal layers:

* **Signal A — deterministic (this file + ``checks.py``)**:
  ``ran`` / ``returned`` / ``type_matched`` / ``side_effect``.
  Tightened so ``ran`` requires non-trivial output (rejects empty /
  whitespace / "ok" / "done" / "true"), ``type_matched`` returns
  ``None`` (not True) when no type was declared, and ``side_effect``
  covers fs + memory + bus emissions.

* **Signal B — independent (``_signals.py``)**: at least one of
  :class:`UserFollowupSignal` / :class:`HoldoutTestSignal` /
  :class:`CrossJudgeSignal` must fire. ``UserFollowupSignal`` is the
  only signal fully implemented today; the other two are stubs the
  abstraction is shaped around. Stubbed signals return ``None`` —
  they do NOT lower the score; they just fail to satisfy Iron Rule
  #1 on their own.

Combined score:

  * If only Signal A available: ``final = deterministic_score``,
    ``promote_eligible = False`` (Iron Rule #1).
  * If both available: ``final = 0.6 * deterministic + 0.4 *
    independent``, ``promote_eligible = (deterministic >= 0.6 AND
    independent >= 0.5)``.

The combiner produces :class:`GraderVerdict` with:
  - both signal scores (so the bus / audit can see them separately),
  - ``promote_eligible`` (the Iron Rule #1 gate consumed by the
    EvolutionController),
  - ``notes`` (human-readable reasons for ``xmclaw evolve review``),
  - ``score`` (the combined float — the legacy single-number contract,
    kept so today's UCB1 and other consumers don't break).

Backward compat: a single-number ``score`` field is still on the
verdict, and ``HonestGrader._legacy_score()`` returns the old-style
deterministic float for callers that have not yet migrated. Both are
marked deprecated in their docstring; the controller MUST consume
``promote_eligible``, not the score, going forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.core.grader._signals import (
    CrossJudgeSignal,
    HoldoutTestSignal,
    IndependentSignal,
    UserFollowupSignal,
    best_independent_score,
)
from xmclaw.core.grader.checks import (
    check_ran,
    check_returned,
    check_side_effect_observable,
    check_type_matched,
)


# ── public verdict shape ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GraderVerdict:
    """Multi-signal verdict — Sprint 3 contract.

    Carries each signal SEPARATELY so promotions never collapse onto
    a single combined score. Aggregating early was the original
    weakness; the controller's gate now reads ``promote_eligible``,
    not ``score``.
    """

    event_id: str
    # ── Signal A ──
    deterministic_score: float           # 0.0–1.0
    deterministic_evidence: dict[str, Any]
    ran: bool
    returned: bool
    type_matched: bool | None            # None = not applicable
    side_effect_observable: bool | None  # None = not applicable
    # ── Signal B ──
    independent_score: float | None      # None = no independent signal fired
    independent_kind: str                # "user_followup" | "holdout_test" | "cross_judge_agreement" | "none"
    independent_evidence: dict[str, Any]
    # ── combined ──
    final_score: float                   # 0.0–1.0 — the audit-grade single number
    promote_eligible: bool               # Iron Rule #1 gate
    notes: list[str] = field(default_factory=list)
    # ── back-compat (so existing UCB1 + audit log readers keep working) ──
    llm_judge_opinion: str | None = None
    evidence: list[str] = field(default_factory=list)

    # B-???: the prior code consumed ``verdict.score``. Keep that name
    # working alongside the new ``final_score`` so call sites can
    # migrate incrementally without a flag-day rename.
    @property
    def score(self) -> float:
        return self.final_score

    def to_payload(self) -> dict[str, Any]:
        """Serialize the verdict for a ``GRADER_VERDICT`` bus event.

        The shape is stable — payload readers (UI, audit log,
        EvolutionAgent aggregator) can rely on these fields. New
        fields are additive; never rename without bumping the bus
        schema version per ``xmclaw/core/AGENTS.md`` §4.
        """
        return {
            "event_id": self.event_id,
            "deterministic_score": self.deterministic_score,
            "deterministic_evidence": dict(self.deterministic_evidence),
            "ran": self.ran,
            "returned": self.returned,
            "type_matched": self.type_matched,
            "side_effect_observable": self.side_effect_observable,
            "independent_score": self.independent_score,
            "independent_kind": self.independent_kind,
            "independent_evidence": dict(self.independent_evidence),
            "final_score": self.final_score,
            "score": self.final_score,  # legacy alias
            "promote_eligible": self.promote_eligible,
            "notes": list(self.notes),
            "llm_judge_opinion": self.llm_judge_opinion,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GraderVerdict":
        """Round-trip from a ``GRADER_VERDICT`` event back to a verdict.

        Used by the audit log replay + the ``xmclaw evolve review``
        UI. Tolerant of missing fields (defaults applied) so legacy
        verdicts emitted before this version remain readable.
        """
        return cls(
            event_id=str(payload.get("event_id", "")),
            deterministic_score=float(payload.get("deterministic_score", 0.0)),
            deterministic_evidence=dict(payload.get("deterministic_evidence") or {}),
            ran=bool(payload.get("ran", False)),
            returned=bool(payload.get("returned", False)),
            type_matched=payload.get("type_matched"),
            side_effect_observable=payload.get("side_effect_observable"),
            independent_score=payload.get("independent_score"),
            independent_kind=str(payload.get("independent_kind", "none")),
            independent_evidence=dict(payload.get("independent_evidence") or {}),
            final_score=float(payload.get("final_score", payload.get("score", 0.0))),
            promote_eligible=bool(payload.get("promote_eligible", False)),
            notes=list(payload.get("notes") or []),
            llm_judge_opinion=payload.get("llm_judge_opinion"),
            evidence=list(payload.get("evidence") or []),
        )


# ── grader engine ─────────────────────────────────────────────────────────


# Signal A weights — applied across the *applicable* checks only.
# Pre-Sprint-3 this redistributed when one check was N/A but still
# awarded credit when ``type_matched`` defaulted to True. Now any
# check that returns ``None`` is dropped from the denominator AND
# numerator, so we never award free points.
_SIGNAL_A_BASE_WEIGHTS: dict[str, float] = {
    "ran": 0.40,            # most important — non-trivial execution
    "returned": 0.20,       # raw output presence (looser than ran)
    "type_matched": 0.20,   # declared shape match
    "side_effect_observable": 0.20,  # fs / memory / bus mutation
}
assert abs(sum(_SIGNAL_A_BASE_WEIGHTS.values()) - 1.0) < 1e-9, (
    "Signal A weights must sum to 1.0"
)

# Combined-score weighting (Iron Rule #1 multi-signal blend):
_DET_WEIGHT: float = 0.6  # deterministic dominates (it's the harder evidence)
_IND_WEIGHT: float = 0.4

# Iron Rule #1 promote-eligibility thresholds.
_PROMOTE_DETERMINISTIC_FLOOR: float = 0.6
_PROMOTE_INDEPENDENT_FLOOR: float = 0.5

# Legacy LLM-opinion cap retained for ``_legacy_score`` only. The
# new combined score path does NOT use it — LLM self-judge is now
# only allowed to enter via :class:`CrossJudgeSignal` which already
# treats disagreement as a NEGATIVE signal.
_LLM_OPINION_CAP_LEGACY: float = 0.20


class HonestGrader:
    """Multi-signal grader — never single-signal-promotes.

    Construction params:
      ``signals`` — independent signal probes to try, in order. The
      first one that fires (returns a non-``None`` score) wins for
      this event. Default is ``[UserFollowupSignal(), HoldoutTestSignal(),
      CrossJudgeSignal()]`` — the only fully-implemented signal goes
      first so its evidence is what shows up in audit logs by
      default.

    The grader's :meth:`grade` is async because the signal probes
    are async (some real-world implementations will need to await
    journal lookups or judge LLM calls). The deterministic checks
    remain synchronous.
    """

    def __init__(
        self,
        signals: Iterable[IndependentSignal] | None = None,
    ) -> None:
        self._signals: tuple[IndependentSignal, ...] = tuple(
            signals if signals is not None else (
                UserFollowupSignal(),
                HoldoutTestSignal(),
                CrossJudgeSignal(),
            )
        )

    @property
    def signals(self) -> tuple[IndependentSignal, ...]:
        return self._signals

    # ── top-level entry point ────────────────────────────────────────

    async def grade(
        self,
        event: BehavioralEvent,
        *,
        history: Iterable[BehavioralEvent] | None = None,
    ) -> GraderVerdict:
        """Produce a multi-signal verdict for ``event``.

        ``history`` is an iterable of recent events from the same
        session — used by signals like :class:`UserFollowupSignal` to
        check whether the user reacted positively/negatively after
        this event. Pass ``None`` (the default) when no history is
        available; the verdict will simply note "independent signal
        not applicable" and ``promote_eligible`` will be False.
        """
        det_score, det_ev_dict, ran, returned, type_matched, side_ok = (
            self._signal_a(event)
        )

        ind_score, ind_kind, ind_ev = await best_independent_score(
            self._signals, event, history=history,
        )

        final_score, eligible, notes = self._combine(
            det_score=det_score,
            ind_score=ind_score,
            ind_kind=ind_kind,
        )

        # Flat evidence list (back-compat: prior consumers expected
        # one stringly-typed list). Keep it short — the structured
        # dicts are the canonical representation now.
        flat_evidence: list[str] = []
        flat_evidence.append(f"deterministic_score={det_score:.3f}")
        if ind_score is not None:
            flat_evidence.append(
                f"independent_score={ind_score:.3f} kind={ind_kind}"
            )
        else:
            flat_evidence.append("independent_score=None (no signal applicable)")
        flat_evidence.append(
            f"final={final_score:.3f} promote_eligible={eligible}"
        )

        return GraderVerdict(
            event_id=event.id,
            deterministic_score=det_score,
            deterministic_evidence=det_ev_dict,
            ran=ran,
            returned=returned,
            type_matched=type_matched,
            side_effect_observable=side_ok,
            independent_score=ind_score,
            independent_kind=ind_kind,
            independent_evidence=ind_ev,
            final_score=final_score,
            promote_eligible=eligible,
            notes=notes,
            llm_judge_opinion=event.payload.get("llm_judge_opinion"),
            evidence=flat_evidence,
        )

    # ── Signal A (deterministic) ─────────────────────────────────────

    @staticmethod
    def _signal_a(
        event: BehavioralEvent,
    ) -> tuple[float, dict[str, Any], bool, bool, bool | None, bool | None]:
        """Run the deterministic checks and weight the applicable ones.

        Returns ``(score, evidence_dict, ran, returned, type_matched,
        side_ok)``. Checks that return ``None`` are dropped from BOTH
        numerator and denominator — never awarded free points.
        """
        ran, ev_ran = check_ran(event)
        returned, ev_ret = check_returned(event)
        type_matched, ev_type = check_type_matched(event)
        side_ok, ev_side = check_side_effect_observable(event)

        applicable: dict[str, bool] = {"ran": ran, "returned": returned}
        # type_matched / side_effect can be None — drop those.
        if type_matched is not None:
            applicable["type_matched"] = type_matched
        if side_ok is not None:
            applicable["side_effect_observable"] = side_ok

        # Re-normalize weights across applicable checks.
        weight_sum = sum(
            _SIGNAL_A_BASE_WEIGHTS[k] for k in applicable
        )
        if weight_sum <= 0:
            score = 0.0
        else:
            score = sum(
                (_SIGNAL_A_BASE_WEIGHTS[k] / weight_sum) * (1.0 if v else 0.0)
                for k, v in applicable.items()
            )

        evidence: dict[str, Any] = {
            "ran": {"value": ran, "evidence": ev_ran},
            "returned": {"value": returned, "evidence": ev_ret},
            "type_matched": {"value": type_matched, "evidence": ev_type},
            "side_effect_observable": {"value": side_ok, "evidence": ev_side},
            "applicable_checks": list(applicable.keys()),
            "weight_sum": weight_sum,
        }
        return score, evidence, ran, returned, type_matched, side_ok

    # ── Combiner ─────────────────────────────────────────────────────

    @staticmethod
    def _combine(
        *,
        det_score: float,
        ind_score: float | None,
        ind_kind: str,
    ) -> tuple[float, bool, list[str]]:
        """Compute final score + Iron Rule #1 promote_eligible.

        Returns ``(final_score, promote_eligible, notes)``.
        """
        notes: list[str] = []
        if ind_score is None:
            # Single-signal only → not promote-eligible (Iron Rule #1).
            final = det_score
            notes.append(
                "single-signal verdict: only deterministic checks fired; "
                "promotion BLOCKED per Iron Rule #1 (need ≥2 independent signals)"
            )
            return final, False, notes

        final = _DET_WEIGHT * det_score + _IND_WEIGHT * ind_score
        # Belt-and-braces clamp.
        final = max(0.0, min(1.0, final))

        if det_score < _PROMOTE_DETERMINISTIC_FLOOR:
            notes.append(
                f"deterministic_score={det_score:.3f} below "
                f"floor={_PROMOTE_DETERMINISTIC_FLOOR}; promotion BLOCKED"
            )
            return final, False, notes

        if ind_score < _PROMOTE_INDEPENDENT_FLOOR:
            notes.append(
                f"independent_score={ind_score:.3f} (kind={ind_kind}) below "
                f"floor={_PROMOTE_INDEPENDENT_FLOOR}; promotion BLOCKED"
            )
            return final, False, notes

        notes.append(
            f"both signals passed thresholds (det>={_PROMOTE_DETERMINISTIC_FLOOR}, "
            f"ind>={_PROMOTE_INDEPENDENT_FLOOR}); promote_eligible=True"
        )
        return final, True, notes

    # ── legacy (deprecated) ──────────────────────────────────────────

    def _legacy_score(self, event: BehavioralEvent) -> float:
        """Pre-Sprint-3 deterministic-only score.

        DEPRECATED. Return value is the legacy weighted blend that
        also folds in an LLM-opinion cap. Kept available so callers
        that haven't migrated to the multi-signal verdict can read a
        single float without changing shape. New code MUST go through
        :meth:`grade` and check ``verdict.promote_eligible``.
        """
        ran, _ = check_ran(event)
        returned, _ = check_returned(event)
        type_matched, _ = check_type_matched(event)
        side_ok, _ = check_side_effect_observable(event)

        # Legacy weighting matches the pre-Sprint-3 file exactly so a
        # caller that depended on the numeric value gets the same
        # answer (within float tolerance).
        legacy_weights = {
            "ran": 0.30,
            "returned": 0.20,
            "type_matched": 0.25,
            "side_effect_observable": 0.25,
        }
        # Pre-Sprint-3 default: missing type_matched counted True,
        # missing side_effect redistributed.
        tm_value = True if type_matched is None else type_matched
        if side_ok is None:
            denom = 1.0 - legacy_weights["side_effect_observable"]
            normalized = {
                k: w / denom for k, w in legacy_weights.items()
                if k != "side_effect_observable"
            }
            values = {"ran": ran, "returned": returned, "type_matched": tm_value}
        else:
            normalized = legacy_weights
            values = {
                "ran": ran, "returned": returned, "type_matched": tm_value,
                "side_effect_observable": bool(side_ok),
            }

        hard = sum(
            normalized[k] * (1.0 if values[k] else 0.0) for k in normalized
        )
        hard_share = 1.0 - _LLM_OPINION_CAP_LEGACY
        total = hard * hard_share

        opinion_score = event.payload.get("llm_judge_score")
        if opinion_score is not None:
            opinion_score = max(0.0, min(1.0, float(opinion_score)))
            total += opinion_score * _LLM_OPINION_CAP_LEGACY

        return max(0.0, min(1.0, total))


__all__ = ["GraderVerdict", "HonestGrader"]
