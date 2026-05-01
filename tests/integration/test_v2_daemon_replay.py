"""Daemon WS reconnect -- event replay + ultrathink flag.

The user's complaint was: "刷新后前端不显示上下文" (after refresh the chat
is empty even though the server has the history). Fix: the daemon keeps
a per-session event log and streams it back to every new WS connection,
bracketed by ``session_replay`` marker frames, with each replayed frame
tagged ``replayed: true`` so the UI can suppress thinking-spinner /
token-double-count behaviors.

Separately: ``ultrathink`` flag in a user frame prepends a
step-by-step directive before the content hits the agent loop.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.app import create_app
from xmclaw.providers.llm.base import (
    LLMChunk, LLMProvider, LLMResponse, Message, Pricing,
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


def _drain_until_llm_response(ws, max_events: int = 20) -> list[dict]:
    events = []
    for _ in range(max_events):
        events.append(ws.receive_json())
        if events[-1]["type"] == EventType.LLM_RESPONSE.value:
            return events
    return events


# ── event replay on reconnect ────────────────────────────────────────────


def test_reconnect_replays_prior_events_with_replay_flag() -> None:
    """New WS connection to an existing session_id receives a
    session_replay marker -> all prior events -> session_replay end,
    with every replayed frame tagged replayed=true."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[
        LLMResponse(content="first turn reply"),
        LLMResponse(content="second turn reply"),
    ])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    # Turn 1 on connection A.
    with client.websocket_connect("/agent/v2/replay-sess") as ws:
        ws.receive_json()  # session_lifecycle: create
        ws.send_text(json.dumps({"type": "user", "content": "first user msg"}))
        _drain_until_llm_response(ws)

    # Connection B on same session_id -- should replay.
    with client.websocket_connect("/agent/v2/replay-sess") as ws:
        # The very first frame should be the replay-start marker.
        start = ws.receive_json()
        assert start["type"] == "session_replay"
        assert start["payload"]["phase"] == "start"
        assert start["payload"]["count"] > 0
        assert start.get("replayed") is True

        # Collect frames until we see the end marker.
        replayed = []
        for _ in range(50):
            frame = ws.receive_json()
            if frame["type"] == "session_replay" and frame["payload"].get("phase") == "end":
                break
            replayed.append(frame)
        # All replayed frames carry replayed=true.
        assert all(f.get("replayed") is True for f in replayed), (
            f"some replayed frame missing replayed=true: {replayed}"
        )
        # Prior user message + llm events must be present.
        types = {f["type"] for f in replayed}
        assert EventType.USER_MESSAGE.value in types
        assert EventType.LLM_REQUEST.value in types
        assert EventType.LLM_RESPONSE.value in types


def test_reconnect_no_prior_events_skips_replay_markers() -> None:
    """A brand-new session_id has no log; the WS should NOT send
    session_replay markers -- it just starts live."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[LLMResponse(content="hi")])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/brand-new-sess") as ws:
        first = ws.receive_json()
        # First live frame should be session_lifecycle:create, NOT a
        # replay marker.
        assert first["type"] == EventType.SESSION_LIFECYCLE.value
        assert first["payload"]["phase"] == "create"


def test_session_log_is_bounded() -> None:
    """The log cap is 400 events per session; after that, oldest drop."""
    from xmclaw.daemon.app import create_app  # reimport to be explicit

    bus = InProcessEventBus()
    # Build app to get the session_logs dict on app.state.
    app = create_app(bus=bus)
    logs = app.state.session_logs

    # Directly publish 450 events for one session.
    from xmclaw.core.bus import make_event
    import asyncio
    async def _pump():
        for i in range(450):
            await bus.publish(make_event(
                session_id="sess-cap", agent_id="test",
                type=EventType.USER_MESSAGE,
                payload={"seq": i},
            ))
        await bus.drain()
    asyncio.run(_pump())

    assert len(logs["sess-cap"]) <= 400


# ── ultrathink flag ──────────────────────────────────────────────────────


def test_ultrathink_flag_prepends_thinking_directive() -> None:
    """When the user frame has ultrathink=true, the message the LLM sees
    is wrapped with a 'think step-by-step' directive."""
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[LLMResponse(content="k")])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/ut-sess") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({
            "type": "user", "content": "what is 2+2?",
            "ultrathink": True,
        }))
        _drain_until_llm_response(ws)

    last = llm.seen_messages[-1]
    user_text = " ".join(m.content or "" for m in last if m.role == "user")
    assert "step-by-step" in user_text
    assert "what is 2+2" in user_text


def test_ultrathink_off_by_default_leaves_message_untouched() -> None:
    bus = InProcessEventBus()
    llm = _RecordingLLM(script=[LLMResponse(content="k")])
    agent = AgentLoop(llm=llm, bus=bus)
    client = TestClient(create_app(bus=bus, agent=agent))

    with client.websocket_connect("/agent/v2/ut-off") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "plain question"}))
        _drain_until_llm_response(ws)

    last = llm.seen_messages[-1]
    user_text = " ".join(m.content or "" for m in last if m.role == "user")
    assert "step-by-step" not in user_text
    assert user_text.strip() == "plain question"


# ── sanitized config endpoint ────────────────────────────────────────────


def test_config_endpoint_redacts_api_keys() -> None:
    app = create_app(config={
        "llm": {
            "anthropic": {"api_key": "sk-verysecretkey123456", "default_model": "claude"},
        },
        "tools": {"allowed_dirs": ["/safe"]},
    })
    client = TestClient(app)
    r = client.get("/api/v2/config")
    assert r.status_code == 200
    body = r.json()
    k = body["config"]["llm"]["anthropic"]["api_key"]
    assert k.startswith("<redacted"), f"api_key not redacted: {k}"
    assert "3456" in k or "456" in k  # tail hint for the user to verify
    # Non-secret fields pass through.
    assert body["config"]["llm"]["anthropic"]["default_model"] == "claude"
    assert body["config"]["tools"]["allowed_dirs"] == ["/safe"]


def test_status_endpoint_reports_model_and_tools() -> None:
    app = create_app(config={
        "llm": {"anthropic": {"api_key": "k", "default_model": "m1"}},
    })
    client = TestClient(app)
    r = client.get("/api/v2/status")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_wired"] is True
    assert body["model"] == "m1"
    assert "file_read" in body["tools"]
    assert "todo_write" in body["tools"]


def test_status_without_agent_reports_echo_mode() -> None:
    app = create_app()  # no config, no agent
    client = TestClient(app)
    r = client.get("/api/v2/status")
    body = r.json()
    assert body["agent_wired"] is False
    assert body["tools"] == []
