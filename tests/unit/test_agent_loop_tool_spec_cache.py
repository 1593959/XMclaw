"""Test A: list_tools is cached once per turn in AgentLoop._run_turn_inner.

Verifies that the curriculum-hint check and the main tool-assembly both
reuse the same ``_cached_tool_specs`` list, so ``effective_tools.list_tools()``
is called at most once per turn.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.core.ir import ToolCallShape


# ── helpers ──────────────────────────────────────────────────────────────


@dataclass
class _CountingToolProvider(ToolProvider):
    """Returns fixed specs but counts ``list_tools()`` calls."""

    specs: list[ToolSpec] = field(default_factory=list)
    list_tools_calls: int = 0

    def list_tools(self) -> list[ToolSpec]:
        self.list_tools_calls += 1
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, ok=True, content="done")


@dataclass
class _DummyLLM(LLMProvider):
    """Always returns a plain-text response (no tools)."""

    model: str = "dummy"

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(delta="hello", seq=0)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content="hello",
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
    return AgentLoop(
        llm=_DummyLLM(),
        bus=bus,
        tools=None,
        max_hops=1,
    )


# ── tests ─────────────────────────────────────────────────────────────────


def test_no_tools_list_tools_never_called(bus: InProcessEventBus) -> None:
    """When tools=None, list_tools should not be called at all."""
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=None, max_hops=1)
    result = asyncio.run(agent.run_turn("session-1", "hi"))
    assert result.ok is True
    assert result.text == "hello"


def test_tools_present_list_tools_called_once(bus: InProcessEventBus) -> None:
    """With a provider wired, the turn should call ``list_tools()`` exactly once."""
    spec = ToolSpec(name="bash", description="run shell", parameters_schema={})
    provider = _CountingToolProvider(specs=[spec])
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=provider, max_hops=1)

    result = asyncio.run(agent.run_turn("session-2", "hi"))
    assert result.ok is True
    assert provider.list_tools_calls == 1


def test_tools_with_allowlist_list_tools_called_once(bus: InProcessEventBus) -> None:
    """FilteredToolProvider path must still call the underlying list_tools only once."""
    spec_a = ToolSpec(name="bash", description="run shell", parameters_schema={})
    spec_b = ToolSpec(
        name="propose_curriculum_edit",
        description="edit curriculum",
        parameters_schema={},
    )
    provider = _CountingToolProvider(specs=[spec_a, spec_b])
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=provider, max_hops=1)

    result = asyncio.run(
        agent.run_turn(
            "session-3",
            "this is frustrating",
            tools_allowlist={"bash", "propose_curriculum_edit"},
        )
    )
    assert result.ok is True
    # The underlying provider sees exactly one call because the turn caches
    # the result early and both the curriculum hint check + main assembly reuse it.
    assert provider.list_tools_calls == 1


def test_multiple_turns_reset_counter(bus: InProcessEventBus) -> None:
    """Each turn gets its own fresh call; the counter grows linearly."""
    spec = ToolSpec(name="bash", description="run shell", parameters_schema={})
    provider = _CountingToolProvider(specs=[spec])
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=provider, max_hops=1)

    for i in range(3):
        result = asyncio.run(agent.run_turn(f"session-{i}", f"turn {i}"))
        assert result.ok is True

    assert provider.list_tools_calls == 3


def test_turn_cache_does_not_mutate_provider_list(bus: InProcessEventBus) -> None:
    """The cached list must be a copy so downstream prefilter can't mutate the provider."""
    spec = ToolSpec(name="bash", description="run shell", parameters_schema={})
    provider = _CountingToolProvider(specs=[spec])
    agent = AgentLoop(llm=_DummyLLM(), bus=bus, tools=provider, max_hops=1)

    asyncio.run(agent.run_turn("session-4", "hi"))
    # After the turn, the provider's internal list should still be intact.
    assert len(provider.specs) == 1
    assert provider.specs[0].name == "bash"
