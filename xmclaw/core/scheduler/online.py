"""OnlineScheduler — streaming evolution (Phase 1 go/no-go).

Design (V2_DEVELOPMENT.md §3.7):
- ``on_event`` feeds every graded event into a sliding-window buffer
- ``decide_next`` picks between {call_tool, respond, ask_user, delegate,
  retry_optimized} based on event context + current best candidate
- ``promote_candidate`` emits a ``skill_promoted`` event whose payload MUST
  include ``evidence: list[str]`` (anti-req #12)

Phase 1: stub. First working impl lands alongside the demo skill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from xmclaw.core.bus.events import BehavioralEvent

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
    evidence: list[str]


@dataclass
class PromotionResult:
    accepted: bool
    reason: str


class OnlineScheduler:
    async def on_event(self, event: BehavioralEvent) -> None:  # noqa: ARG002
        raise NotImplementedError("Phase 1")

    async def decide_next(self, ctx: DecisionContext) -> Decision:  # noqa: ARG002
        raise NotImplementedError("Phase 1")

    async def promote_candidate(
        self, candidate: Candidate  # noqa: ARG002
    ) -> PromotionResult:
        raise NotImplementedError("Phase 1")
