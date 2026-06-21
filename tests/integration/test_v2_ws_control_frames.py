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

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape, ToolCall, ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.app import create_app
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider
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

    async def complete_streaming(  # noqa: ANN001
        self, messages, tools=None, *, on_chunk=None, on_thinking_chunk=None,
        on_tool_block=None, on_stream_fallback=None, cancel=None,
        extended_thinking=None,
    ):
        # Real streaming stub: fire a first-token chunk (even for a
        # tool-call-only response with empty content) so the hop-loop's
        # first-token guard is satisfied — otherwise it stalls on the
        # _first_token_timeout for every no-content hop.
        resp = await self.complete(messages, tools=tools)
        if on_chunk is not None:
            await on_chunk(resp.content or " ")
        if on_tool_block is not None:
            for tc in (resp.tool_calls or ()):
                on_tool_block(tc)
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
    # Force the multi-hop tool path. Short prompts ("ask me") otherwise
    # route to the instant single-shot, which passes no tools — the
    # scripted ask_user_question never fires, so the test would block
    # forever on receive_json() waiting for AGENT_ASKED_QUESTION.
    agent._mode_instant_enabled = False
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


# ── 2026-06-15: Stop / new-message must HARD-cancel a running tool ──


_SLEEP_SECONDS = 15.0  # long enough that a cooperative-only cancel can't
                       # interrupt it; bounds regression runtime.


class _SleepToolProvider(ToolProvider):
    """A tool that just sleeps — stands in for any long-running tool
    (video poll, slow bash, web fetch) that NEVER reaches the hop-loop's
    cooperative cancel check. Only a hard task-cancel can interrupt it."""

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="sleep_long",
            description="Sleep for a long time (test fixture).",
            parameters_schema={"type": "object", "properties": {}},
            read_only=True,
        )]

    async def invoke(self, call: ToolCall) -> ToolResult:
        await asyncio.sleep(_SLEEP_SECONDS)
        return ToolResult(call_id=call.id, ok=True, content="slept")


def _make_sleep_client(bus: InProcessEventBus) -> TestClient:
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="sleep_long", args={},
                provenance="anthropic", id="tc-sleep-1",
            ),),
        ),
        LLMResponse(content="done after sleeping", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=_SleepToolProvider())
    return TestClient(create_app(bus=bus, agent=agent))


def test_cancel_hard_interrupts_long_running_tool(bus: InProcessEventBus) -> None:
    """The core fix: Stop must interrupt a tool that's mid-execution —
    not wait for it to finish. A cooperative-only cancel would leave the
    15s sleep running; the hard task-cancel unwinds it near-instantly and
    emits ``turn_cancelled``."""
    client = _make_sleep_client(bus)
    with client.websocket_connect("/agent/v2/sess-hard-cancel") as ws:
        ws.receive_json()  # session_create
        ws.send_text(json.dumps({"type": "user", "content": "sleep please"}))

        # Wait until the tool has actually started (TOOL_INVOCATION_STARTED
        # or any event past the LLM response) so the cancel lands mid-tool.
        for _ in range(30):
            evt = ws.receive_json()
            if evt["type"] in (
                EventType.TOOL_INVOCATION_STARTED.value,
                EventType.TOOL_CALL_EMITTED.value,
            ):
                break

        t0 = time.monotonic()
        ws.send_text(json.dumps({"type": "cancel"}))

        saw_cancelled = False
        for _ in range(40):
            evt = ws.receive_json()
            if (
                evt["type"] == EventType.SESSION_LIFECYCLE.value
                and evt["payload"].get("phase") == "turn_cancelled"
            ):
                saw_cancelled = True
                break
        elapsed = time.monotonic() - t0
        assert saw_cancelled, "turn was not hard-cancelled (no turn_cancelled)"
        assert elapsed < _SLEEP_SECONDS - 3, (
            f"cancel took {elapsed:.1f}s — the tool was NOT interrupted, "
            f"the turn ran to completion (cooperative-only regression)"
        )


