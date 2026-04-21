"""Daemon + AgentLoop integration — end-to-end WS conversation.

With an ``AgentLoop`` wired into ``create_app``, a user frame sent
through the WebSocket triggers a real LLM ↔ tool loop and streams
every BehavioralEvent back to the client. This test uses scripted
mock LLMs (no network, no API key) to exercise the full plumbing.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.daemon.app import create_app
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider


@dataclass
class _ScriptedLLM(LLMProvider):
    script: list[LLMResponse] = field(default_factory=list)
    _i: int = 0

    async def stream(  # pragma: no cover
        self, messages, tools=None, *, cancel=None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(self, messages, tools=None):  # noqa: ANN001
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@dataclass
class _StubTools(ToolProvider):
    specs: list[ToolSpec] = field(default_factory=list)

    def list_tools(self) -> list[ToolSpec]:
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"stub-result for {call.name}",
            side_effects=(),
        )


def _collect_ws_events(ws, max_events: int = 20, max_wait: float = 0.5):
    """Greedily pull events off the WS until no more arrive within max_wait."""
    events = []
    while len(events) < max_events:
        try:
            raw = ws.receive_text(timeout=max_wait)
        except (Exception, TimeoutError):
            break
        events.append(json.loads(raw))
    return events


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


# ── plain text response ──────────────────────────────────────────────────


def test_daemon_with_agent_delivers_text_response(bus: InProcessEventBus) -> None:
    llm = _ScriptedLLM(script=[
        LLMResponse(content="Hi from the agent.", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/sess-chat") as ws:
        # Drain session_create.
        ws.receive_json()

        ws.send_text(json.dumps({"type": "user", "content": "hello"}))

        # Agent emits USER_MESSAGE + LLM_REQUEST + LLM_RESPONSE.
        types_seen: list[str] = []
        response_content: str | None = None
        for _ in range(10):
            evt = ws.receive_json()
            types_seen.append(evt["type"])
            if evt["type"] == EventType.LLM_RESPONSE.value:
                # Stop collecting once the terminal LLM_RESPONSE lands.
                # Capture the content_length for cross-check.
                response_content = "done"
                break

        assert EventType.USER_MESSAGE.value in types_seen
        assert EventType.LLM_REQUEST.value in types_seen
        assert EventType.LLM_RESPONSE.value in types_seen
        assert response_content == "done"


# ── tool-aware path ──────────────────────────────────────────────────────


def test_daemon_runs_tool_call_through_ws(bus: InProcessEventBus) -> None:
    tools = _StubTools(specs=[
        ToolSpec(name="echo", description="echoes", parameters_schema={"type": "object"}),
    ])
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="echo", args={"x": 1},
                provenance="anthropic", id="tc-ws-1",
            ),),
        ),
        LLMResponse(content="finished", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/sess-tool") as ws:
        ws.receive_json()  # session_create

        ws.send_text(json.dumps({"type": "user", "content": "please echo"}))

        # Collect up to 12 frames — the tool-round trip produces
        # roughly 8 events (user, llm_req, llm_resp, tool_emitted,
        # tool_started, tool_finished, llm_req, llm_resp).
        received: list[dict] = []
        for _ in range(12):
            evt = ws.receive_json()
            received.append(evt)
            if evt["type"] == EventType.LLM_RESPONSE.value and evt["payload"].get("content_length", 0) > 0:
                break

        types = [e["type"] for e in received]
        assert types.count(EventType.LLM_REQUEST.value) >= 2
        assert types.count(EventType.LLM_RESPONSE.value) >= 2
        assert EventType.TOOL_CALL_EMITTED.value in types
        assert EventType.TOOL_INVOCATION_STARTED.value in types
        assert EventType.TOOL_INVOCATION_FINISHED.value in types

        # Tool invocation finished event must carry the call id + result.
        finished = next(
            e for e in received
            if e["type"] == EventType.TOOL_INVOCATION_FINISHED.value
        )
        assert finished["payload"]["call_id"] == "tc-ws-1"
        assert finished["payload"]["ok"] is True


# ── agent crash is surfaced, not swallowed ───────────────────────────────


def test_daemon_surfaces_anti_req_violation_when_agent_crashes(
    bus: InProcessEventBus,
) -> None:
    """Agent.run_turn shouldn't raise — but if it does, daemon publishes
    an ANTI_REQ_VIOLATION so the client never sees a silent socket stall."""
    class _CrashingAgent(AgentLoop):
        async def run_turn(self, session_id, user_message):  # type: ignore[override]
            raise RuntimeError("agent blew up")

    agent = _CrashingAgent(
        llm=_ScriptedLLM(script=[]),  # never called
        bus=bus,
    )
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/sess-crash") as ws:
        ws.receive_json()  # session_create
        ws.send_text(json.dumps({"type": "user", "content": "trigger"}))

        # First frame after send should be the ANTI_REQ_VIOLATION.
        for _ in range(3):
            evt = ws.receive_json()
            if evt["type"] == EventType.ANTI_REQ_VIOLATION.value:
                assert "agent blew up" in evt["payload"]["message"]
                return
        pytest.fail("did not receive ANTI_REQ_VIOLATION after agent crash")
