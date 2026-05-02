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


# ── Epic #24 Phase 1.5: HonestGrader → GRADER_VERDICT plumbing ─────────


@pytest.mark.asyncio
async def test_grader_verdict_published_after_tool_invocation() -> None:
    """Every TOOL_INVOCATION_FINISHED must be paired with a GRADER_VERDICT
    so the EvolutionAgent observer has data to aggregate (Epic #24)."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="bash", args={"cmd": "echo hi"},
                provenance="anthropic", id="tc-1",
            ),),
        ),
        LLMResponse(content="done", tool_calls=()),
    ])
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="bash", description="run shell",
            parameters_schema={"type": "object"},
        )],
        results={
            "bash": ToolResult(
                call_id="", ok=True, content="hi",
                side_effects=(),
            ),
        },
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    result = await agent.run_turn("sess", "run echo")
    await bus.drain()

    verdicts = [e for e in result.events if e.type == EventType.GRADER_VERDICT]
    assert len(verdicts) == 1, "exactly one GRADER_VERDICT per tool call"
    p = verdicts[0].payload
    assert p["call_id"] == "tc-1"
    assert p["tool_name"] == "bash"
    assert p["ran"] is True
    assert p["returned"] is True
    assert isinstance(p["score"], float)
    assert 0.0 <= p["score"] <= 1.0
    # Non-skill tool: skill_id / version not stamped.
    assert "skill_id" not in p


@pytest.mark.asyncio
async def test_grader_verdict_carries_skill_id_for_skill_prefixed_tools() -> None:
    """When the tool name comes from SkillToolProvider (``skill_<id>``
    with ``__`` for ``.``), the verdict payload must reverse the encoding
    and stamp ``skill_id`` so EvolutionAgent's _ingest can aggregate.
    Without this, Phase 1's evolution feedback loop is silently empty."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                # demo.read_and_summarize → skill_demo__read_and_summarize
                name="skill_demo__read_and_summarize",
                args={}, provenance="anthropic", id="tc-skill",
            ),),
        ),
        LLMResponse(content="summarized", tool_calls=()),
    ])
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="skill_demo__read_and_summarize",
            description="bridged skill",
            parameters_schema={"type": "object"},
        )],
        results={
            "skill_demo__read_and_summarize": ToolResult(
                call_id="", ok=True, content="ok",
                side_effects=(),
            ),
        },
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    result = await agent.run_turn("sess", "do skill")
    await bus.drain()

    verdicts = [e for e in result.events if e.type == EventType.GRADER_VERDICT]
    assert len(verdicts) == 1
    p = verdicts[0].payload
    assert p["skill_id"] == "demo.read_and_summarize"
    assert p["version"] == 0  # Phase 1.5 default; Phase 3 fans out


@pytest.mark.asyncio
async def test_evolution_agent_observer_receives_skill_verdicts() -> None:
    """End-to-end: AgentLoop → GRADER_VERDICT → EvolutionAgent._ingest.
    Verifies the closed loop the Phase 1.5 patch was specifically
    written to fix — observer's per (skill_id, version) aggregate
    actually accumulates plays + reward when a skill_-prefixed tool
    runs."""
    from xmclaw.daemon.evolution_agent import EvolutionAgent

    bus = InProcessEventBus()
    observer = EvolutionAgent("evo-test", bus)
    await observer.start()
    try:
        llm = _ScriptedLLM(script=[
            LLMResponse(
                content="",
                tool_calls=(ToolCall(
                    name="skill_summary", args={},
                    provenance="anthropic", id="tc-1",
                ),),
            ),
            LLMResponse(content="ok", tool_calls=()),
        ])
        tools = _StubToolProvider(
            specs=[ToolSpec(
                name="skill_summary", description="bridged",
                parameters_schema={"type": "object"},
            )],
            results={
                "skill_summary": ToolResult(
                    call_id="", ok=True, content="result",
                    side_effects=(),
                ),
            },
        )
        agent = AgentLoop(llm=llm, bus=bus, tools=tools)
        await agent.run_turn("sess", "go")
        await bus.drain()

        evals = observer.snapshot()
        assert len(evals) == 1, "observer aggregated exactly one arm"
        e = evals[0]
        assert e.candidate_id == "summary"
        assert e.version == 0
        assert e.plays == 1
        assert e.mean_score > 0.0  # ran=True/returned=True yields positive score
    finally:
        await observer.stop()


# ── B-189: LLM provider wall-clock timeout ────────────────────────


class _HangingLLM(LLMProvider):
    """Provider whose ``complete_streaming`` blocks forever — simulates
    the chat-59bb7a7a hop-6 stall where the cloud LLM stopped
    responding without raising."""

    model: str = "hanging"

    async def stream(self, messages, tools=None, *, cancel=None):  # noqa: D401, ANN001
        if False:
            yield  # type: ignore[unreachable]

    async def complete_streaming(  # noqa: D401, ANN001
        self, messages, tools=None, *, on_chunk=None,
        on_thinking_chunk=None, cancel=None,
    ):
        await asyncio.Event().wait()  # blocks until cancelled

    async def complete(self, messages, tools=None):  # noqa: D401, ANN001
        await asyncio.Event().wait()

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.mark.asyncio
async def test_llm_call_timeout_aborts_turn_with_clear_error() -> None:
    """B-189: hanging provider call must abort within
    ``llm_timeout_s`` rather than wedge the turn forever. The turn
    returns an error result; the bus carries one ANTI_REQ_VIOLATION
    + one LLM_RESPONSE(ok=False)."""
    bus = InProcessEventBus()
    captured: list = []
    bus.subscribe(lambda e: True, lambda e: captured.append(e) or None)

    agent = AgentLoop(
        llm=_HangingLLM(), bus=bus,
        llm_timeout_s=0.3,  # tight bound for fast test
    )
    out = await agent.run_turn("sess-timeout", "anything")
    await bus.drain()

    assert out.ok is False
    assert "timed out" in (out.error or "").lower()

    # Bus must reflect both signals.
    types = [e.type.value for e in captured]
    assert "anti_req_violation" in types
    # LLM_RESPONSE event with ok=False carries the error string the
    # WS client renders to the user.
    llm_resps = [e for e in captured if e.type.value == "llm_response"]
    assert any(
        not r.payload.get("ok") and "timed out" in str(
            r.payload.get("error", "")
        ).lower()
        for r in llm_resps
    )


@pytest.mark.asyncio
async def test_llm_timeout_min_floor() -> None:
    """``llm_timeout_s`` floors at 5s so a config typo (0 / negative)
    can't accidentally disable the safety net."""
    bus = InProcessEventBus()
    agent = AgentLoop(
        llm=_HangingLLM(), bus=bus,
        llm_timeout_s=0.0,  # caller asked for "no timeout"
    )
    # Internal value clamped to 5.0 — we don't want to wait that long
    # in a test, so just verify the field directly.
    assert agent._llm_timeout_s >= 5.0  # noqa: SLF001 — pin the floor
