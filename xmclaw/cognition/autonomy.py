"""AutonomyPolicy — gate-keeper for "should the agent act on its
own initiative?".

Where R3 metacognition produces ``ReformProposal`` envelopes (and
R4 perception produces ``Percept`` envelopes that may suggest
proactive actions), the AutonomyPolicy decides what HAPPENS to those
proposals based on the operator's configured autonomy level.

Three decision tiers (matching the existing ``CognitiveDaemonConfig.
autonomy_level`` int but giving them names):

* **observe** (level < 25): never act, never even surface.
  Proposals get logged for the operator to review later but the user
  never sees a notification.
* **suggest** (25 ≤ level < 75): surface as a "建议" / "Suggestion"
  for the operator to manually approve. The agent NEVER auto-applies.
  This is the safe default for vibe-coding-with-AI workflows where
  trust is being built.
* **execute** (level ≥ 75): auto-apply LOW-risk actions; high-risk
  still requires confirmation. Risk is action-kind-aware:
  - low risk: curriculum_edit, preference_update, memory_consolidate
  - high risk: skill_propose (modifies behavior), task scheduling
    (touches real systems), file writes outside workspace, etc.
  Even at execute tier, high-risk actions go through ``double_confirm``.

The policy also enforces a per-action **rate limiter**: at most N
auto-applies per hour by kind. Prevents a hot-spot in the Reformer
from drowning the user in proposals.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


AutonomyTier = Literal["observe", "suggest", "execute"]
RiskLevel = Literal["low", "medium", "high"]
DecisionVerdict = Literal[
    "drop",                  # never surface
    "surface",               # show as suggestion (don't apply)
    "auto_apply",            # apply without confirmation
    "needs_confirmation",    # apply only after user nods
]


# Default action kinds and their risk levels. Operators can override
# via cfg.cognition.autonomy.risk_overrides.
_DEFAULT_RISK_BY_KIND: dict[str, RiskLevel] = {
    # R3 reform proposals
    "curriculum_edit":    "low",
    "preference_update":  "low",
    "skill_propose":      "high",     # changes behaviour
    # R1 reflection consolidation
    "memory_consolidate": "low",
    "memory_archive":     "medium",
    # R2 task / goal actions
    "goal_add":           "medium",
    "goal_complete":      "low",
    "task_submit":        "medium",
    "task_cancel":        "medium",
    # R4 percept-driven follow-ups
    "send_notification":  "low",
    "open_url":           "medium",
    # File-system / external-system actions
    "file_write":         "high",
    "file_delete":        "high",
    "shell_command":      "high",
    "send_message":       "high",     # email / slack / etc
}


def _tier_from_level(level: int) -> AutonomyTier:
    if level >= 75:
        return "execute"
    if level >= 25:
        return "suggest"
    return "observe"


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    """Decision for one proposed action.

    Fields:
        verdict       — what the policy says happens to this proposal
        risk          — resolved risk level for the action kind
        tier          — the autonomy tier evaluated under
        reason        — short human-readable rationale
        rate_limit_remaining — how many more auto-applies of this kind
                                fit before the rate cap kicks in
                                (informational; -1 = no cap)
    """
    verdict: DecisionVerdict
    risk: RiskLevel
    tier: AutonomyTier
    reason: str
    rate_limit_remaining: int = -1


@dataclass
class AutonomyPolicy:
    """Decides verdict per-action based on autonomy_level + risk +
    rate limits.

    Args:
        autonomy_level: 0..100; mutable so live config reload can
            adjust without rebuilding the policy.
        risk_overrides: dict[kind, RiskLevel] — operator overrides
            on top of the default risk table.
        max_auto_applies_per_hour: per-kind cap on ``auto_apply``
            verdicts. Default 10 (so a runaway Reformer can emit 10
            curriculum_edits in an hour, then everything else queues
            as ``needs_confirmation``).

    Stateful: tracks per-kind apply timestamps in-memory. Process
    restart resets the counter (acceptable: the rate limiter exists
    to dampen feedback loops, not to enforce a global budget).
    """

    autonomy_level: int = 0
    risk_overrides: dict[str, RiskLevel] = field(default_factory=dict)
    max_auto_applies_per_hour: int = 10
    _apply_log: dict[str, list[float]] = field(
        default_factory=dict, init=False, repr=False,
    )

    @property
    def tier(self) -> AutonomyTier:
        return _tier_from_level(int(self.autonomy_level))

    def risk_of(self, kind: str) -> RiskLevel:
        if kind in self.risk_overrides:
            return self.risk_overrides[kind]
        return _DEFAULT_RISK_BY_KIND.get(kind, "high")  # default "high" — fail closed

    def evaluate(
        self,
        *,
        action_kind: str,
        confidence: float = 1.0,
        is_user_present: bool = True,
    ) -> AutonomyDecision:
        """Decide what the policy says about applying ``action_kind``.

        Args:
            action_kind: e.g. ``curriculum_edit`` / ``skill_propose``.
                Looked up in the risk table.
            confidence: caller's confidence in [0, 1]. Below 0.4 the
                verdict is downgraded one tier (drop instead of
                surface; surface instead of auto_apply).
            is_user_present: when False, ``execute`` tier downgrades
                ``auto_apply`` of high-risk actions to ``surface``
                (don't take risky moves while the user is afk).
        """
        risk = self.risk_of(action_kind)
        tier = self.tier

        # observe: drop everything (operator opted into "watch only").
        if tier == "observe":
            return AutonomyDecision(
                verdict="drop", risk=risk, tier=tier,
                reason="autonomy_level<25 (observe-only)",
            )

        # suggest: surface only — never auto-apply.
        if tier == "suggest":
            # Confidence floor: very low confidence proposals at
            # suggest tier aren't even worth showing.
            if confidence < 0.2:
                return AutonomyDecision(
                    verdict="drop", risk=risk, tier=tier,
                    reason=f"suggest tier + confidence={confidence:.2f}<0.2",
                )
            return AutonomyDecision(
                verdict="surface", risk=risk, tier=tier,
                reason="autonomy_level<75 (suggest tier)",
            )

        # execute tier:
        #   * high risk → always needs_confirmation (double-confirm)
        #   * medium risk + low confidence → needs_confirmation
        #   * medium risk + user away → surface (don't act on
        #     borderline things while the user is afk)
        #   * low risk → auto_apply (with rate limit)
        if risk == "high":
            return AutonomyDecision(
                verdict="needs_confirmation", risk=risk, tier=tier,
                reason="execute tier but high-risk action",
            )
        if risk == "medium":
            if confidence < 0.4:
                return AutonomyDecision(
                    verdict="needs_confirmation", risk=risk, tier=tier,
                    reason=f"medium risk + confidence={confidence:.2f}<0.4",
                )
            if not is_user_present:
                return AutonomyDecision(
                    verdict="surface", risk=risk, tier=tier,
                    reason="medium risk + user away → defer",
                )
            return self._auto_or_rate_limit(action_kind, risk, tier)
        # low risk
        if confidence < 0.3:
            return AutonomyDecision(
                verdict="surface", risk=risk, tier=tier,
                reason=f"low risk but confidence={confidence:.2f}<0.3",
            )
        return self._auto_or_rate_limit(action_kind, risk, tier)

    # ── Rate limiter ─────────────────────────────────────────────

    def _auto_or_rate_limit(
        self, kind: str, risk: RiskLevel, tier: AutonomyTier,
    ) -> AutonomyDecision:
        """Returns ``auto_apply`` if the per-kind rate budget allows,
        else ``surface`` with a rate-limit reason."""
        now = time.time()
        bucket = self._apply_log.setdefault(kind, [])
        # Drop entries older than 1 hour.
        cutoff = now - 3600.0
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        remaining = max(0, self.max_auto_applies_per_hour - len(bucket))
        if remaining <= 0:
            return AutonomyDecision(
                verdict="surface", risk=risk, tier=tier,
                reason=(
                    f"rate-limited: {self.max_auto_applies_per_hour} "
                    f"{kind} per hour cap hit"
                ),
                rate_limit_remaining=0,
            )
        return AutonomyDecision(
            verdict="auto_apply", risk=risk, tier=tier,
            reason="execute tier + low/med risk",
            rate_limit_remaining=remaining,
        )

    def record_applied(self, kind: str) -> None:
        """Caller hooks here AFTER a successful auto_apply. Drives
        the rate limiter."""
        bucket = self._apply_log.setdefault(kind, [])
        bucket.append(time.time())


__all__ = [
    "AutonomyDecision",
    "AutonomyPolicy",
    "AutonomyTier",
    "DecisionVerdict",
    "RiskLevel",
]
