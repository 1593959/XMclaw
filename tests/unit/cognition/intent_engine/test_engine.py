"""Tests for IntentEngine."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.cognition.intent_engine.engine import IntentEngine
from xmclaw.cognition.intent_engine.store import IntentStore
from xmclaw.core.bus.events import BehavioralEvent, EventType


@pytest.fixture
def engine() -> IntentEngine:
    with tempfile.TemporaryDirectory() as td:
        store = IntentStore(Path(td) / "intent.db")
        eng = IntentEngine(store=store)
        try:
            yield eng
        finally:
            store.close()


class TestIntentEngine:
    async def test_on_event_populates_window(self, engine: IntentEngine) -> None:
        ev = BehavioralEvent(
            id="e1", ts=1.0, session_id="s1", agent_id="a1",
            type=EventType.USER_MESSAGE, payload={"text": "hello"},
        )
        await engine.on_event(ev)
        assert len(engine._context_window) == 1

    async def test_rule_layer_deploy_keyword(self, engine: IntentEngine) -> None:
        ev = BehavioralEvent(
            id="e1", ts=1.0, session_id="s1", agent_id="a1",
            type=EventType.USER_MESSAGE, payload={"text": "deploy to production"},
        )
        await engine.on_event(ev)
        preds = engine.top_predictions(k=1, min_confidence=0.5)
        assert len(preds) == 1
        assert preds[0].intent_type == "post_deploy_check"

    async def test_rule_layer_grader_fail(self, engine: IntentEngine) -> None:
        ev = BehavioralEvent(
            id="e1", ts=1.0, session_id="s1", agent_id="a1",
            type=EventType.GRADER_VERDICT,
            payload={"verdict": "fail", "deterministic_score": 0.1},
        )
        await engine.on_event(ev)
        preds = engine.top_predictions(k=1, min_confidence=0.5)
        assert len(preds) == 1
        assert preds[0].intent_type == "review_recent_failures"

    async def test_learn_creates_pattern(self, engine: IntentEngine) -> None:
        ev1 = BehavioralEvent(
            id="e1", ts=1.0, session_id="s1", agent_id="a1",
            type=EventType.USER_MESSAGE, payload={},
        )
        ev2 = BehavioralEvent(
            id="e2", ts=2.0, session_id="s1", agent_id="a1",
            type=EventType.TOOL_INVOCATION_FINISHED, payload={},
        )
        await engine.on_event(ev1)
        await engine.on_event(ev2)
        # A pattern for [user_message, tool_invocation_finished] should exist.
        patterns = engine._store.list_patterns(limit=10)
        assert any("user_message" in p.antecedent for p in patterns)

    def test_to_proposal(self, engine: IntentEngine) -> None:
        from xmclaw.cognition.intent_engine.models import IntentPrediction
        pred = IntentPrediction(
            intent_type="test",
            confidence=0.9,
            rationale="test rationale",
            proposed_action={"message": "hello", "urgency": "high"},
        )
        proposal = engine.to_proposal(pred)
        assert proposal.message == "hello"
        assert proposal.urgency == "high"
        assert proposal.confidence == 0.9
