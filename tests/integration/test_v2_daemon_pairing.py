"""Daemon with pairing auth — WS integration tests (anti-req #8).

Verifies the server-side gate: with an ``auth_check`` wired, a WS
connection without the correct token is refused (close code 4401);
with the right token, it completes normally.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from xmclaw.daemon.app import create_app
from xmclaw.daemon.pairing import validate_token


def _make_auth(expected: str):
    async def _auth(presented: str | None) -> bool:
        return validate_token(expected, presented)
    return _auth


TOKEN = "deadbeef" * 8  # 64 hex chars, predictable for tests


# ── accept path ──────────────────────────────────────────────────────────


def test_valid_token_via_query_param_accepted() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    with client.websocket_connect(
        f"/agent/v2/sess?token={TOKEN}"
    ) as ws:
        # First frame is the session_create event — connection is live.
        frame = ws.receive_json()
        assert frame["type"] == "session_lifecycle"


def test_valid_token_via_authorization_header_accepted() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    with client.websocket_connect(
        "/agent/v2/sess",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "session_lifecycle"


# ── reject paths ─────────────────────────────────────────────────────────


def test_missing_token_rejected_with_4401() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/agent/v2/sess") as ws:
            ws.receive_json()  # should disconnect before any frame
    assert exc_info.value.code == 4401


def test_wrong_token_rejected_with_4401() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/agent/v2/sess?token=not-the-right-one",
        ) as ws:
            ws.receive_json()
    assert exc_info.value.code == 4401


def test_empty_token_rejected() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent/v2/sess?token=") as ws:
            ws.receive_json()


def test_malformed_authorization_header_rejected() -> None:
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    # Header exists but no "Bearer" prefix — ignore, no token found → reject.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/agent/v2/sess",
            headers={"Authorization": "Basic junk"},
        ) as ws:
            ws.receive_json()


def test_query_param_wins_over_header_when_both_present() -> None:
    """If the query param is set, header is ignored — simplifies the
    client story (use one path, don't mix)."""
    app = create_app(auth_check=_make_auth(TOKEN))
    client = TestClient(app)
    # Query param is the right token; header is garbage.
    with client.websocket_connect(
        f"/agent/v2/sess?token={TOKEN}",
        headers={"Authorization": "Bearer wrong"},
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "session_lifecycle"


# ── no-auth mode (explicit caller opts out) ─────────────────────────────


def test_no_auth_check_accepts_anything() -> None:
    """When auth_check is None, the old echo-mode semantics apply —
    anyone can connect. Required for tests that predate Phase 4.4."""
    app = create_app()  # auth_check=None by default
    client = TestClient(app)
    with client.websocket_connect("/agent/v2/sess") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "session_lifecycle"


# ── auth_check crash guard ──────────────────────────────────────────────


def test_auth_check_exception_treated_as_deny() -> None:
    """A buggy auth_check must not grant access — it denies, closing 4401."""
    async def crashing_auth(presented: str | None) -> bool:
        raise RuntimeError("auth logic exploded")

    app = create_app(auth_check=crashing_auth)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/agent/v2/sess?token={TOKEN}") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4401
