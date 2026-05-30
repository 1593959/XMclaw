"""Tests for the OpenAI-compatible /v1 endpoints (P2-1).

Two layers (per the front-back boundary rule):

  1. Pure-function tests on the message-splitting / content-flattening
     helpers — fast, no app.
  2. End-to-end TestClient hitting the REAL ``/v1/chat/completions``
     and ``/v1/models`` URLs an OpenAI SDK would call, with a mocked
     AgentLoop on ``app.state.agent``. Verifies the contract shape
     the SDK expects (id / object / choices[0].message.content /
     finish_reason).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.openai_compat import (
    _content_to_text,
    _Message,
    _split_messages,
)


# ─── Layer 1: helper unit tests ───────────────────────────────────


def test_content_to_text_plain_string():
    assert _content_to_text("hello") == "hello"


def test_content_to_text_none_is_empty():
    assert _content_to_text(None) == ""


def test_content_to_text_multimodal_parts():
    parts = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]
    out = _content_to_text(parts)
    assert "describe this" in out
    assert "[image: http://x/y.png]" in out


def test_split_messages_extracts_system_and_last_user():
    msgs = [
        _Message(role="system", content="you are helpful"),
        _Message(role="user", content="hi"),
        _Message(role="assistant", content="hello"),
        _Message(role="user", content="how are you"),
    ]
    system, history, final_user = _split_messages(msgs)
    assert system == "you are helpful"
    assert final_user.content == "how are you"
    # History is everything between system and the last user message.
    assert [h["content"] for h in history] == ["hi", "hello"]


def test_split_messages_no_system_prompt():
    msgs = [_Message(role="user", content="hi")]
    system, history, final_user = _split_messages(msgs)
    assert system is None
    assert history == []
    assert final_user.content == "hi"


def test_split_messages_empty_array_raises_400():
    with pytest.raises(HTTPException) as exc:
        _split_messages([])
    assert exc.value.status_code == 400


def test_split_messages_no_user_message_raises_400():
    msgs = [_Message(role="system", content="x")]
    with pytest.raises(HTTPException) as exc:
        _split_messages(msgs)
    assert exc.value.status_code == 400


def test_split_messages_tolerates_trailing_assistant():
    """Some clients append an empty assistant prefix. The last USER
    message is still the question to answer."""
    msgs = [
        _Message(role="user", content="real question"),
        _Message(role="assistant", content=""),
    ]
    _system, _history, final_user = _split_messages(msgs)
    assert final_user.content == "real question"


# ─── Layer 2: end-to-end TestClient ───────────────────────────────


@pytest.fixture
def client_with_agent() -> TestClient:
    """Real app with a mocked AgentLoop on app.state.agent."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})

    fake_agent = MagicMock()
    fake_agent._histories = {}

    async def _fake_run_turn(*, session_id, user_message, **kwargs):
        from xmclaw.daemon.turn_types import AgentTurnResult
        return AgentTurnResult(
            ok=True,
            text=f"echo: {user_message}",
            hops=1,
        )

    fake_agent.run_turn = AsyncMock(side_effect=_fake_run_turn)
    app.state.agent = fake_agent

    # llm_registry with two profile ids for the /models test.
    fake_registry = MagicMock()
    fake_registry.ids = MagicMock(return_value=["fast", "strong"])
    fake_registry.__contains__ = MagicMock(side_effect=lambda x: x in ("fast", "strong"))
    app.state.llm_registry = fake_registry

    return TestClient(app)


def test_chat_completions_returns_openai_shape(client_with_agent: TestClient):
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "fast",
        "messages": [{"role": "user", "content": "hello there"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # OpenAI contract shape.
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "fast"
    assert isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "echo: hello there"
    assert choice["finish_reason"] == "stop"
    assert "usage" in body


def test_chat_completions_loads_history_into_session(
    client_with_agent: TestClient,
):
    """Multi-turn client re-sends the whole history; the prior
    messages must populate the agent's session before the last user
    message runs."""
    app_agent = client_with_agent.app.state.agent
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "fast",
        "session_id": "sess-1",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "second"},
        ],
    })
    assert r.status_code == 200
    # The agent's in-memory history for sess-1 got pre-populated with
    # the two leading messages (first + answer one).
    hist = app_agent._histories.get("sess-1")
    assert hist is not None
    assert len(hist) == 2
    assert hist[0].content == "first"
    assert hist[1].content == "answer one"
    # The final user message "second" is what got run.
    call_kwargs = app_agent.run_turn.call_args.kwargs
    assert call_kwargs["user_message"] == "second"


def test_chat_completions_empty_messages_returns_400(
    client_with_agent: TestClient,
):
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "fast",
        "messages": [],
    })
    assert r.status_code == 400


def test_chat_completions_no_user_message_returns_400(
    client_with_agent: TestClient,
):
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "fast",
        "messages": [{"role": "system", "content": "x"}],
    })
    assert r.status_code == 400


def test_chat_completions_unknown_model_falls_through_to_default(
    client_with_agent: TestClient,
):
    """An unrecognized model name shouldn't 404 — the OpenAI spec is
    forgiving, and a client that picked 'gpt-4' should still get an
    answer from the registry default."""
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "gpt-4-turbo",  # not a configured profile id
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    # run_turn was called with profile_id=None (default routing).
    call_kwargs = client_with_agent.app.state.agent.run_turn.call_args.kwargs
    assert call_kwargs["llm_profile_id"] is None


def test_chat_completions_known_model_pins_profile(
    client_with_agent: TestClient,
):
    r = client_with_agent.post("/v1/chat/completions", json={
        "model": "strong",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    call_kwargs = client_with_agent.app.state.agent.run_turn.call_args.kwargs
    assert call_kwargs["llm_profile_id"] == "strong"


def test_models_lists_configured_profiles(client_with_agent: TestClient):
    r = client_with_agent.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    assert ids == {"fast", "strong"}
    for m in body["data"]:
        assert m["object"] == "model"
        assert m["owned_by"] == "xmclaw"


def test_models_empty_registry_falls_back_to_default():
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    app.state.llm_registry = None
    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert ids == {"default"}


def test_chat_completions_agent_not_ready_returns_503():
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    app.state.agent = None
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "x",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 503


# ─── Router registration (front-back boundary inspection) ─────────


def test_v1_routes_registered():
    from xmclaw.daemon.routers.openai_compat import router
    paths = {r.path for r in router.routes}
    assert "/v1/chat/completions" in paths
    assert "/v1/models" in paths
