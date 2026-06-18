"""Test B: output_schema string is cached inside AgentLoop.

Verifies that the same schema dict produces a cache hit on the second call
and only pays the ``json.dumps`` cost once.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.core.ir import ToolCallShape


# ── helpers ──────────────────────────────────────────────────────────────


@dataclass
class _DummyLLM(LLMProvider):
    model: str = "dummy"

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(delta="{}", seq=0)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content="{}",
            tool_calls=(),
            prompt_tokens=1,
            completion_tokens=1,
            stop_reason="end_turn",
        )

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def agent(bus: InProcessEventBus) -> AgentLoop:
    return AgentLoop(llm=_DummyLLM(), bus=bus, tools=None, max_hops=1)


# ── tests ─────────────────────────────────────────────────────────────────


def test_schema_block_cache_hit_on_second_call(agent: AgentLoop) -> None:
    """Calling ``_get_schema_block`` twice with the same dict returns the same string."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    block1 = agent._get_schema_block(schema)
    block2 = agent._get_schema_block(schema)
    assert block1 is block2  # exact identity because of cache
    assert len(agent._schema_block_cache) == 1


def test_schema_block_cache_miss_different_schemas(agent: AgentLoop) -> None:
    """Two different schemas populate two distinct cache entries."""
    schema_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    block_a = agent._get_schema_block(schema_a)
    block_b = agent._get_schema_block(schema_b)
    assert block_a != block_b
    assert len(agent._schema_block_cache) == 2


def test_schema_block_cache_ignores_key_order(agent: AgentLoop) -> None:
    """Dicts with the same keys/values but different insertion order share a cache slot."""
    schema1 = {"type": "object", "properties": {"x": {"type": "string"}}}
    schema2 = {"properties": {"x": {"type": "string"}}, "type": "object"}
    block1 = agent._get_schema_block(schema1)
    block2 = agent._get_schema_block(schema2)
    assert block1 is block2
    assert len(agent._schema_block_cache) == 1


def test_schema_block_cache_used_in_run_turn(bus: InProcessEventBus) -> None:
    """A full turn with output_schema exercises the cache path."""
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=None, max_hops=1)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    result = asyncio.run(agent.run_turn("session-1", "hi", output_schema=schema))
    assert result.ok is True
    assert len(agent._schema_block_cache) == 1

    # Second turn with the SAME schema must hit the cache.
    result2 = asyncio.run(agent.run_turn("session-2", "hi", output_schema=schema))
    assert result2.ok is True
    assert len(agent._schema_block_cache) == 1


def test_schema_block_cache_includes_instruction_wrapper(agent: AgentLoop) -> None:
    """The cached block must contain the full <output_schema> wrapper, not just JSON."""
    schema = {"type": "object"}
    block = agent._get_schema_block(schema)
    assert "<output_schema>" in block
    assert "</output_schema>" in block
    assert "You MUST respond with a single JSON object" in block
    assert '"type": "object"' in block


def test_schema_block_cache_stable_key_for_nested_dicts(agent: AgentLoop) -> None:
    """Nested schemas with identical content produce identical cache keys."""
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
    }
    block1 = agent._get_schema_block(schema)
    block2 = agent._get_schema_block(schema)
    assert block1 is block2


def test_schema_block_cache_none_schema_safe(agent: AgentLoop) -> None:
    """When output_schema is None, the turn should not touch the cache at all."""
    result = asyncio.run(agent.run_turn("session-3", "hi"))
    assert result.ok is True
    assert len(agent._schema_block_cache) == 0
