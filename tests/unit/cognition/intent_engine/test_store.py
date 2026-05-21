"""Tests for IntentStore."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.cognition.intent_engine.models import UserPattern
from xmclaw.cognition.intent_engine.store import IntentStore


@pytest.fixture
def store() -> IntentStore:
    with tempfile.TemporaryDirectory() as td:
        s = IntentStore(Path(td) / "intent.db")
        try:
            yield s
        finally:
            s.close()


class TestIntentStore:
    def test_upsert_and_get(self, store: IntentStore) -> None:
        pat = UserPattern(
            pattern_id="p1",
            label="test",
            antecedent=["user_message", "tool_invocation_finished"],
            predicted_intent="check_logs",
            frequency=3,
            confidence=0.8,
            last_seen=1234567890.0,
        )
        store.upsert_pattern(pat)
        got = store.get_pattern("p1")
        assert got is not None
        assert got.pattern_id == "p1"
        assert got.confidence == 0.8

    def test_list_patterns_min_confidence(self, store: IntentStore) -> None:
        store.upsert_pattern(UserPattern(
            pattern_id="low", label="low", antecedent=["a"],
            predicted_intent="x", confidence=0.3,
        ))
        store.upsert_pattern(UserPattern(
            pattern_id="high", label="high", antecedent=["b"],
            predicted_intent="y", confidence=0.9,
        ))
        results = store.list_patterns(min_confidence=0.5)
        assert len(results) == 1
        assert results[0].pattern_id == "high"

    def test_bump_frequency(self, store: IntentStore) -> None:
        store.upsert_pattern(UserPattern(
            pattern_id="p1", label="l", antecedent=["a"],
            predicted_intent="x", frequency=1,
        ))
        store.bump_frequency("p1")
        got = store.get_pattern("p1")
        assert got is not None
        assert got.frequency == 2

    def test_feedback_stats(self, store: IntentStore) -> None:
        store.record_feedback("p1", "accepted")
        store.record_feedback("p1", "accepted")
        store.record_feedback("p1", "ignored")
        stats = store.feedback_stats("p1")
        assert stats["accepted"] == 2
        assert stats["ignored"] == 1

    def test_update_confidence_clamps(self, store: IntentStore) -> None:
        store.upsert_pattern(UserPattern(
            pattern_id="p1", label="l", antecedent=["a"],
            predicted_intent="x", confidence=0.5,
        ))
        store.update_confidence("p1", 1.5)
        got = store.get_pattern("p1")
        assert got is not None
        assert got.confidence == 1.0
        store.update_confidence("p1", -0.5)
        got = store.get_pattern("p1")
        assert got is not None
        assert got.confidence == 0.0
