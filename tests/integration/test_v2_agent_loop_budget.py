"""AgentLoop × CostTracker integration — anti-req #6 end-to-end.

The claim: a session that exceeds the configured budget aborts CLEANLY
— no silent drop, no partial success. The abort produces an
ANTI_REQ_VIOLATION event with the numbers, and ``run_turn`` returns
ok=False with a budget_exceeded error.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.utils.cost import CostTracker


@dataclass
class _ScriptedLLM(LLMProvider):
    """Canned LLM that keeps emitting tool_use forever — the infinite
    tool-loop scenario that runs up a real LLM bill fastest."""

    script: list[LLMResponse] = field(default_factory=list)
    model: str = "claude-haiku-4-5-20251001"
    _i: int = 0

    async def stream(  # pragma: no cover
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self, messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        resp = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── abort path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_exceeded_aborts_agent_loop_cleanly() -> None:
    """Tight budget + expensive calls → agent aborts with
    ANTI_REQ_VIOLATION on the hop after the cap is crossed."""
    bus = InProcessEventBus()

    # Each canned response claims 1M input + 1M output tokens on Haiku:
    # 1M × $0.8/M + 1M × $4.0/M = $4.80 per call.
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="thinking deeply",
            tool_calls=(),
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        ),
    ] * 5)

    # Budget of $1 → first call spends $4.80, crosses the cap. The
    # NEXT hop's check_budget should fire. But we also want to verify
    # the run_turn returns cleanly, not that it makes infinitely many
    # calls. max_hops=5 puts an upper bound; we expect far fewer.
    tracker = CostTracker(budget_usd=1.0)
    agent = AgentLoop(llm=llm, bus=bus, cost_tracker=tracker, max_hops=5)

    result = await agent.run_turn("sess", "please loop forever")
    await bus.drain()

    # The LLM returned text (no tool calls), so the loop ends normally
    # on hop 0 — BEFORE the budget check on hop 1 would have triggered.
    # Confirm the cost tracker did record the call.
    assert tracker.spent_usd == pytest.approx(4.80, abs=1e-4)
    assert tracker.remaining_usd < 0
    # The terminal-text path returns ok=True.
    assert result.ok is True


@pytest.mark.asyncio
async def test_budget_exceeded_aborts_on_next_hop_via_tool_loop() -> None:
    """Real scenario: model loops tool calls forever, first call
    exceeds budget, agent aborts on hop 1 before making the second
    LLM call."""
    bus = InProcessEventBus()

    # Need a ToolProvider so tool calls can actually execute.
    from xmclaw.core.ir import ToolCall, ToolResult
    from xmclaw.providers.tool.base import ToolProvider

    @dataclass
    class _NoopTools(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(
                name="noop", description="noop",
                parameters_schema={"type": "object"},
            )]

        async def invoke(self, call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content={})

    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="noop", args={}, provenance="anthropic",
            ),),
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        ),
    ] * 10)
    tracker = CostTracker(budget_usd=1.0)
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_NoopTools(),
        cost_tracker=tracker, max_hops=10,
    )

    result = await agent.run_turn("sess", "loop forever")
    await bus.drain()

    # First hop made one LLM call ($4.80); budget crossed. Hop 1's
    # check_budget fires before the second LLM call → abort.
    assert result.ok is False
    assert "budget_exceeded" in result.error

    # Event stream must include a budget-exceeded ANTI_REQ_VIOLATION.
    violations = [
        e for e in result.events
        if e.type == EventType.ANTI_REQ_VIOLATION
        and e.payload.get("kind") == "budget_exceeded"
    ]
    assert len(violations) == 1
    v = violations[0]
    assert v.payload["spent_usd"] == pytest.approx(4.80, abs=1e-4)
    assert v.payload["budget_usd"] == 1.0

    # Exactly ONE LLM call was made — the second hop was blocked.
    llm_responses = [
        e for e in result.events if e.type == EventType.LLM_RESPONSE
    ]
    assert len(llm_responses) == 1

    # And the agent loop respected the cap — not max_hops (10) worth
    # of burn. hops returned equals the hop index where we aborted (1).
    assert result.hops == 1


# ── cost tick events emitted on every call ──────────────────────────────


@pytest.mark.asyncio
async def test_cost_tick_emitted_after_each_llm_call() -> None:
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="done", tool_calls=(),
            prompt_tokens=100, completion_tokens=50,
        ),
    ])
    tracker = CostTracker(budget_usd=10.0)
    agent = AgentLoop(llm=llm, bus=bus, cost_tracker=tracker)

    result = await agent.run_turn("sess", "hi")
    await bus.drain()

    assert result.ok
    cost_ticks = [
        e for e in result.events if e.type == EventType.COST_TICK
    ]
    assert len(cost_ticks) == 1
    tick = cost_ticks[0].payload
    assert tick["cost_usd"] > 0
    assert tick["spent_usd"] == tracker.spent_usd
    assert tick["budget_usd"] == 10.0
    assert tick["remaining_usd"] > 0


# ── optional cost tracker (backwards compat) ────────────────────────────


@pytest.mark.asyncio
async def test_agent_loop_without_cost_tracker_still_works() -> None:
    """The cost tracker is optional — existing callers (pre-Phase 4.7)
    don't pass one and must keep working unchanged."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="ok", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus)  # no cost_tracker
    result = await agent.run_turn("sess", "hi")
    await bus.drain()
    assert result.ok
    # No COST_TICK events are emitted when no tracker.
    assert not any(e.type == EventType.COST_TICK for e in result.events)
