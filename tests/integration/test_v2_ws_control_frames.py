"""WS control frames must be handled while a turn is in flight.

Regression for the 2026-06-11 "QuestionCard click takes minutes" bug:
the WS handler awaited ``run_turn`` inline in its receive loop, so an
``answer_question`` (or ``cancel``) frame sent mid-turn sat unread in
the socket buffer. ``ask_user_question`` blocked on its Future until
the 180s tool wall-clock killed it — the user's click was then resolved
against a dead future (``resolved: false``) and silently dropped.

These tests drive the REAL stack end-to-end (per the front-back
boundary rule): real ``create_app`` WS endpoint, real ``AgentLoop``,
real ``BuiltinTools.ask_user_question`` future plumbing — only the LLM
is scripted.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape, ToolCall
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.app import create_app
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Pricing,
)
from xmclaw.providers.tool.builtin import BuiltinTools


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
        resp = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


def _make_client(bus: InProcessEventBus) -> TestClient:
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="ask_user_question",
                args={
                    "question": "Pick one",
                    "options": [
                        {"label": "Alpha", "value": "alpha"},
                        {"label": "Beta", "value": "beta"},
                    ],
                },
                provenance="anthropic", id="tc-ask-1",
            ),),
        ),
        LLMResponse(content="final answer after question", tool_calls=()),
    ])
    tools = BuiltinTools(enable_bash=False, enable_web=False)
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    return TestClient(create_app(bus=bus, agent=agent))


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


def test_answer_question_frame_resolves_mid_turn(bus: InProcessEventBus) -> None:
    """Click on a QuestionCard option must unblock the turn immediately —
    the answer frame is processed WHILE run_turn is awaiting the tool."""
    client = _make_client(bus)
    with client.websocket_connect("/agent/v2/sess-ask-e2e") as ws:
        ws.receive_json()  # session_create

        ws.send_text(json.dumps({"type": "user", "content": "ask me"}))

        # Wait for the live AGENT_ASKED_QUESTION event to learn the qid.
        qid = None
        for _ in range(30):
            evt = ws.receive_json()
            if evt["type"] == EventType.AGENT_ASKED_QUESTION.value:
                qid = evt["payload"]["question_id"]
                break
        assert qid, "AGENT_ASKED_QUESTION never arrived"

        # The turn is now blocked inside ask_user_question. Send the
        # answer on the same socket — pre-fix this frame was never read
        # until the tool wall-clock expired.
        ws.send_text(json.dumps({
            "type": "answer_question",
            "question_id": qid,
            "value": "alpha",
        }))

        saw_resolved = False
        saw_final = False
        for _ in range(40):
            evt = ws.receive_json()
            if evt["type"] == EventType.USER_ANSWERED_QUESTION.value:
                assert evt["payload"]["resolved"] is True
                assert evt["payload"]["value"] == "alpha"
                saw_resolved = True
            if (
                evt["type"] == EventType.LLM_RESPONSE.value
                and not evt["payload"].get("tool_calls")
            ):
                saw_final = True
            if saw_resolved and saw_final:
                break
        assert saw_resolved, "answer was not resolved against the live future"
        assert saw_final, "turn did not complete after the answer"


def test_cancel_frame_unblocks_pending_question(bus: InProcessEventBus) -> None:
    """Stop button must cancel a turn blocked on an unanswered question.
    With ask_user_question exempt from the tool wall-clock, the cancel
    frame is the only escape hatch — verify it works mid-turn."""
    client = _make_client(bus)
    with client.websocket_connect("/agent/v2/sess-ask-cancel") as ws:
        ws.receive_json()  # session_create

        ws.send_text(json.dumps({"type": "user", "content": "ask me"}))

        qid = None
        for _ in range(30):
            evt = ws.receive_json()
            if evt["type"] == EventType.AGENT_ASKED_QUESTION.value:
                qid = evt["payload"]["question_id"]
                break
        assert qid, "AGENT_ASKED_QUESTION never arrived"

        ws.send_text(json.dumps({"type": "cancel"}))

        # The cancelled future makes the tool return a failed result;
        # the scripted LLM then produces its final text and the turn
        # ends — no 180s stall.
        saw_cancel_ack = False
        saw_final = False
        for _ in range(40):
            evt = ws.receive_json()
            if (
                evt["type"] == EventType.SESSION_LIFECYCLE.value
                and evt["payload"].get("phase") == "cancel_requested"
            ):
                saw_cancel_ack = True
            if (
                evt["type"] == EventType.LLM_RESPONSE.value
                and not evt["payload"].get("tool_calls")
            ):
                saw_final = True
                break
        assert saw_cancel_ack, "cancel frame was not acknowledged mid-turn"
        assert saw_final, "turn did not unwind after cancel"
