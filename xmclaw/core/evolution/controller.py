"""EvolutionController — autonomous promotion based on scheduler evidence.

Closes the loop that Phase 1, 2, and 3.1/3.2 assembled in pieces:

    bench → scheduler.arm_stats → EvolutionController.consider_promotion
        → registry.promote (with evidence) → HEAD moves
        → next session starts from the promoted baseline

Decision logic (deliberately conservative):

  Input: a ``CandidateEvaluation`` for each arm the scheduler has tried
  (plays, mean score, optional structural/domain breakdown).

  For each non-HEAD candidate, it must clear ALL four thresholds:

    1. plays       ≥ ``min_plays``          (enough signal)
    2. mean_score  ≥ ``min_mean``           (absolute quality floor)
    3. gap_over_head ≥ ``min_gap_over_head`` (relative improvement)
    4. best_minus_second ≥ ``min_gap_over_second`` (statistical
        separation from the runner-up, so we don't promote on noise)

  If no candidate clears, return ``NO_CHANGE``. If one clears, return
  a ``PROMOTE`` decision carrying the evidence strings that would be
  passed verbatim to ``registry.promote(evidence=...)``.

  Anti-req #12 is structurally enforced: this module NEVER calls
  registry.promote itself. It only returns a decision + evidence. The
  orchestrator is responsible for the actual registry mutation — that
  way, if someone later wires up a "skip-the-check" path, the evidence
  list is still required at the registry door.

Phase 3.3 promotes on summary-skill level: a new skill version freezes
the winning prompt variant. Phase 4 generalizes to any dimension
(temperature, tool choice, model selection).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EvolutionDecision(str, Enum):
    PROMOTE = "promote"
    NO_CHANGE = "no_change"
    ROLLBACK = "rollback"   # reserved for Phase 3.4 — monitors head regression


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    """Conservative defaults — tune up for tighter promotion gates.

    Every default was chosen so the Phase 1 live bench (MiniMax, 40 turns,
    6 arms) would plausibly trigger at most one promotion per session.
    """

    min_plays: int = 10
    min_mean: float = 0.65
    min_gap_over_head: float = 0.05
    min_gap_over_second: float = 0.03


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """What the controller needs to know about one arm.

    The scheduler doesn't produce this directly — the orchestrator
    summarizes ``scheduler.stats`` + ``scheduler.candidates`` into a
    list of these before calling ``consider_promotion``. Keeping the
    controller independent of the OnlineScheduler lets later schedulers
    (learned, cross-session, etc.) feed in the same way.
    """

    candidate_id: str          # identifier the caller uses to address this arm
    version: int               # skill version this candidate represents
    plays: int
    mean_score: float
    notes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvolutionReport:
    decision: EvolutionDecision
    winner_candidate_id: str | None = None
    winner_version: int | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


class EvolutionController:
    """Pure decision engine — no side effects, no I/O, no registry access.

    Instantiate with thresholds; call ``consider_promotion`` with the
    current evaluations and HEAD version. The caller wires the returned
    report into their registry.
    """

    def __init__(self, thresholds: PromotionThresholds | None = None) -> None:
        self._t = thresholds or PromotionThresholds()

    def consider_promotion(
        self,
        evaluations: list[CandidateEvaluation],
        *,
        head_version: int | None,
        head_mean: float | None = None,
    ) -> EvolutionReport:
        """Return a report with either a promotion or NO_CHANGE.

        ``head_version`` is the currently-active skill version. Candidates
        whose ``version == head_version`` are the baseline and cannot
        themselves be "promoted" — they're already HEAD.

        ``head_mean`` is the HEAD's measured mean score in this bench
        (if available). If None, gap-over-head is computed against the
        session's overall mean as a less-strict baseline.
        """
        if not evaluations:
            return EvolutionReport(
                decision=EvolutionDecision.NO_CHANGE,
                reason="no candidates to evaluate",
            )

        # Rank all candidates by mean score descending.
        ranked = sorted(
            evaluations, key=lambda e: e.mean_score, reverse=True,
        )
        best = ranked[0]

        # If HEAD is the best, no promotion.
        if head_version is not None and best.version == head_version:
            return EvolutionReport(
                decision=EvolutionDecision.NO_CHANGE,
                reason=(
                    f"HEAD v{head_version} is already the best arm "
                    f"(mean={best.mean_score:.3f})"
                ),
            )

        # Baseline for gap-over-head.
        if head_mean is not None:
            baseline = head_mean
        else:
            # No measured HEAD — fall back to the session mean across arms.
            total_plays = sum(e.plays for e in evaluations)
            if total_plays == 0:
                baseline = 0.0
            else:
                baseline = sum(
                    e.mean_score * e.plays for e in evaluations
                ) / total_plays

        second = ranked[1] if len(ranked) > 1 else None

        # Gate 1: enough plays.
        if best.plays < self._t.min_plays:
            return EvolutionReport(
                decision=EvolutionDecision.NO_CHANGE,
                reason=(
                    f"best arm has only {best.plays} plays, need "
                    f"≥ {self._t.min_plays}"
                ),
            )

        # Gate 2: absolute quality floor.
        if best.mean_score < self._t.min_mean:
            return EvolutionReport(
                decision=EvolutionDecision.NO_CHANGE,
                reason=(
                    f"best arm mean {best.mean_score:.3f} below floor "
                    f"{self._t.min_mean}"
                ),
            )

        # Gate 3: gap over HEAD baseline.
        gap_head = best.mean_score - baseline
        if gap_head < self._t.min_gap_over_head:
            return EvolutionReport(
                decision=EvolutionDecision.NO_CHANGE,
                reason=(
                    f"best arm mean {best.mean_score:.3f} vs baseline "
                    f"{baseline:.3f} → gap {gap_head:.3f} < "
                    f"threshold {self._t.min_gap_over_head}"
                ),
            )

        # Gate 4: statistical separation from runner-up. Only applies
        # when a second arm exists with any plays.
        if second is not None and second.plays > 0:
            gap_second = best.mean_score - second.mean_score
            if gap_second < self._t.min_gap_over_second:
                return EvolutionReport(
                    decision=EvolutionDecision.NO_CHANGE,
                    reason=(
                        f"best arm {best.candidate_id!r} mean "
                        f"{best.mean_score:.3f} within "
                        f"{self._t.min_gap_over_second} of runner-up "
                        f"{second.candidate_id!r} mean "
                        f"{second.mean_score:.3f} — not enough "
                        f"separation for promotion"
                    ),
                )

        # All gates cleared — propose a promotion.
        evidence = [
            f"candidate={best.candidate_id}",
            f"plays={best.plays}",
            f"mean={best.mean_score:.3f}",
            f"baseline={baseline:.3f}",
            f"gap_over_head={gap_head:.3f}",
        ]
        if second is not None:
            evidence.append(
                f"gap_over_second={best.mean_score - second.mean_score:.3f}"
            )
        return EvolutionReport(
            decision=EvolutionDecision.PROMOTE,
            winner_candidate_id=best.candidate_id,
            winner_version=best.version,
            evidence=tuple(evidence),
            reason=(
                f"arm {best.candidate_id!r} cleared all gates — "
                f"promoting v{best.version}"
            ),
        )
