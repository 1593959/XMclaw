"""Daemon + AgentLoop multi-turn -- conversation memory across the WS.

Complements the unit tests in test_v2_agent_memory.py: this one drives
the full stack (WebSocket -> app.py handler -> AgentLoop.run_turn) to
prove the history survives the real entry point too. Any regression
that made run_turn stateless again would fail both suites, but this
one would also catch bugs in the WS-level plumbing (session_id threading,
clear_session on disconnect, etc).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.app import create_app
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


@dataclass
class _RecordingLLM(LLMProvider):
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


def _drain_until_llm_response(ws, max_events: int = 15) -> None:
    """Pull frames off the WS until we see an LLM_RESPONSE (end of turn)."""
    for _ in range(max_events):
        evt = ws.receive_json()
        if evt["type"] == EventType.LLM_RESPONSE.value:
            return
    raise AssertionError("never saw an LLM_RESPONSE frame")


def test_ws_turn2_sees_turn1_context() -> None:
    """Send two user messages over the same WS session -- the second
    run_turn's LLM call must include the first exchange."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[
        LLMResponse(content="Nice to meet you, Alice."),
        LLMResponse(content="Your name is Alice."),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/memory-sess") as ws:
        ws.receive_json()  # session_lifecycle: create

        ws.send_text(json.dumps({"type": "user", "content": "Hi, my name is Alice."}))
        _drain_until_llm_response(ws)

        ws.send_text(json.dumps({"type": "user", "content": "What's my name?"}))
        _drain_until_llm_response(ws)

    # Assert on what the LLM was shown on its SECOND call.
    assert len(llm.seen_messages) == 2
    turn2 = llm.seen_messages[1]
    user_content = " ".join(m.content or "" for m in turn2 if m.role == "user")
    asst_content = " ".join(m.content or "" for m in turn2 if m.role == "assistant")
    assert "Alice" in user_content, f"turn 2 lost user history: {user_content}"
    assert "Alice" in asst_content, f"turn 2 lost assistant history: {asst_content}"


def test_ws_three_turn_reference_chain() -> None:
    """Realistic 3-turn test: agent establishes a fact in turn 1, user
    references it in turn 2 and 3, and turn 3 must see the full chain."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[
        LLMResponse(content="Got it, you're working on project Hermes."),
        LLMResponse(content="Hermes is a messaging API, based on what you told me."),
        LLMResponse(content="Yes, Hermes's messaging design is what we discussed."),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/chain-sess") as ws:
        ws.receive_json()
        for msg in [
            "I'm building a project called Hermes.",
            "What does Hermes do?",
            "Can you summarize Hermes's messaging design?",
        ]:
            ws.send_text(json.dumps({"type": "user", "content": msg}))
            _drain_until_llm_response(ws)

    turn3 = llm.seen_messages[2]
    # Must see all 3 user messages and the first 2 assistant responses.
    users = [m.content for m in turn3 if m.role == "user"]
    assts = [m.content for m in turn3 if m.role == "assistant"]
    assert len(users) == 3, f"turn 3 should see 3 user msgs, got {users}"
    assert len(assts) == 2, f"turn 3 should see 2 asst msgs, got {assts}"
    assert any("project called Hermes" in u for u in users)
    assert any("What does Hermes do" in u for u in users)


def test_ws_reconnect_preserves_session_history() -> None:
    """Browser refresh is a WS close, and the user's prior exchanges
    MUST survive it. The earlier auto-wipe-on-disconnect behavior meant
    refreshing the tab turned a 20-turn conversation into a fresh
    stranger, which the user rightly called a data-loss bug.

    Fix: history stays in the AgentLoop keyed by session_id. Re-opening
    the same session from a new WS connection sees the full prior
    transcript, so turn N+1 still references turn N even after a
    refresh. Explicit ``clear_session`` is still available for a
    /reset intent from the UI.
    """
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[
        LLMResponse(content="first-connect reply"),
        LLMResponse(content="second-connect reply"),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/same-id") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "my secret is hunter2"}))
        _drain_until_llm_response(ws)

    # Second connection reuses the same session_id (simulates refresh).
    with client.websocket_connect("/agent/v2/same-id") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "what's my secret?"}))
        _drain_until_llm_response(ws)

    second = llm.seen_messages[1]
    user_text = " ".join(m.content or "" for m in second if m.role == "user")
    assistant_text = " ".join(m.content or "" for m in second if m.role == "assistant")
    # The prior user turn ("my secret is hunter2") must still be visible
    # to the LLM on the second connection.
    assert "hunter2" in user_text, (
        f"refresh lost prior user history: {user_text!r}"
    )
    # And the prior assistant reply must be in the history too.
    assert "first-connect reply" in assistant_text, (
        f"refresh lost prior assistant history: {assistant_text!r}"
    )


def test_clear_session_still_available_for_explicit_reset() -> None:
    """Even though disconnect no longer wipes, ``clear_session`` is the
    explicit reset path used by a /reset intent from the UI."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[
        LLMResponse(content="first"),
        LLMResponse(content="second"),
    ])
    agent = AgentLoop(llm=llm, bus=bus)

    import asyncio
    asyncio.run(agent.run_turn("reset-me", "first user msg"))
    agent.clear_session("reset-me")
    asyncio.run(agent.run_turn("reset-me", "second user msg"))

    second = llm.seen_messages[1]
    roles = [m.role for m in second]
    # After explicit clear, history is gone: only system + new user.
    assert roles == ["system", "user"], (
        f"clear_session didn't drop history: {roles}"
    )