def test_ws_stays_alive_after_hard_cancel(bus: InProcessEventBus) -> None:
    """After a hard cancel (Stop) the WS loop must keep serving — a
    follow-up message gets a normal response, fast."""
    client = _make_sleep_client(bus)
    with client.websocket_connect("/agent/v2/sess-cancel-then-msg") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "sleep please"}))
        for _ in range(30):
            evt = ws.receive_json()
            if evt["type"] in (
                EventType.TOOL_INVOCATION_STARTED.value,
                EventType.TOOL_CALL_EMITTED.value,
            ):
                break
        # Stop the running turn explicitly, then wait for confirmation.
        ws.send_text(json.dumps({"type": "cancel"}))
        for _ in range(40):
            evt = ws.receive_json()
            if (
                evt["type"] == EventType.SESSION_LIFECYCLE.value
                and evt["payload"].get("phase") == "turn_cancelled"
            ):
                break
        # Now a fresh message must be served quickly.
        ws.send_text(json.dumps({"type": "user", "content": "hi again"}))
        saw_final = False
        t0 = time.monotonic()
        for _ in range(60):
            evt = ws.receive_json()
            if (
                evt["type"] == EventType.LLM_RESPONSE.value
                and not evt["payload"].get("tool_calls")
            ):
                saw_final = True
                break
        elapsed = time.monotonic() - t0
        assert saw_final, "WS did not serve a follow-up after Stop"
        assert elapsed < _SLEEP_SECONDS - 3, (
            f"follow-up waited {elapsed:.1f}s — WS loop wedged after Stop"
        )


# ── #1 Steering: a mid-turn message injects, doesn't abort ─────────


@dataclass
class _CapturingLLM(_ScriptedLLM):
    """Records the messages seen on each complete() call so a test can
    assert that a steering message got spliced into the running turn."""

    seen_user_texts: list[str] = field(default_factory=list)

    async def complete(self, messages, tools=None):  # noqa: ANN001
        for m in messages:
            if getattr(m, "role", None) == "user":
                self.seen_user_texts.append(getattr(m, "content", "") or "")
        return await super().complete(messages, tools=tools)


def test_steering_injects_into_running_turn() -> None:
    """Core #1: a message sent WHILE a turn runs is injected into that
    turn (seen at the next hop) and does NOT abort it — driven directly
    against AgentLoop so it's deterministic (no WS event-timing races)."""
    bus = InProcessEventBus()
    started = asyncio.Event()
    release = asyncio.Event()

    class _GatedTool(ToolProvider):
        """Blocks until the test releases it — a deterministic stand-in
        for a long-running tool, so the test can inject steering while
        the turn is provably mid-tool."""

        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(name="gate", description="wait",
                             parameters_schema={"type": "object", "properties": {}})]

        async def invoke(self, call: ToolCall) -> ToolResult:
            started.set()
            await release.wait()
            return ToolResult(call_id=call.id, ok=True, content="released")

    llm = _CapturingLLM(script=[
        LLMResponse(content="", tool_calls=(ToolCall(
            name="gate", args={}, provenance="anthropic", id="tc-g1",
        ),)),
        LLMResponse(content="done per steering", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=_GatedTool())

    async def _drive() -> object:
        turn = asyncio.create_task(agent.run_turn("sess-steer", "start"))
        await asyncio.wait_for(started.wait(), timeout=5.0)  # turn is mid-tool
        steered = agent.enqueue_steering("sess-steer", "ACTUALLY do X instead")
        release.set()  # let the tool finish → hop1 drains steering
        res = await asyncio.wait_for(turn, timeout=5.0)
        return steered, res

    steered, res = asyncio.run(_drive())
    assert steered is True, "enqueue_steering returned False for a live turn"
    assert res.ok is True
    assert any("ACTUALLY do X" in t for t in llm.seen_user_texts), (
        "steering text never reached the LLM — it was not injected"
    )


def test_steering_via_ws_echoes_and_does_not_spawn_second_turn(bus: InProcessEventBus) -> None:
    """WS boundary: a user frame mid-turn is echoed as channel='steering'
    and is NOT run as a separate turn (the reader injects it instead)."""
    llm = _CapturingLLM(script=[
        LLMResponse(content="", tool_calls=(ToolCall(
            name="sleep_long", args={}, provenance="anthropic", id="tc-s1",
        ),)),
        LLMResponse(content="done", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=_SleepToolProvider())
    client = TestClient(create_app(bus=bus, agent=agent))
    with client.websocket_connect("/agent/v2/sess-steer-ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "start"}))
        for _ in range(30):
            evt = ws.receive_json()
            if evt["type"] in (
                EventType.TOOL_INVOCATION_STARTED.value,
                EventType.TOOL_CALL_EMITTED.value,
            ):
                break
        ws.send_text(json.dumps({"type": "user", "content": "steer me"}))
        # The very next events must include the steering echo (published
        # immediately by the reader), well before the 15s tool finishes.
        saw_steer_echo = False
        for _ in range(10):
            evt = ws.receive_json()
            if (
                evt["type"] == EventType.USER_MESSAGE.value
                and evt["payload"].get("channel") == "steering"
            ):
                saw_steer_echo = True
                break
        assert saw_steer_echo, "steering frame was not echoed as channel=steering"
