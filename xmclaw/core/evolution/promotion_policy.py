"""Sprint 3 Iron Rule #2 — promotion policy: bundle → decision.

This module turns a :class:`xmclaw.core.evolution.staging.GateBundle`
into a tri-state :class:`Decision` (promote / hold_for_human /
reject) under a configurable :class:`PromotionPolicy`.

It deliberately knows nothing about the registry, the bus, or the
controller — that wiring is the controller-integration follow-up.
What lives here is the **decision logic**, isolated so it is unit-
testable in microseconds and so the controller and the future
`xmclaw evolve review` UI both consume the same policy module.

Iron Rule #2 (`docs/EVOLUTION_HONEST_STATE.md`):

    "Staging → gate → explicit promote. The orchestrator never
    mutates `SkillRegistry` HEAD inline. Always: candidate dir →
    4 gates → explicit `promote()` call (auto-policy or human)."

Three policies are exposed:

* :attr:`PromotionPolicy.AUTO_ON_PASS_ALL` — if every gate passed
  (no failures, no skips), promote automatically. Otherwise hold or
  reject. Skips force human review under this policy.
* :attr:`PromotionPolicy.HUMAN_REQUIRED_ALWAYS` — never auto-promote.
  Either hold (gates passed/skipped) or reject (gates failed).
* :attr:`PromotionPolicy.HUMAN_REQUIRED_FOR_HIGH_RISK` — promote
  automatically only when the bundle is *not* high-risk; otherwise
  hold. Failures still reject.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from xmclaw.core.evolution.staging import GateBundle

DecisionAction = Literal["promote", "hold_for_human", "reject"]


class PromotionPolicy(Enum):
    """Auto / hold / human-only policies for staged promotion.

    The default in the controller-integration follow-up will be
    :attr:`HUMAN_REQUIRED_ALWAYS` — matching the existing
    ``evolution.auto_apply=false`` ship-default and Iron Rule #2's
    "explicit promote" stance.
    """

    AUTO_ON_PASS_ALL = "auto_on_pass_all"
    HUMAN_REQUIRED_ALWAYS = "human_required_always"
    HUMAN_REQUIRED_FOR_HIGH_RISK = "human_required_for_high_risk"


@dataclass(frozen=True, slots=True)
class Decision:
    """Tri-state promotion outcome with a human-readable reason.

    The controller passes the ``reason`` to the bus event payload so
    `xmclaw evolve review` can group decisions by stable strings.
    """

    action: DecisionAction
    reason: str


def is_high_risk(bundle: GateBundle) -> bool:
    """Heuristic — should this bundle be flagged for human review?

    Two triggers (matching Iron Rule #2's "two independent signals
    minimum" stance from Iron Rule #1):

    1. Holdout test was skipped — we have no second signal to back
       up the legacy plays / mean / gap heuristics.
    2. Structure gate surfaced findings — even on pass, a non-empty
       findings list (e.g. "python_no_top_level_def_or_import") is
       a yellow flag worth human eyes.

    A failed gate is *not* "high risk" — it's outright reject under
    every policy, handled by `decide()` directly.
    """
    holdout = bundle.by_name("holdout_test")
    if holdout is not None and holdout.status == "skipped":
        return True

    structure = bundle.by_name("structure_validation")
    if structure is not None and structure.status == "passed":
        findings = structure.evidence.get("findings", [])
        if isinstance(findings, list) and findings:
            return True

    return False


def decide(bundle: GateBundle, policy: PromotionPolicy) -> Decision:
    """Map a (bundle, policy) pair to a single :class:`Decision`.

    The decision tree (in order):

    1. Any gate failed → ``reject`` (always; no policy lets you
       promote a failing candidate).
    2. ``HUMAN_REQUIRED_ALWAYS`` → ``hold_for_human``.
    3. ``HUMAN_REQUIRED_FOR_HIGH_RISK`` →
       * ``hold_for_human`` if `is_high_risk(bundle)` else
       * ``promote``.
    4. ``AUTO_ON_PASS_ALL`` →
       * ``promote`` if no skips and no failures, else
       * ``hold_for_human``.
    """
    if bundle.failed_any:
        failed = [r.name for r in bundle.results if r.status == "failed"]
        return Decision(
            action="reject",
            reason=f"gates failed: {', '.join(failed)}",
        )

    if policy is PromotionPolicy.HUMAN_REQUIRED_ALWAYS:
        return Decision(
            action="hold_for_human",
            reason="policy=HUMAN_REQUIRED_ALWAYS",
        )

    if policy is PromotionPolicy.HUMAN_REQUIRED_FOR_HIGH_RISK:
        if is_high_risk(bundle):
            return Decision(
                action="hold_for_human",
                reason="bundle flagged high-risk (holdout skipped or structure findings)",
            )
        return Decision(
            action="promote",
            reason="policy=HUMAN_REQUIRED_FOR_HIGH_RISK; bundle low-risk",
        )

    # AUTO_ON_PASS_ALL.
    if bundle.skipped_any:
        skipped = [r.name for r in bundle.results if r.status == "skipped"]
        return Decision(
            action="hold_for_human",
            reason=f"policy=AUTO_ON_PASS_ALL but gates skipped: {', '.join(skipped)}",
        )
    return Decision(
        action="promote",
        reason="policy=AUTO_ON_PASS_ALL; all gates passed",
    )


__all__ = [
    "Decision",
    "DecisionAction",
    "PromotionPolicy",
    "decide",
    "is_high_risk",
]
