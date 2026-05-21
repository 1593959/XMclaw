"""Intent Engine — Jarvis Phase J2: predictive proactive assistance.

Learns user behaviour patterns from the event stream and generates
proactive proposals with confidence scores, replacing fixed-rule
triggers with data-driven intent prediction.
"""
from __future__ import annotations

from xmclaw.cognition.intent_engine.engine import IntentEngine
from xmclaw.cognition.intent_engine.models import IntentPrediction, UserPattern
from xmclaw.cognition.intent_engine.store import IntentStore
from xmclaw.cognition.intent_engine.trigger import IntentPredictionTrigger

__all__ = [
    "IntentEngine",
    "IntentPrediction",
    "IntentPredictionTrigger",
    "IntentStore",
    "UserPattern",
]
