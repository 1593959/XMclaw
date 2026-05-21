"""Dataclasses for the Intent Engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UserPattern:
    """A learned behaviour pattern extracted from the event stream.

    Patterns are keyed by a hash of their *antecedent* event sequence
    (what typically happens *before* the intent is expressed).
    """

    pattern_id: str
    # Human-readable label, e.g. "deploy_then_check_logs"
    label: str
    # Ordered list of event types that form the antecedent.
    antecedent: list[str]
    # The predicted intent / action that usually follows.
    predicted_intent: str
    # How many times this pattern has been observed.
    frequency: int = 0
    # [0, 1] — empirical acceptance rate (user acted on the proposal).
    confidence: float = 0.0
    # Epoch seconds.
    last_seen: float = 0.0
    # Optional metadata (time-of-day buckets, weekday, project, etc.).
    context_buckets: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class IntentPrediction:
    """A single scored prediction produced by the IntentEngine."""

    intent_type: str
    confidence: float
    # Human-readable explanation for the operator / UI.
    rationale: str
    # Structured payload for the downstream trigger / orchestrator.
    proposed_action: dict[str, Any] = field(default_factory=dict)
    # Reference to the pattern that produced this prediction.
    pattern_id: str | None = None
    # Layer that produced it: "rule" | "statistical" | "llm"
    source_layer: str = "rule"


@dataclass(slots=True)
class ProactiveProposal:
    """Final shape emitted to the EventBus (mirrors TriggerProposal fields
    but adds intent-engine-specific telemetry).
    """

    message: str
    urgency: str = "normal"  # "low" | "normal" | "high"
    confidence: float = 0.0
    intent_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
