"""Chaos tests for the AgentLoop hop loop.

Inject faults at LLM and ToolProvider boundaries to verify that
B-351 (defensive invoke), B-17 (transient retry), and the parallel
tool-execution path remain resilient under stress.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider


# ── chaotic mock LLM ────────────────────────────────────────────────────


@dataclass
class _ChaoticLLM(LLMProvider):
    """LLM that randomly fails or returns malformed data.

    *fail_rate* — probability (0-1) that ``complete()`` raises.
    *bad_tool_rate* — probability that a tool call carries garbage args.
    """

    script: list[LLMResponse] = field(default_factory=list)
    fail_rate: float = 0.0
    bad_tool_rate: float = 0.0
    model: str = "chaotic"
    _i: int = 0
    _rng: random.Random = field(default_factory=lambda: random.Random(42))

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        if self._rng.random() < self.fail_rate:
            raise RuntimeError("simulated LLM failure")
        if self._i >= len(self.script):
            raise RuntimeError("chaotic LLM exhausted")
        resp = self.script[self._i]
        self._i += 1

        # Occasionally corrupt tool-call args
        if resp.tool_calls and self._rng.random() < self.bad_tool_rate:
            corrupted = []
            for tc in resp.tool_calls:
                if self._rng.random() < 0.5:
                    corrupted.append(
                        ToolCall(
                            name=tc.name,
                            args={"__corrupt": True},
                            provenance=tc.provenance,
                            id=tc.id,
                        )
                    )
                else:
                    corrupted.append(tc)
            resp = LLMResponse(content=resp.content, tool_calls=tuple(corrupted))
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── chaotic tool provider ───────────────────────────────────────────────


@dataclass
class _ChaoticToolProvider(ToolProvider):
    """Tool provider that randomly raises, times out, or returns garbage.

    *raise_rate* — probability that ``invoke()`` raises uncaught.
    *timeout_rate* — probability that ``invoke()`` hangs until cancelled.
    *garbage_rate* — probability that the result is nonsensical.
    """

    specs: list[ToolSpec] = field(default_factory=list)
    raise_rate: float = 0.0
    timeout_rate: float = 0.0
    garbage_rate: float = 0.0
    _rng: random.Random = field(default_factory=lambda: random.Random(99))

    def list_tools(self) -> list[ToolSpec]:
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        if self._rng.random() < self.timeout_rate:
            await asyncio.sleep(10_000)
        if self._rng.random() < self.raise_rate:
            raise RuntimeError(f"chaotic tool {call.name} exploded")
        if self._rng.random() < self.garbage_rate:
            return ToolResult(
                call_id=call.id,
                ok=True,
                content="\x00\x01\x02" * 1000,
                error=None,
                side_effects=(),
            )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=f"ok from {call.name}",
            error=None,
            side_effects=(),
        )


# ── helpers ─────────────────────────────────────────────────────────────


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"tool {name}",
                    parameters_schema={"type": "object", "properties": {}})


# ── test cases ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_random_failure_does_not_crash_turn() -> None:
    """B-351 adjacent: even when the LLM throws mid-turn, the loop
    must return a failed AgentTurnResult instead of propagating."""
    bus = InProcessEventBus()
    llm = _ChaoticLLM(
        script=[LLMResponse(content="first", tool_calls=())],
        fail_rate=1.0,
    )
    agent = AgentLoop(llm=llm, bus=bus)
    result = await agent.run_turn("s1", "hello")
    await bus.drain()

    assert result.ok is False
    assert "upstream" in result.error.lower() or "failure" in result.error.lower()


@pytest.mark.asyncio
async def test_tool_random_raise_is_caught() -> None:
    """B-351: ToolProvider that raises uncaught must not abort the turn.
    The defensive wrapper catches it and returns a failed ToolResult."""
    bus = InProcessEventBus()
    llm = _ChaoticLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(name="boom", args={}, provenance="test", id="tc-1"),
            ),
        ),
        LLMResponse(content="Recovered.", tool_calls=()),
    ])
    tools = _ChaoticToolProvider(
        specs=[_spec("boom")],
        raise_rate=1.0,
    )
    agent = AgentLoop(llm=llm, tools=tools, bus=bus)
    agent._mode_instant_enabled = False  # force normal mode so tools are offered
    result = await agent.run_turn("s1", "hello")
    await bus.drain()

    assert result.ok is True
    assert "Recovered." in result.text
    # The failed tool result should be in the conversation history.
    assert "uncaught" in result.text.lower() or result.tool_calls


@pytest.mark.asyncio
async def test_parallel_tool_calls_with_partial_failures() -> None:
    """Parallel execution: when 3 tools are called and 1 fails,
    the other 2 must still complete and all 3 results land in
    messages in the correct order."""
    bus = InProcessEventBus()
    llm = _ChaoticLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(name="a", args={}, provenance="test", id="tc-a"),
                ToolCall(name="b", args={}, provenance="test", id="tc-b"),
                ToolCall(name="c", args={}, provenance="test", id="tc-c"),
            ),
        ),
        LLMResponse(content="Done.", tool_calls=()),
    ])

    results_map = {
        "a": ToolResult(call_id="tc-a", ok=True, content="A-ok", error=None, side_effects=()),
        "b": ToolResult(call_id="tc-b", ok=False, content=None, error="B-failed", side_effects=()),
        "c": ToolResult(call_id="tc-c", ok=True, content="C-ok", error=None, side_effects=()),
    }

    class _PartialProvider(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [_spec("a"), _spec("b"), _spec("c")]

        async def invoke(self, call: ToolCall) -> ToolResult:
            r = results_map[call.name]
            return ToolResult(
                call_id=call.id, ok=r.ok, content=r.content,
                error=r.error, side_effects=r.side_effects,
            )

    tools = _PartialProvider()
    agent = AgentLoop(llm=llm, tools=tools, bus=bus)
    agent._mode_instant_enabled = False  # force normal mode so tools are offered
    result = await agent.run_turn("s1", "hello")
    await bus.drain()

    assert result.ok is True
    assert result.hops == 2
    assert "Done." in result.text
    # All three tool calls should be recorded.
    names = {tc["name"] for tc in result.tool_calls}
    assert names == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_high_fanout_tools_do_not_deadlock() -> None:
    """Stress: 20 parallel tool calls must complete without deadlock
    or event-loop starvation."""
    bus = InProcessEventBus()
    calls = tuple(
        ToolCall(name=f"t{i}", args={"i": i}, provenance="test", id=f"tc-{i}")
        for i in range(20)
    )
    llm = _ChaoticLLM(script=[
        LLMResponse(content="", tool_calls=calls),
        LLMResponse(content="All done.", tool_calls=()),
    ])

    class _SlowProvider(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [_spec(f"t{i}") for i in range(20)]

        async def invoke(self, call: ToolCall) -> ToolResult:
            # Tiny sleep to force interleaving.
            await asyncio.sleep(0.001)
            return ToolResult(
                call_id=call.id, ok=True,
                content=f"result-{call.name}",
                error=None, side_effects=(),
            )

    tools = _SlowProvider()
    agent = AgentLoop(llm=llm, tools=tools, bus=bus)
    agent._mode_instant_enabled = False  # force normal mode so tools are offered
    result = await agent.run_turn("s1", "hello")
    await bus.drain()

    assert result.ok is True
    assert result.hops == 2
    assert len(result.tool_calls) == 20


@pytest.mark.asyncio
async def test_mixed_chaos_survives() -> None:
    """Combined stress: LLM occasionally fails, tools occasionally raise
    or return garbage, yet the agent must never crash and must eventually
    return a result (success or failure)."""
    bus = InProcessEventBus()
    script = [
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(name="x", args={}, provenance="test", id="tc-x"),
                ToolCall(name="y", args={}, provenance="test", id="tc-y"),
            ),
        ),
        LLMResponse(content=" survived.", tool_calls=()),
    ]
    llm = _ChaoticLLM(script=script, fail_rate=0.1, bad_tool_rate=0.1)
    tools = _ChaoticToolProvider(
        specs=[_spec("x"), _spec("y")],
        raise_rate=0.1,
        garbage_rate=0.1,
    )
    agent = AgentLoop(llm=llm, tools=tools, bus=bus)
    agent._mode_instant_enabled = False  # force normal mode so tools are offered

    # Run several turns — probability says at least one will hit a fault.
    for _ in range(10):
        result = await agent.run_turn("s1", "hello")
        await bus.drain()
        # Must NEVER propagate an exception.
        assert result is not None
        # Must always have a text or error field.
        assert result.text is not None or result.error is not None
