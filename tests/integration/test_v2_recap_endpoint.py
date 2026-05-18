"""Recap endpoint — Wave-32+ (2026-05-18).

End-to-end TestClient test for ``GET /api/v2/session/{id}/recap``.
Covers:

  * No agent wired → 503
  * Empty history → 200 with ``recap: null``
  * Real history + stub LLM → 200 with the LLM's text in ``recap``

Per CLAUDE.md "tests must cross the front-back boundary" rule: this
hits the real router via TestClient against a real ``create_app``,
not a direct call to the handler.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import Message
from xmclaw.daemon.app import create_app


@dataclass
class _StubResp:
    content: str
    tool_calls: tuple = ()


class _StubLLM:
    def __init__(self, reply: str = "Building auth flow. Write tests next.") -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        return _StubResp(content=self.reply)

    def stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _StubAgent:
    """Minimal AgentLoop surface needed by the recap router: a
    ``_histories`` dict and an ``_llm`` attribute."""

    def __init__(self, histories=None, llm=None) -> None:
        self._histories = histories or {}
        self._llm = llm or _StubLLM()


def test_recap_returns_503_when_no_agent_wired() -> None:
    bus = InProcessEventBus()
    client = TestClient(create_app(bus=bus))
    resp = client.get("/api/v2/session/any/recap")
    assert resp.status_code == 503
    assert "no agent" in resp.json().get("error", "").lower()


def test_recap_empty_history_returns_null() -> None:
    bus = InProcessEventBus()
    agent = _StubAgent(histories={"sess-empty": []})
    client = TestClient(create_app(bus=bus, agent=agent))
    resp = client.get("/api/v2/session/sess-empty/recap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess-empty"
    assert body["recap"] is None
    assert body["messages_considered"] == 0


def test_recap_unknown_session_returns_null() -> None:
    """An unknown session_id isn't an error — the recap card just
    has nothing to show. Frontend treats null the same as empty."""
    bus = InProcessEventBus()
    agent = _StubAgent()
    client = TestClient(create_app(bus=bus, agent=agent))
    resp = client.get("/api/v2/session/never-seen/recap")
    assert resp.status_code == 200
    assert resp.json()["recap"] is None


def test_recap_with_history_returns_llm_text() -> None:
    bus = InProcessEventBus()
    llm = _StubLLM(reply="Working on login flow. Run pytest next.")
    agent = _StubAgent(
        histories={
            "sess-1": [
                Message(role="user", content="add login"),
                Message(role="assistant", content="started"),
            ],
        },
        llm=llm,
    )
    client = TestClient(create_app(bus=bus, agent=agent))
    resp = client.get("/api/v2/session/sess-1/recap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["recap"] == "Working on login flow. Run pytest next."
    assert body["messages_considered"] == 2
    assert llm.calls == 1


def test_recap_respects_window_query_param() -> None:
    """Operator-supplied ``window`` clamps how much tail goes to the
    LLM. Useful when a long session's recap quality degrades."""
    bus = InProcessEventBus()
    llm = _StubLLM(reply="ok")
    agent = _StubAgent(
        histories={
            "sess-big": [
                Message(role="user", content=f"msg {i}") for i in range(40)
            ],
        },
        llm=llm,
    )
    client = TestClient(create_app(bus=bus, agent=agent))
    resp = client.get("/api/v2/session/sess-big/recap?window=5")
    body = resp.json()
    assert body["messages_considered"] == 5


@pytest.mark.parametrize("bogus", ["abc", "-3", "0"])
def test_recap_invalid_window_falls_back_to_default(bogus: str) -> None:
    """Bad window values shouldn't 500 — fall back to the default
    instead so the recap card still renders."""
    bus = InProcessEventBus()
    agent = _StubAgent(
        histories={"sess-q": [Message(role="user", content="hi")]},
        llm=_StubLLM(reply="ok"),
    )
    client = TestClient(create_app(bus=bus, agent=agent))
    resp = client.get(f"/api/v2/session/sess-q/recap?window={bogus}")
    assert resp.status_code == 200
