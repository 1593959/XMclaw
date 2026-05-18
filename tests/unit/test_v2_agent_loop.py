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
    # B-190: graceful truncation — text MUST be non-empty so the UI
    # shows something instead of silently rendering a blank assistant
    # bubble. Should also point at the config knob to raise.
    assert result.text
    assert "agent.max_hops" in result.text
    violations = [
        e for e in result.events
        if e.type == EventType.ANTI_REQ_VIOLATION
    ]
    assert len(violations) == 1
    assert "max_hops" in violations[0].payload["message"]
    assert "tools_used" in violations[0].payload


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
async def test_b299_browse_meta_tool_does_not_stamp_skill_id() -> None:
    """B-299: ``skill_browse`` is the synthesised meta-discovery tool,
    not a registry-backed skill. The agent_loop verdict path used to
    blindly stamp ``skill_id = call.name.removeprefix("skill_")`` for
    every ``skill_*`` tool — which would have created a phantom
    ``skill_id="browse"`` arm in EvolutionAgent + VariantSelector
    every time the LLM used the discovery tool. This test pins that
    skill_browse calls publish a verdict WITHOUT a skill_id so the
    observer's empty-skill_id early return drops them."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="skill_browse",
                args={"query": "find me a git skill"},
                provenance="anthropic", id="tc-browse",
            ),),
        ),
        LLMResponse(content="ok", tool_calls=()),
    ])
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="skill_browse",
            description="meta discovery",
            parameters_schema={"type": "object"},
        )],
        results={
            "skill_browse": ToolResult(
                call_id="", ok=True,
                content={"matches": [], "note": "no results"},
                side_effects=(),
            ),
        },
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    result = await agent.run_turn("sess", "find a skill")
    await bus.drain()

    verdicts = [e for e in result.events if e.type == EventType.GRADER_VERDICT]
    assert len(verdicts) == 1, "verdict still emitted (other observers want it)"
    p = verdicts[0].payload
    # The whole point: NO skill_id stamping, so EvolutionAgent ignores it.
    assert "skill_id" not in p, (
        "skill_browse must NOT stamp skill_id — that would inject a "
        "phantom 'browse' arm into the bandit/EWMA"
    )
    assert "version" not in p
    assert p["tool_name"] == "skill_browse"


@pytest.mark.asyncio
async def test_evolution_agent_observer_receives_skill_verdicts(
    tmp_path,
) -> None:
    """End-to-end: AgentLoop → GRADER_VERDICT → EvolutionAgent._ingest.
    Verifies the closed loop the Phase 1.5 patch was specifically
    written to fix — observer's per (skill_id, version) aggregate
    actually accumulates plays + reward when a skill_-prefixed tool
    runs.

    B-297 added EWMA state persistence to state.json under
    ``evolution_dir() / <agent_id>/``. Without an explicit
    ``audit_dir`` the agent writes to the user's real
    ``~/.xmclaw/v2/evolution/evo-test/state.json``, accumulating plays
    across pytest invocations and breaking the ``plays == 1`` assert
    on the second run onward. Pin ``audit_dir=tmp_path`` so each test
    runs against a clean state file.
    """
    from xmclaw.daemon.evolution_agent import EvolutionAgent

    bus = InProcessEventBus()
    observer = EvolutionAgent("evo-test", bus, audit_dir=tmp_path)
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
        on_thinking_chunk=None, on_tool_block=None, cancel=None,
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


# ── B-202: curriculum-edit hint passive trigger ──────────────────────────


@dataclass
class _CapturingLLM(LLMProvider):
    """Records the user-message content the agent sent it on each call."""

    response_text: str = "ok"
    captured_messages: list[list[Message]] = field(default_factory=list)
    model: str = "capturing"

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
        # Snapshot the messages the agent sent.
        self.captured_messages.append(list(messages))
        return LLMResponse(content=self.response_text, tool_calls=())

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


def _last_user_message(messages: list[Message]) -> str:
    """Return the trailing user message body the LLM saw."""
    for m in reversed(messages):
        if m.role == "user":
            content = m.content
            return content if isinstance(content, str) else str(content)
    return ""


@pytest.mark.asyncio
async def test_b202_frustration_injects_curriculum_hint_when_tool_present() -> None:
    """Frustration markers + tool wired ⇒ hint block lands in the user
    message the LLM sees."""
    bus = InProcessEventBus()
    llm = _CapturingLLM()
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="propose_curriculum_edit",
            description="propose a curriculum lesson",
            parameters_schema={"type": "object"},
        )],
        results={},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-b202-1", "为什么你又这样做？错了！")
    await bus.drain()

    sent = _last_user_message(llm.captured_messages[-1])
    assert "<curriculum-hint>" in sent
    assert "propose_curriculum_edit" in sent


@pytest.mark.asyncio
async def test_b202_no_hint_when_tool_not_wired() -> None:
    """Without ``propose_curriculum_edit`` registered we must NOT
    surface the hint — would be misleading and waste tokens."""
    bus = InProcessEventBus()
    llm = _CapturingLLM()
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="echo", description="",
            parameters_schema={"type": "object"},
        )],
        results={},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-b202-2", "为什么你又这样做？")
    await bus.drain()

    sent = _last_user_message(llm.captured_messages[-1])
    assert "<curriculum-hint>" not in sent


@pytest.mark.asyncio
async def test_b202_no_hint_on_neutral_message() -> None:
    """Neutral message ⇒ hint stays silent (false-positive guard)."""
    bus = InProcessEventBus()
    llm = _CapturingLLM()
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="propose_curriculum_edit",
            description="propose",
            parameters_schema={"type": "object"},
        )],
        results={},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-b202-3", "Please write me a haiku.")
    await bus.drain()

    sent = _last_user_message(llm.captured_messages[-1])
    assert "<curriculum-hint>" not in sent


@pytest.mark.asyncio
async def test_b202_hint_dedup_within_session() -> None:
    """Once-per-session: hint fires on turn 1, NOT on turn 2 even when
    the user shows frustration again. Repeating the hint every turn
    would tilt the agent toward over-proposing curriculum edits and
    dilute the signal."""
    bus = InProcessEventBus()
    llm = _CapturingLLM()
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="propose_curriculum_edit",
            description="propose",
            parameters_schema={"type": "object"},
        )],
        results={},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-b202-4", "你不要又错！")  # turn 1, frustrated
    await agent.run_turn("sess-b202-4", "为什么还是错？")  # turn 2, still frustrated
    await bus.drain()

    turn1 = _last_user_message(llm.captured_messages[0])
    turn2 = _last_user_message(llm.captured_messages[1])
    assert "<curriculum-hint>" in turn1
    assert "<curriculum-hint>" not in turn2


@pytest.mark.asyncio
async def test_b202_hint_resets_on_clear_session() -> None:
    """After ``clear_session`` the dedup flag drops — a fresh
    session should be eligible for the hint again."""
    bus = InProcessEventBus()
    llm = _CapturingLLM()
    tools = _StubToolProvider(
        specs=[ToolSpec(
            name="propose_curriculum_edit",
            description="propose",
            parameters_schema={"type": "object"},
        )],
        results={},
    )
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    await agent.run_turn("sess-b202-5", "为什么又错")
    agent.clear_session("sess-b202-5")
    await agent.run_turn("sess-b202-5", "你看看，错了")
    await bus.drain()

    turn1 = _last_user_message(llm.captured_messages[0])
    turn_after_reset = _last_user_message(llm.captured_messages[1])
    assert "<curriculum-hint>" in turn1
    assert "<curriculum-hint>" in turn_after_reset


# ── B-351 (Sprint 1): tool invoke uncaught exception → synthetic
#                       failure ToolResult, finish event still fires.


class _RaisingToolProvider(ToolProvider):
    """ToolProvider whose ``invoke`` violates the contract by RAISING
    instead of returning a ToolResult. Real-world this happens when an
    MCP bridge has a network exception its own try/except missed, or
    when a custom ToolProvider has a contract bug.
    """

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="bad_tool", description="raises", parameters_schema={})]

    async def invoke(self, call: ToolCall) -> ToolResult:
        raise RuntimeError("simulated upstream connection reset")


@pytest.mark.asyncio
async def test_b351_tool_invoke_uncaught_exception_still_emits_finish() -> None:
    """Pre-B-351: if ToolProvider.invoke() raised, the agent loop
    propagated the exception, TOOL_INVOCATION_FINISHED never fired,
    and the UI's tool_use bubble stayed at status="running" forever
    with no way to recover.
    Now: agent_loop wraps invoke in try/except, synthesizes a
    failed ToolResult on uncaught exception, and STILL publishes
    TOOL_INVOCATION_FINISHED with the error string. UI flips to
    "error" instead of stuck-running.
    """
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        # First hop: ask for the bad tool.
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(id="t1", name="bad_tool", args={}, provenance="synthetic"),
            ),
        ),
        # Second hop: terminal text after the synthetic error landed.
        LLMResponse(content="couldn't run the tool", tool_calls=()),
    ])
    tools = _RaisingToolProvider()
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)

    # Must NOT raise — the uncaught exception inside invoke() is
    # converted to a failed ToolResult inside the agent loop.
    result = await agent.run_turn("sess-b351", "trigger bad tool")
    await bus.drain()

    assert result.ok  # turn-level still OK; tool-level was failed
    finish_events = [
        e for e in result.events
        if e.type == EventType.TOOL_INVOCATION_FINISHED
    ]
    assert len(finish_events) == 1, (
        "TOOL_INVOCATION_FINISHED must fire even when the tool raised"
    )
    payload = finish_events[0].payload
    assert payload["call_id"] == "t1"
    assert payload["ok"] is False
    assert payload["error"], "error string must be populated"
    # The error must mention both the exception type and the
    # contract-violation note so debug is easy.
    assert "RuntimeError" in payload["error"]
    assert "uncaught" in payload["error"].lower() or "contract" in payload["error"].lower()
