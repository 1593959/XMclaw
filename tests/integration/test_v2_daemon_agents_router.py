"""Agents router + WS multi-agent routing — Epic #17 Phase 3.

Locks in the behavior Phase 3 adds without touching the primary:

  * ``/api/v2/agents`` CRUD (list / create / get / delete)
  * The primary config-built agent shows up as ``main`` in the list,
    is reserved, and can't be deleted through this surface
  * WS ``?agent_id=X`` routes to the named agent; missing ids close
    the socket with code 4404
  * WS default (no agent_id) uses the primary agent, unchanged
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.daemon.app import create_app


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch) -> Any:
    """Reroute ``agents_registry_dir()`` to a pytest tmp so one test's
    agent doesn't leak into the next via the user's real ~/.xmclaw."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(bus: InProcessEventBus, tmp_registry) -> TestClient:
    return TestClient(create_app(bus=bus))


@pytest.fixture
def llm_config() -> dict[str, Any]:
    return {
        "llm": {
            "anthropic": {
                "api_key": "sk-ant-test",
                "default_model": "claude-haiku-4-5",
            },
        },
    }


# ── list_agents ──────────────────────────────────────────────────────────


def test_list_is_empty_in_echo_mode(client: TestClient) -> None:
    # No config → no primary agent → list is empty. The manager is
    # wired but contains nothing until a POST arrives.
    resp = client.get("/api/v2/agents")
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


def test_list_includes_primary_when_config_wired(
    bus: InProcessEventBus, llm_config: dict[str, Any], tmp_registry
) -> None:
    with TestClient(create_app(bus=bus, config=llm_config)) as client:
        resp = client.get("/api/v2/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["agents"]) == 1
        primary = body["agents"][0]
        assert primary["agent_id"] == "main"
        assert primary["primary"] is True
        assert primary["ready"] is True


def test_list_includes_created_agents(
    client: TestClient, llm_config: dict[str, Any]
) -> None:
    client.post(
        "/api/v2/agents", json={"agent_id": "worker-1", "config": llm_config}
    )
    resp = client.get("/api/v2/agents")
    ids = [a["agent_id"] for a in resp.json()["agents"]]
    assert ids == ["worker-1"]


# ── create_agent ─────────────────────────────────────────────────────────


def test_create_agent_happy_path(
    client: TestClient, llm_config: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "qa", "config": llm_config}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"ok": True, "agent_id": "qa", "ready": True}


def test_create_without_llm_registers_but_not_ready(client: TestClient) -> None:
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "stub", "config": {}}
    )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True, "agent_id": "stub", "ready": False}


def test_create_rejects_missing_agent_id(client: TestClient) -> None:
    resp = client.post("/api/v2/agents", json={"config": {}})
    assert resp.status_code == 400
    assert "required" in resp.json()["error"]


def test_create_rejects_reserved_main(client: TestClient) -> None:
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "main", "config": {}}
    )
    assert resp.status_code == 400
    assert "reserved" in resp.json()["error"]


def test_create_rejects_unsafe_id(client: TestClient) -> None:
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "../escape", "config": {}}
    )
    assert resp.status_code == 400
    assert "A-Za-z" in resp.json()["error"]


def test_create_rejects_invalid_json(client: TestClient) -> None:
    resp = client.post(
        "/api/v2/agents",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_create_rejects_non_object_config(client: TestClient) -> None:
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "x", "config": ["list"]}
    )
    assert resp.status_code == 400
    assert "object" in resp.json()["error"]


def test_create_twice_conflicts(
    client: TestClient, llm_config: dict[str, Any]
) -> None:
    client.post(
        "/api/v2/agents", json={"agent_id": "dup", "config": llm_config}
    )
    resp = client.post(
        "/api/v2/agents", json={"agent_id": "dup", "config": llm_config}
    )
    assert resp.status_code == 409


# ── get_agent ────────────────────────────────────────────────────────────


def test_get_returns_404_for_unknown(client: TestClient) -> None:
    resp = client.get("/api/v2/agents/nope")
    assert resp.status_code == 404


def test_get_returns_primary_summary_when_config_wired(
    bus: InProcessEventBus, llm_config: dict[str, Any], tmp_registry
) -> None:
    with TestClient(create_app(bus=bus, config=llm_config)) as client:
        resp = client.get("/api/v2/agents/main")
        assert resp.status_code == 200
        assert resp.json() == {
            "agent_id": "main",
            "ready": True,
            "primary": True,
        }


def test_get_returns_created_agent(
    client: TestClient, llm_config: dict[str, Any]
) -> None:
    client.post(
        "/api/v2/agents", json={"agent_id": "inspect", "config": llm_config}
    )
    resp = client.get("/api/v2/agents/inspect")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "inspect"
    assert body["ready"] is True
    assert body["primary"] is False


