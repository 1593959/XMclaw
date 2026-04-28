"""AgentLoop -- multi-turn conversation memory (the "doesn't know who he
is" bug fix).

Before this test existed, ``AgentLoop.run_turn`` rebuilt the ``messages``
list from scratch every call -- just [system, user] -- discarding every
prior exchange. The agent had no way to remember what the user said
three turns ago, or even what it itself said one turn ago.

These tests lock the fix in place: each turn sees the transcript of
every preceding turn (user + assistant + tool messages) and trimming
respects the structural invariant that a ``role="tool"`` message must
follow an ``role="assistant"`` message that emitted that tool_call.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider


# ── recording LLM -- captures what messages each complete() saw ──────────


@dataclass
class _RecordingLLM(LLMProvider):
    """Remembers every ``messages`` list it was called with, so the test
    can assert that turn N saw turn N-1's exchange."""

    script: list[LLMResponse] = field(default_factory=list)
    seen_messages: list[list[Message]] = field(default_factory=list)
    model: str = "recorder"
    _i: int = 0

    async def stream(  # pragma: no cover
        self, messages, tools=None, *, cancel=None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(self, messages, tools=None):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn2_sees_turn1_exchange() -> None:
    """The literal "doesn't know who I am" bug. Turn 1: user tells agent
    their name. Turn 2: user asks "what's my name". The LLM MUST see the
    turn-1 exchange in the messages it's given on turn 2."""
    llm = _RecordingLLM(script=[
        LLMResponse(content="Nice to meet you, Alice."),
        LLMResponse(content="Your name is Alice."),
    ])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    r1 = await agent.run_turn("s1", "Hi, my name is Alice.")
    assert r1.ok
    r2 = await agent.run_turn("s1", "What's my name?")
    assert r2.ok

    # On turn 2 the LLM should have received: system, turn1-user,
    # turn1-assistant, turn2-user. So any message in the second call
    # should mention "Alice".
    turn2_msgs = llm.seen_messages[1]
    assert any(
        m.role == "user" and "Alice" in (m.content or "")
        for m in turn2_msgs
    ), f"turn 2 messages lost turn 1's user text: {turn2_msgs}"
    assert any(
        m.role == "assistant" and "Alice" in (m.content or "")
        for m in turn2_msgs
    ), f"turn 2 messages lost turn 1's assistant text: {turn2_msgs}"


@pytest.mark.asyncio
async def test_sessions_are_isolated() -> None:
    """History for session A must not leak into session B."""
    llm = _RecordingLLM(script=[
        LLMResponse(content="OK session A."),
        LLMResponse(content="Fresh session B."),
    ])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    await agent.run_turn("alpha", "I am in alpha.")
    await agent.run_turn("beta", "I am in beta.")

    # Session beta's LLM call should not see the alpha user message.
    beta_msgs = llm.seen_messages[1]
    assert not any(
        "alpha" in (m.content or "") for m in beta_msgs
    ), f"session beta saw alpha's history: {beta_msgs}"


@pytest.mark.asyncio
async def test_clear_session_drops_history() -> None:
    """``clear_session`` resets a session to its initial state."""
    llm = _RecordingLLM(script=[
        LLMResponse(content="first"),
        LLMResponse(content="second"),
    ])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    await agent.run_turn("s1", "remember this")
    agent.clear_session("s1")
    await agent.run_turn("s1", "new topic")

    # The second run's LLM call should see only [system, new_user]
    # because we wiped history in between.
    second_msgs = llm.seen_messages[1]
    roles = [m.role for m in second_msgs]
    assert roles == ["system", "user"], (
        f"clear_session didn't drop history -- roles were {roles}"
    )


@pytest.mark.asyncio
async def test_failed_turn_does_not_poison_history() -> None:
    """If the LLM raises on turn 2, turn 3 must still see turn 1's history
    cleanly -- not a truncated mess or an empty state."""

    @dataclass
    class _FailsOnCall(_RecordingLLM):
        """Fail on Nth call, don't consume a script slot for failures
        so the happy-path script stays aligned."""
        fail_on_call_idx: int = -1
        _calls: int = 0

        async def complete(self, messages, tools=None):  # noqa: ANN001
            self.seen_messages.append(list(messages))
            call_idx = self._calls
            self._calls += 1
            if call_idx == self.fail_on_call_idx:
                raise RuntimeError("simulated upstream failure")
            resp = self.script[self._i]
            self._i += 1
            return resp

    llm = _FailsOnCall(
        script=[
            LLMResponse(content="turn 1 response"),
            LLMResponse(content="turn 3 response"),
        ],
        fail_on_call_idx=1,  # 2nd complete() call raises
    )
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    await agent.run_turn("s1", "turn 1 user")
    r2 = await agent.run_turn("s1", "turn 2 user which will fail")
    assert not r2.ok
    r3 = await agent.run_turn("s1", "turn 3 user")
    assert r3.ok

    # The LLM's third call (index 2 in seen_messages) should contain the
    # turn-1 exchange but NOT the failed turn-2 user message.
    turn3_msgs = llm.seen_messages[2]
    user_msgs = [m for m in turn3_msgs if m.role == "user"]
    assert any("turn 1" in (m.content or "") for m in user_msgs)
    # Turn 2 failed, so its user message should not have been persisted.
    assert not any("turn 2" in (m.content or "") for m in user_msgs), (
        f"failed turn poisoned history: {user_msgs}"
    )


@pytest.mark.asyncio
async def test_history_cap_trims_old_messages() -> None:
    """With a small cap, old messages drop off; the most-recent exchange
    is always retained."""
    # 6 scripted responses for 6 turns.
    script = [LLMResponse(content=f"resp {i}") for i in range(6)]
    llm = _RecordingLLM(script=script)
    agent = AgentLoop(
        llm=llm, bus=InProcessEventBus(), history_cap=4,
    )

    for i in range(6):
        await agent.run_turn("s1", f"user msg {i}")

    # Turn 6's call should be: base system + (optional compression
    # summary system) + ≤4 history + 1 new user. B-28 added the
    # compression summary as a synthetic system message at the head
    # of history when compression fires, so the upper bound is 7.
    # Crucially still ≪ 12 (the no-cap baseline).
    turn6_msgs = llm.seen_messages[-1]
    assert len(turn6_msgs) <= 7, (
        f"history cap not applied: got {len(turn6_msgs)} messages"
    )

    # Most recent user msg must be present; the earliest must be gone.
    user_contents = [m.content for m in turn6_msgs if m.role == "user"]
    assert any("user msg 5" in c for c in user_contents)
    assert not any("user msg 0" in c for c in user_contents), (
        f"oldest user msg should be trimmed: {user_contents}"
    )


# ── tool round trips preserve history across calls ───────────────────────


@dataclass
class _StubTools(ToolProvider):
    specs: list[ToolSpec] = field(default_factory=list)

    def list_tools(self) -> list[ToolSpec]:
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"stub for {call.name}",
            side_effects=(),
        )


