"""AgentLoop — unit tests.

Covers the five paths:
  1. Plain text response (no tools) → terminal ok=True on first hop
  2. Tool call → invocation → feed result back → terminal text on next hop
  3. LLM raises → ok=False with error envelope, never propagates
  4. Model emits tool_calls without a ToolProvider → ANTI_REQ_VIOLATION
  5. Loop exceeds max_hops → ANTI_REQ_VIOLATION + ok=False

Also verifies the BehavioralEvent stream: each hop emits LLM_REQUEST +
LLM_RESPONSE; tool turns emit TOOL_CALL_EMITTED + TOOL_INVOCATION_*.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

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


# ── scripted mock LLM ─────────────────────────────────────────────────────


@dataclass
class _ScriptedLLM(LLMProvider):
    """Returns the i-th scripted response on the i-th call."""

    script: list[LLMResponse] = field(default_factory=list)
    raise_on_hop: int | None = None
    model: str = "scripted"
    _i: int = 0

    async def stream(  # pragma: no cover — not used
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
        if self.raise_on_hop is not None and self._i == self.raise_on_hop:
            raise RuntimeError("simulated upstream failure")
        if self._i >= len(self.script):
            raise RuntimeError(
                f"_ScriptedLLM exhausted after {len(self.script)} calls"
            )
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── mock tool provider ────────────────────────────────────────────────────


@dataclass
class _StubToolProvider(ToolProvider):
    specs: list[ToolSpec] = field(default_factory=list)
    results: dict[str, ToolResult] = field(default_factory=dict)

    def list_tools(self) -> list[ToolSpec]:
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        result = self.results.get(call.name)
        if result is None:
            return ToolResult(call_id=call.id, ok=False, content=None,
                              error=f"no stub for {call.name}")
        # Recreate with the real call_id.
        return ToolResult(
            call_id=call.id, ok=result.ok, content=result.content,
            error=result.error, latency_ms=result.latency_ms,
            side_effects=result.side_effects,
        )


# ── path 1: plain text ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_text_response_terminates_on_first_hop() -> None:
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="Hello, world.", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    result = await agent.run_turn("sess", "hi")
    await bus.drain()

    assert result.ok
    assert result.text == "Hello, world."
    assert result.hops == 1
    types = [e.type for e in result.events]
    assert EventType.USER_MESSAGE in types
    assert EventType.LLM_REQUEST in types
    assert EventType.LLM_RESPONSE in types
    # No tool events because no tool calls happened.
    assert EventType.TOOL_CALL_EMITTED not in types


# ── path 2: tool call then terminal text ─────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_then_final_text() -> None:
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="echo", args={"x": 1},
                provenance="anthropic", id="tc-1",
            ),),
        ),
        LLMResponse(content="done", tool_calls=()),
    ])
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="echo", description="echoes",
            parameters_schema={"type": "object"},
        )],
        results={
            "echo": ToolResult(
                call_id="", ok=True, content={"echoed": 1},
                side_effects=(),
            ),
        },
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    result = await agent.run_turn("sess", "please echo")
    await bus.drain()

    assert result.ok
    assert result.text == "done"
    assert result.hops == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "echo"
    assert result.tool_calls[0]["ok"] is True

    types = [e.type for e in result.events]
    assert types.count(EventType.LLM_REQUEST) == 2
    assert types.count(EventType.LLM_RESPONSE) == 2
    assert types.count(EventType.TOOL_CALL_EMITTED) == 1
    assert types.count(EventType.TOOL_INVOCATION_STARTED) == 1
    assert types.count(EventType.TOOL_INVOCATION_FINISHED) == 1


@pytest.mark.asyncio
async def test_tool_invocation_finished_carries_side_effects() -> None:
    """Anti-req #4 gate: grader needs the real side_effects list from
    ToolResult, not a hint from the tool spec. Verify the agent loop
    passes them verbatim into TOOL_INVOCATION_FINISHED.payload."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="writer", args={}, provenance="anthropic",
            ),),
        ),
        LLMResponse(content="ok", tool_calls=()),
    ])
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="writer", description="writes",
            parameters_schema={"type": "object"},
        )],
        results={
            "writer": ToolResult(
                call_id="", ok=True, content={"path": "/tmp/x"},
                side_effects=("/tmp/x",),
            ),
        },
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    result = await agent.run_turn("sess", "write it")
    await bus.drain()

    finished = [
        e for e in result.events
        if e.type == EventType.TOOL_INVOCATION_FINISHED
    ]
    assert len(finished) == 1
    assert finished[0].payload["expected_side_effects"] == ["/tmp/x"]


# ── path 3: LLM raises ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_exception_surfaces_as_ok_false_no_propagation() -> None:
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[], raise_on_hop=0)  # raises immediately
    agent = AgentLoop(llm=llm, bus=bus)
    result = await agent.run_turn("sess", "hi")
    await bus.drain()

    assert result.ok is False
    assert "simulated upstream failure" in result.error
    # LLM_RESPONSE with ok=False is emitted for the failed hop.
    failed = [
        e for e in result.events
        if e.type == EventType.LLM_RESPONSE and e.payload.get("ok") is False
    ]
    assert len(failed) == 1


# ── path 4: tool call without a provider ─────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_without_provider_raises_anti_req_violation() -> None:
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="phantom", args={}, provenance="anthropic",
            ),),
        ),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=None)
    result = await agent.run_turn("sess", "hi")
    await bus.drain()

    assert result.ok is False
    violations = [
        e for e in result.events
        if e.type == EventType.ANTI_REQ_VIOLATION
    ]
    assert len(violations) == 1
    assert "no ToolProvider" in violations[0].payload["message"]


# ── path 5: hop limit ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hop_limit_terminates_and_emits_violation() -> None:
    """Model keeps calling tools forever — loop halts at max_hops."""
    bus = InProcessEventBus()
    loop_response = LLMResponse(
        content="",
        tool_calls=(ToolCall(
            name="noop", args={}, provenance="anthropic",
        ),),
    )
    llm = _ScriptedLLM(script=[loop_response] * 20)
    tools = _StubToolProvider(
        specs=[ToolSpec(name="noop", description="", parameters_schema={})],
        results={"noop": ToolResult(
            call_id="", ok=True, content={}, side_effects=(),
        )},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools, max_hops=3)
    result = await agent.run_turn("sess", "loop forever")
    await bus.drain()

    assert result.ok is False
    assert "max_hops" in result.error
    assert result.hops == 3
    violations = [
        e for e in result.events
        if e.type == EventType.ANTI_REQ_VIOLATION
    ]
    assert len(violations) == 1
    assert "max_hops" in violations[0].payload["message"]


# ── user message is always published ─────────────────────────────────────


@pytest.mark.asyncio
async def test_user_message_always_published_even_on_immediate_failure() -> None:
    """Ensures the grader sees the user message even when the first
    LLM call crashes — otherwise the audit trail is incomplete."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[], raise_on_hop=0)
    agent = AgentLoop(llm=llm, bus=bus)
    result = await agent.run_turn("sess", "hello")
    await bus.drain()
    user_events = [e for e in result.events if e.type == EventType.USER_MESSAGE]
    assert len(user_events) == 1
    assert user_events[0].payload["content"] == "hello"
