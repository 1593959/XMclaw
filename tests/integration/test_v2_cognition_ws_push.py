"""End-to-end test for /api/v2/cognition/ws (Audit pass-3 finding C2).

Per the standing rule (CLAUDE.md → tests for cross-cutting features
must exercise the full HTTP path the frontend uses), the cognition
WS push is the BIGGEST UI-vs-backend coupling — it pushes the live
``CognitiveState`` every 2 s and ``pages/Cognition.js`` rewrites
``cogState`` from each frame. Without this test a WS schema drift
silently freezes the dashboard (no console error, no failed test —
the polling fallback masks the regression).

Cases (mirroring docs/AUDIT_PASS_3_FINDINGS.md C2):

1. WS connects + receives initial frame within ~3 s.
2. Frame shape matches the GET /state response (same top-level keys
   the frontend reads: ``goals``, ``attention_focus``, ``fatigue``,
   ``salience_threshold``, ``attention_capacity``).
3. Subsequent frames keep arriving on the heartbeat (>=2 frames in 5 s).
4. WS gracefully handles ``cognitive_state=None`` — frames carry an
   ``error`` key instead of crashing the connection.
5. Disconnect on the client side closes cleanly so a second connect
   to the same app boots without "WebSocket already accepted" leaks.
6. Frame's ``goals`` items match the ``Goal`` dataclass shape
   (``id`` / ``description`` / ``priority`` / ``source`` / ``status``).
7. Frame's ``attention_focus`` items have ``salience_score`` rounded
   to 3 decimals (matches GET /state behavior).
8. Auth contract: the WS handler currently accepts unauthenticated
   connections (no 4401). This test pins that contract so future
   auth tightening is a deliberate, visible change rather than a
   silent dashboard-killer.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.cognition.state import AttentionFocus, CognitiveState, Goal
from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


def _fake_state(
    *,
    goals: list[Goal] | None = None,
    focus: list[AttentionFocus] | None = None,
    fatigue: dict[str, float] | None = None,
    salience_threshold: float = 0.3,
    attention_capacity: int = 7,
) -> CognitiveState:
    """Build a real CognitiveState — the WS handler does ``getattr``
    against attribute names, so a dataclass passes through unchanged
    while still exercising the real serialisation path. Mocking with
    MagicMock here would mask attribute-rename regressions."""
    cs = CognitiveState(
        current_goals=list(goals or []),
        attention_focus=list(focus or []),
        fatigue=dict(fatigue or {}),
        attention_capacity=attention_capacity,
        salience_threshold=salience_threshold,
    )
    return cs


@pytest.fixture
def cs_with_data() -> CognitiveState:
    return _fake_state(
        goals=[
            Goal(
                id="g1",
                description="ship Audit batch 5",
                priority=8,
                source="user",
                status="active",
            ),
        ],
        focus=[
            # An ugly float to verify the 3-decimal rounding in case 7.
            AttentionFocus(
                percept_id="p1",
                content="incoming WS frame",
                salience_score=0.123456789,
            ),
        ],
        fatigue={"p0": 0.4321},
    )


@pytest.fixture
def client(cs_with_data: CognitiveState) -> TestClient:
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    app.state.cognitive_state = cs_with_data
    return TestClient(app)


# ── Cases 1-3: liveness, shape, heartbeat ──────────────────────────


def test_ws_connects_and_first_frame_arrives_quickly(
    client: TestClient,
) -> None:
    """Case 1: WS accepts the connection and pushes the initial frame
    on tick zero of the loop (no client-side prompt needed)."""
    t0 = time.monotonic()
    with client.websocket_connect("/api/v2/cognition/ws") as ws:
        frame = ws.receive_json()
    elapsed = time.monotonic() - t0
    assert isinstance(frame, dict)
    assert "goals" in frame
    # First frame is sent before the first asyncio.sleep, so it should
    # arrive almost immediately (<3 s of headroom for slow CI).
    assert elapsed < 3.0, f"first frame took {elapsed:.2f}s (>3s budget)"


def test_ws_frame_shape_matches_get_state(
    client: TestClient,
) -> None:
    """Case 2: keys must exactly match the frontend parser, which is
    written once for both REST and WS paths (see Cognition.js)."""
    expected_keys = {
        "goals",
        "attention_focus",
        "fatigue",
        "salience_threshold",
        "attention_capacity",
    }
    with client.websocket_connect("/api/v2/cognition/ws") as ws:
        frame = ws.receive_json()
    missing = expected_keys - set(frame.keys())
    assert not missing, (
        f"WS frame missing keys {missing} — Cognition.js relies on "
        f"these to populate cards. Got keys: {sorted(frame.keys())}"
    )


def test_ws_pushes_multiple_frames_on_heartbeat(
    client: TestClient,
) -> None:
    """Case 3: the loop ticks every PUSH_INTERVAL_S=2.0s. We must
    receive at least 2 frames in a 5 s window — proves the loop body
    is genuinely repeating, not a one-shot."""
    frames: list[dict] = []
    t_deadline = time.monotonic() + 5.0
    with client.websocket_connect("/api/v2/cognition/ws") as ws:
        while time.monotonic() < t_deadline and len(frames) < 3:
            frames.append(ws.receive_json())
    assert len(frames) >= 2, (
        f"only received {len(frames)} frame(s) in 5 s — heartbeat "
        f"loop appears broken (PUSH_INTERVAL_S regression?)"
    )


# ── Case 4: graceful degradation when cognition not wired ──────────


def test_ws_emits_error_payload_when_cognitive_state_missing() -> None:
    """Case 4: when neither ``app.state.cognitive_state`` nor
    ``app.state.agent._cognitive_state`` is set, the handler's
    ``_cognitive_state`` helper returns None and the WS must emit a
    JSON ``{"error": ...}`` frame rather than crashing or sending a
    zero-valued payload that the UI would render as "everything's
    fine, just empty"."""
    bus = InProcessEventBus()
    app = create_app(bus=bus)
    with TestClient(app) as tc:
        # Force both fallback paths in routers/cognition._cognitive_state
        # to None: the shared shortcut AND the per-agent attribute. The
        # default lifespan wires an agent that exposes an
        # ``_cognitive_state`` so we have to clear both to reach the
        # "not wired" branch the UI cares about.
        app.state.cognitive_state = None
        agent = getattr(app.state, "agent", None)
        if agent is not None and hasattr(agent, "_cognitive_state"):
            agent._cognitive_state = None
        with tc.websocket_connect("/api/v2/cognition/ws") as ws:
            frame = ws.receive_json()
    assert "error" in frame, (
        f"missing-cognition path must emit an error frame so the UI "
        f"can fall back to its 5 s polling — got {frame!r}"
    )