@pytest.mark.asyncio
async def test_history_preserves_tool_turns() -> None:
    """A tool round trip inside turn 1 must be carried into turn 2's
    messages (assistant-with-tool_calls + tool_result), because dropping
    either half would violate provider invariants."""
    tools = _StubTools(specs=[
        ToolSpec(name="ping", description="ping", parameters_schema={"type": "object"}),
    ])
    llm = _RecordingLLM(script=[
        # Turn 1: model calls a tool
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="ping", args={}, provenance="anthropic", id="t1",
            ),),
        ),
        # Turn 1: model settles with text after tool result
        LLMResponse(content="done via tool"),
        # Turn 2: plain text
        LLMResponse(content="I remember"),
    ])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus(), tools=tools)

    await agent.run_turn("s1", "please ping")
    await agent.run_turn("s1", "recall earlier")

    # Turn 2 (LLM call index 2) should have: system + [turn1 user, turn1
    # assistant-with-tool, tool result, turn1 assistant text] + turn 2 user.
    turn2_msgs = llm.seen_messages[2]
    roles = [m.role for m in turn2_msgs]
    assert "tool" in roles, f"tool message was lost: {roles}"
    # Tool block must be preceded by an assistant message (provider invariant).
    tool_idx = roles.index("tool")
    assert roles[tool_idx - 1] == "assistant", (
        f"tool result not immediately preceded by assistant: {roles}"
    )
