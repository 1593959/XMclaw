"""OnlineScheduler — streaming evolution (Phase 1 go/no-go).

The scheduler owns a set of ``candidates`` (e.g. prompt variants, skill
versions, tool-choice policies). On every grader verdict it updates the
candidate's reward stats. ``decide_next`` picks the next candidate via
UCB1 — an unplayed candidate beats any played one (exploration priority),
then UCB1 trades off exploit (best mean) vs explore (few plays).

``promote_candidate`` emits a ``skill_promoted`` event whose payload MUST
carry ``evidence: list[str]`` (anti-req #12 — no evidence, no promotion).

This is intentionally a bandit, not a full RL policy. Phase 4 upgrades to
cross-session signals and learned arm embeddings (V2_DEVELOPMENT.md §3.7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.scheduler.policy import best_of_n, ucb1

Decision = Literal["call_tool", "respond", "ask_user", "delegate", "retry_optimized"]


@dataclass
class DecisionContext:
    session_id: str
    recent_events: list[BehavioralEvent]
    user_request: str


@dataclass
class Candidate:
    skill_id: str
    version: int
    prompt_delta: dict[str, Any]
    evidence: list[str] = field(default_factory=list)


@dataclass
class PromotionResult:
    accepted: bool
    reason: str


@dataclass
class ArmStats:
    plays: int = 0
    total_reward: float = 0.0

    @property
    def mean(self) -> float:
        return self.total_reward / self.plays if self.plays else 0.0


class OnlineScheduler:
    """Phase 1 implementation: UCB1 bandit over candidates.

    Parameters
    ----------
    candidates : list[Candidate]
        Initial candidate pool. New candidates can be added later via
        ``add_candidate``.
    exploration_c : float, default 2.0
        UCB1 exploration constant. Higher → more exploration.
    promotion_evidence_floor : int, default 1
        Minimum number of ``evidence`` entries a ``Candidate`` must carry
        before ``promote_candidate`` will accept it (anti-req #12).
    """

    def __init__(
        self,
        candidates: list[Candidate] | None = None,
        *,
        exploration_c: float = 2.0,
        promotion_evidence_floor: int = 1,
    ) -> None:
        self._candidates: list[Candidate] = list(candidates or [])
        self._stats: list[ArmStats] = [ArmStats() for _ in self._candidates]
        self._last_chosen_idx: int | None = None
        self._exploration_c = exploration_c
        self._promotion_evidence_floor = promotion_evidence_floor

    # ── public API ──

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return tuple(self._candidates)

    @property
    def stats(self) -> tuple[ArmStats, ...]:
        return tuple(self._stats)

    def add_candidate(self, c: Candidate) -> int:
        """Add a candidate and return its index."""
        self._candidates.append(c)
        self._stats.append(ArmStats())
        return len(self._candidates) - 1

    def pick(self) -> int:
        """Pick the next candidate index via UCB1. Record as last chosen."""
        if not self._candidates:
            raise RuntimeError("no candidates to pick from")
        means = [s.mean for s in self._stats]
        plays = [s.plays for s in self._stats]
        idx = ucb1(means, plays, c=self._exploration_c)
        self._last_chosen_idx = idx
        return idx

    def best_known(self) -> int:
        """Return the greedy best candidate index (exploit-only)."""
        return best_of_n([s.mean for s in self._stats])

    # ── event-driven hooks (wired to the bus) ──

    async def on_event(self, event: BehavioralEvent) -> None:
        """Consume events. Currently only ``grader_verdict`` updates stats.

        The grader_verdict payload must carry a ``candidate_idx: int`` field
        so the scheduler knows which arm the score belongs to. If the
        publisher omits it, we use ``self._last_chosen_idx`` as a fallback.
        """
        if event.type != EventType.GRADER_VERDICT:
            return

        idx = event.payload.get("candidate_idx", self._last_chosen_idx)
        score = event.payload.get("score")
        if idx is None or score is None:
            return
        if idx < 0 or idx >= len(self._stats):
            return
        self._stats[idx].plays += 1
        self._stats[idx].total_reward += float(score)

    async def decide_next(self, ctx: DecisionContext) -> Decision:  # noqa: ARG002
        """Phase 1 stub: always returns ``call_tool``.

        The full state machine lands with Phase 2 when real agent-loop
        messages arrive. For Phase 1's bench, the loop is externally driven
        and only ``pick`` + ``on_event`` are exercised.
        """
        return "call_tool"

    async def promote_candidate(self, candidate: Candidate) -> PromotionResult:
        """Accept or reject a candidate for promotion.

        Anti-requirement #12: promotion requires non-empty ``evidence`` on
        the ``Candidate``. The emitted ``skill_promoted`` event (published
        by the bus subscriber after a successful promotion) carries this
        evidence verbatim.
        """
        if len(candidate.evidence) < self._promotion_evidence_floor:
            return PromotionResult(
                accepted=False,
                reason=(
                    f"refused: evidence={len(candidate.evidence)} below floor "
                    f"{self._promotion_evidence_floor} (anti-req #12)"
                ),
            )
        return PromotionResult(accepted=True, reason="ok")