# ── Case 5: clean disconnect / reconnect ───────────────────────────


def test_ws_clean_disconnect_allows_immediate_reconnect(
    client: TestClient,
) -> None:
    """Case 5: closing the WS on the client side must let the handler
    exit cleanly (the WebSocketDisconnect branch). A subsequent
    connect on the SAME app must succeed — no "WebSocket already
    accepted" or stuck state from the prior session."""
    with client.websocket_connect("/api/v2/cognition/ws") as ws1:
        ws1.receive_json()
    # Reconnect inside the SAME TestClient app instance.
    with client.websocket_connect("/api/v2/cognition/ws") as ws2:
        frame2 = ws2.receive_json()
    assert "goals" in frame2, (
        "second connect did not receive a normal frame — handler "
        "may be leaking task / accept state across disconnects"
    )


# ── Case 6: goal item shape ────────────────────────────────────────


def test_ws_goal_items_match_dataclass_shape(
    client: TestClient,
) -> None:
    """Case 6: every goal in the frame must have the 5 keys that
    Cognition.js reads (``id``, ``description``, ``priority``,
    ``source``, ``status``). Missing any of these = silent UI
    breakage (e.g. status badge renders as ``undefined``)."""
    with client.websocket_connect("/api/v2/cognition/ws") as ws:
        frame = ws.receive_json()
    assert frame["goals"], "test fixture should produce >=1 goal"
    g = frame["goals"][0]
    for key in ("id", "description", "priority", "source", "status"):
        assert key in g, f"goal missing key {key!r}: {g!r}"
    assert g["id"] == "g1"
    assert g["priority"] == 8
    assert g["status"] == "active"


# ── Case 7: salience rounding parity with GET /state ───────────────


def test_ws_salience_score_rounded_to_three_decimals(
    client: TestClient,
) -> None:
    """Case 7: GET /state rounds ``salience_score`` to 3 decimals
    (see routers/cognition.py line ~127). The WS frame MUST round
    identically — otherwise the same percept renders with two
    different precisions depending on whether the page is mid-WS-
    handshake or polling, which is exactly the kind of subtle UI
    flicker the audit flagged."""
    with client.websocket_connect("/api/v2/cognition/ws") as ws:
        frame = ws.receive_json()
    assert frame["attention_focus"], "fixture should produce >=1 focus"
    score = frame["attention_focus"][0]["salience_score"]
    # Original value 0.123456789 → rounded to 0.123. Compare via the
    # round() identity rather than equality on a float literal so a
    # banker-rounding flip would still surface clearly.
    assert score == round(0.123456789, 3), (
        f"salience_score not rounded to 3 decimals: got {score!r}"
    )
    # And: more than 3 decimals would push the str repr past 5 chars.
    assert len(str(score).split(".")[-1]) <= 3


# ── Case 8: auth contract ──────────────────────────────────────────


def test_ws_does_not_enforce_auth_token() -> None:
    """Case 8: the cognition WS handler currently accepts anonymous
    connections — there's no token check in cognition_ws. The
    docstring mentions a "standard pairing-token query param" but
    the handler does NOT close with 4401 when it's missing.

    This test pins that contract: future tightening (adding a real
    auth check) WILL fail this test, forcing whoever does it to
    audit the dashboard's ``new WebSocket(...)`` call (which only
    appends ``?token=`` when the React tree was given a token via
    props) and update both sides in lockstep.
    """
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    app.state.cognitive_state = _fake_state()
    with TestClient(app) as tc:
        # No ?token=… on the URL. Today this MUST connect.
        with tc.websocket_connect("/api/v2/cognition/ws") as ws:
            frame = ws.receive_json()
    assert isinstance(frame, dict), (
        "anonymous connect was rejected — if you intentionally added "
        "auth to /api/v2/cognition/ws, also update Cognition.js to "
        "always send the token + update this test."
    )