# ── delete_agent ─────────────────────────────────────────────────────────


def test_delete_happy_path(
    client: TestClient, llm_config: dict[str, Any]
) -> None:
    client.post(
        "/api/v2/agents", json={"agent_id": "kill-me", "config": llm_config}
    )
    resp = client.delete("/api/v2/agents/kill-me")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert client.get("/api/v2/agents/kill-me").status_code == 404


def test_delete_missing_is_404(client: TestClient) -> None:
    resp = client.delete("/api/v2/agents/never")
    assert resp.status_code == 404


def test_delete_rejects_reserved_main(client: TestClient) -> None:
    resp = client.delete("/api/v2/agents/main")
    assert resp.status_code == 400
    assert "reserved" in resp.json()["error"]


# ── WS routing ───────────────────────────────────────────────────────────


class _StubAgent:
    """Stand-in AgentLoop that records which run_turn was called.

    We don't care about the actual LLM path in Phase 3 routing tests —
    only that the WS handler reaches the RIGHT agent when given an
    agent_id. Two stubs with distinct names let us verify the split.
    """

    def __init__(self, label: str, bus: InProcessEventBus) -> None:
        self._label = label
        self._bus = bus
        self.turns: list[tuple[str, str]] = []

    async def run_turn(self, session_id: str, content: str, **kwargs) -> None:
        self.turns.append((session_id, content))


def test_ws_without_agent_id_uses_primary(
    bus: InProcessEventBus, tmp_registry
) -> None:
    primary = _StubAgent("primary", bus)
    app = create_app(bus=bus, agent=primary)  # type: ignore[arg-type]
    with TestClient(app) as client, client.websocket_connect(
        "/agent/v2/sess1"
    ) as ws:
        ws.receive_json()  # session_create
        ws.send_text(json.dumps({"type": "user", "content": "hello"}))
    assert primary.turns == [("sess1", "hello")]


def test_ws_with_unknown_agent_id_closes_4404(
    bus: InProcessEventBus, tmp_registry
) -> None:
    app = create_app(bus=bus)
    with TestClient(app) as client:
        with pytest.raises(Exception) as exc_info:
            with client.websocket_connect(
                "/agent/v2/sess1?agent_id=missing"
            ) as ws:
                ws.receive_text()
        # starlette's WebSocketDisconnect carries the code.
        assert "4404" in str(exc_info.value) or getattr(
            exc_info.value, "code", None
        ) == 4404


def test_ws_with_registered_agent_id_routes_to_that_agent(
    bus: InProcessEventBus, llm_config: dict[str, Any], tmp_registry
) -> None:
    # Real flow: POST /api/v2/agents builds a real Workspace (with
    # a real AgentLoop). We don't invoke the LLM — a non-"user" frame
    # doesn't trigger run_turn. The only behavior we're verifying is
    # that the handshake succeeds, which it only does if the agent
    # was found in the manager.
    app = create_app(bus=bus)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v2/agents",
            json={"agent_id": "worker", "config": llm_config},
        )
        assert resp.status_code == 201
        with client.websocket_connect(
            "/agent/v2/sess1?agent_id=worker"
        ) as ws:
            event = ws.receive_json()
            assert event["type"] == EventType.SESSION_LIFECYCLE.value
            assert event["payload"]["phase"] == "create"


def test_ws_agent_id_main_resolves_to_primary(
    bus: InProcessEventBus, tmp_registry
) -> None:
    primary = _StubAgent("primary", bus)
    app = create_app(bus=bus, agent=primary)  # type: ignore[arg-type]
    with TestClient(app) as client, client.websocket_connect(
        "/agent/v2/sess1?agent_id=main"
    ) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "user", "content": "ping"}))
    assert primary.turns == [("sess1", "ping")]


def test_primary_and_registered_do_not_cross_contaminate(
    bus: InProcessEventBus, llm_config: dict[str, Any], tmp_registry
) -> None:
    primary = _StubAgent("primary", bus)
    app = create_app(bus=bus, agent=primary)  # type: ignore[arg-type]
    with TestClient(app) as client:
        client.post(
            "/api/v2/agents",
            json={"agent_id": "side", "config": llm_config},
        )
        # Main flow routes to primary.
        with client.websocket_connect("/agent/v2/p") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({"type": "user", "content": "a"}))
        # Side flow routes to the registered agent — primary MUST
        # NOT see this turn.
        with client.websocket_connect("/agent/v2/s?agent_id=side") as ws:
            ws.receive_json()
    assert primary.turns == [("p", "a")]
