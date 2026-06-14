"""IntentPredictionTrigger — bridges IntentEngine into ProactiveAgent.

Registered in daemon lifespan alongside IdleCheckInTrigger and
SystemHealthTrigger. On each tick it asks the IntentEngine for its
highest-confidence predictions and surfaces the best one as a
TriggerProposal.
"""
from __future__ import annotations

from xmclaw.cognition.intent_engine.engine import IntentEngine
from xmclaw.cognition.intent_engine.models import IntentPrediction
from xmclaw.cognition.proactive_agent import ProactiveContext, ProactiveTrigger, TriggerProposal
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class IntentPredictionTrigger(ProactiveTrigger):
    """Query the IntentEngine each tick and surface the top prediction."""

    # Minimum seconds since last user message before we even consider
    # firing. Without this guard, the trigger fires right after a turn
    # completes — the user sees a proposal immediately after the agent
    # just finished responding, which feels like the agent needlessly
    # butting in ("刚索引完，要不要帮你搜？" spam).
    _MIN_IDLE_SECONDS: float = 120.0

    def __init__(
        self,
        engine: IntentEngine,
        *,
        cooldown_s: float = 1800.0,
        confidence_threshold: float = 0.7,
    ) -> None:
        self.name = "intent_prediction"
        self.cooldown_s = float(cooldown_s)
        self._engine = engine
        self._confidence_threshold = float(confidence_threshold)
        # Track which predictions we have already surfaced this cooldown
        # window so we don't repeat the same intent every tick.
        self._surfaced: dict[str, float] = {}

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        # 2026-05-24 user report: intent_prediction kept butting in
        # mid-turn ("我注意到你的计划连续失败了好几次..."), interrupting
        # the user/agent flow. Proactive triggers are for IDLE moments,
        # not for narrating over an in-flight turn. Guard against any
        # currently-running turn before considering predictions.
        agent_loop = getattr(ctx, "agent_loop", None)
        if agent_loop is not None:
            active = getattr(agent_loop, "_cancel_events", None)
            if active:
                return False
        # 2026-06-11: require minimum idle time since last user message.
        # Without this, the trigger fires immediately after a turn
        # completes — the user sees "刚索引完，要不要我帮你搜？" right
        # after the agent finished a task, which feels like needless
        # butting-in. Proposals should only fire when the user has been
        # genuinely inactive for a while.
        last_user_ts = getattr(ctx, "last_user_message_ts", None)
        if last_user_ts is not None and (ctx.now - last_user_ts) < self._MIN_IDLE_SECONDS:
            return False
        predictions = self._engine.top_predictions(
            k=1, min_confidence=self._confidence_threshold,
        )
        if not predictions:
            return False
        top = predictions[0]
        # Deduplicate: same intent type not re-surfaced within cooldown.
        last_surfaced = self._surfaced.get(top.intent_type, 0.0)
        if ctx.now - last_surfaced < self.cooldown_s:
            return False
        return True

    async def propose(self, ctx: ProactiveContext) -> TriggerProposal | None:
        predictions = self._engine.top_predictions(
            k=1, min_confidence=self._confidence_threshold,
        )
        if not predictions:
            return None
        top = predictions[0]
        self._surfaced[top.intent_type] = ctx.now
        proposal = self._engine.to_proposal(top)
        return TriggerProposal(
            trigger_name=self.name,
            message=proposal.message,
            urgency=proposal.urgency,
            payload={
                "confidence": proposal.confidence,
                "intent_type": proposal.intent_type,
                **proposal.payload,
            },
        )

    def record_reaction(self, prediction: IntentPrediction, reaction: str) -> None:
        """Called by the UI / orchestrator when the user responds to a
        proposal surfaced by this trigger. Closes the learning loop."""
        self._engine.record_user_reaction(prediction.pattern_id, reaction)
