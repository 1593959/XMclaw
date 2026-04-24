"""v2 daemon app — HTTP + WebSocket integration smoke.

Uses FastAPI's ``TestClient`` so this runs without starting uvicorn.
Verifies:
  * /health responds with ok + version
  * WS accepts a user frame, publishes it on the bus, and forwards
    any resulting session-scoped events back to the client
  * Sessions are isolated: events for session A don't leak to session B
  * Disconnect triggers a session_lifecycle(destroy) event
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.app import create_app


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def client(bus: InProcessEventBus) -> TestClient:
    return TestClient(create_app(bus=bus))


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["bus"] == "InProcessEventBus"


def test_ws_connect_emits_session_create(client: TestClient) -> None:
    with client.websocket_connect("/agent/v2/sess-alpha") as ws:
        frame = ws.receive_json()
        assert frame["type"] == EventType.SESSION_LIFECYCLE.value
        assert frame["session_id"] == "sess-alpha"
        assert frame["payload"]["phase"] == "create"


def test_ws_user_message_echoed_via_bus(client: TestClient) -> None:
    with client.websocket_connect("/agent/v2/sess-beta") as ws:
        # Skip the session_create event.
        ws.receive_json()

        ws.send_text(json.dumps({"type": "user", "content": "hi daemon"}))
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "hi daemon"
        assert event["payload"]["channel"] == "ws"


def test_ws_malformed_frame_dropped_but_connection_survives(
    client: TestClient,
) -> None:
    with client.websocket_connect("/agent/v2/sess-mal") as ws:
        ws.receive_json()  # session create

        ws.send_text("{not json")     # bad frame — dropped silently
        ws.send_text(json.dumps({"type": "user", "content": "ok"}))
        # Connection must still work for the follow-up valid frame.
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "ok"


def test_sessions_are_isolated(client: TestClient) -> None:
    """A user message in session A must not appear on session B's socket."""
    with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
         client.websocket_connect("/agent/v2/sess-B") as ws_b:
        # Drain each session's own create event.
        ws_a.receive_json()
        ws_b.receive_json()

        ws_a.send_text(json.dumps({"type": "user", "content": "for A"}))
        evt = ws_a.receive_json()
        assert evt["session_id"] == "sess-A"
        assert evt["payload"]["content"] == "for A"

        # B has no event waiting. Use a short iter_text-style probe:
        # receive_json with timeout not directly supported in TestClient,
        # so we send a follow-up to B and verify A's message did not
        # leak into B's event order.
        ws_b.send_text(json.dumps({"type": "user", "content": "for B"}))
        evt_b = ws_b.receive_json()
        assert evt_b["session_id"] == "sess-B"
        assert evt_b["payload"]["content"] == "for B"


def test_session_destroy_on_disconnect(
    client: TestClient, bus: InProcessEventBus,
) -> None:
    """Disconnect publishes a session_lifecycle(destroy) event on the bus."""
    received: list = []

    async def collect(ev) -> None:  # noqa: ANN001
        received.append(ev)

    bus.subscribe(
        lambda e: (
            e.type == EventType.SESSION_LIFECYCLE
            and e.session_id == "sess-bye"
            and e.payload.get("phase") == "destroy"
        ),
        collect,
    )

    with client.websocket_connect("/agent/v2/sess-bye") as ws:
        ws.receive_json()  # create
    # Connection closed — daemon should now have published destroy.
    # TestClient runs handlers on the same thread, so by here the
    # event has been emitted.
    assert len(received) == 1


def test_unknown_frame_type_is_ignored_for_phase_40(
    client: TestClient,
) -> None:
    """Phase 4.0 only handles 'user' frames. Other frame types are
    silently ignored (no crash, no spurious event). Phase 4.1 wires
    more frame types to scheduler calls."""
    with client.websocket_connect("/agent/v2/sess-ignore") as ws:
        ws.receive_json()  # create
        ws.send_text(json.dumps({"type": "cancel", "id": "x"}))
        # Follow up with a real user frame — should be the next event.
        ws.send_text(json.dumps({"type": "user", "content": "ok"}))
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "ok"


def test_skill_promoted_broadcasts_across_sessions(
    client: TestClient, bus: InProcessEventBus,
) -> None:
    """Epic #4 REPL flash: evolution events must reach every REPL.

    The orchestrator emits ``SKILL_PROMOTED`` with ``session_id="_system"``
    (or the evolution fiber's own id). A naive per-session WS filter would
    swallow those events — nobody's REPL would flash. This test verifies
    both sockets see the promotion regardless of which session triggered it.

    ``with client:`` makes TestClient hold a single shared portal across
    the two websocket_connect blocks — needed so that ``bus.publish``
    called via ``client.portal`` runs on the same loop where the two
    WS handlers subscribed their ``forward`` coroutines.
    """
    with client:
        with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
             client.websocket_connect("/agent/v2/sess-B") as ws_b:
            ws_a.receive_json()  # each session's own create
            ws_b.receive_json()

            # Publish a promotion event on a totally unrelated session id
            # — mimics the orchestrator emitting on "_system".
            async def _pub() -> None:
                await bus.publish(make_event(
                    session_id="_system", agent_id="orchestrator",
                    type=EventType.SKILL_PROMOTED,
                    payload={
                        "skill_id": "email_digest",
                        "from_version": 3,
                        "to_version": 4,
                        "evidence": ["plays=12"],
                    },
                ))
                await bus.drain()
            client.portal.call(_pub)

            evt_a = ws_a.receive_json()
            evt_b = ws_b.receive_json()
            assert evt_a["type"] == EventType.SKILL_PROMOTED.value
            assert evt_b["type"] == EventType.SKILL_PROMOTED.value
            assert evt_a["payload"]["skill_id"] == "email_digest"
            assert evt_b["payload"]["skill_id"] == "email_digest"
            # Event keeps its original session_id — clients format it,
            # they don't rely on it matching their own session.
            assert evt_a["session_id"] == "_system"
            assert evt_b["session_id"] == "_system"
