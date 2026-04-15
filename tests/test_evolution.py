import asyncio
import pytest
from xmclaw.evolution.engine import EvolutionEngine
from xmclaw.memory.manager import MemoryManager


@pytest.mark.asyncio
async def test_evolution_extract_insights():
    engine = EvolutionEngine("default")
    sessions = [
        {"user": "read file", "assistant": "ok", "tool_calls": [{"name": "file_read"}]},
        {"user": "read file", "assistant": "ok", "tool_calls": [{"name": "file_read"}]},
        {"user": "read file", "assistant": "ok", "tool_calls": [{"name": "file_read"}]},
    ]
    insights = engine._extract_insights(sessions)
    assert len(insights) >= 1
    assert insights[0]["type"] == "pattern"


@pytest.mark.asyncio
async def test_evolution_detects_negative_feedback():
    engine = EvolutionEngine("default")
    sessions = [
        {"user": "This is wrong", "assistant": "sorry", "tool_calls": []},
    ]
    insights = engine._extract_insights(sessions)
    assert len(insights) >= 1
    assert insights[0]["type"] == "problem"
