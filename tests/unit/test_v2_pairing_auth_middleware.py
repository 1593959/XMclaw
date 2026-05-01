"""B-73: HTTP pairing-token auth middleware.

Covers four concerns:
  * Without a valid token, /api/v2/* HTTP routes return 401
  * With a valid token (?token= or Authorization: Bearer), the request
    passes through
  * Allowlisted paths (/health, /api/v2/pair) are reachable WITHOUT a
    token (otherwise the UI couldn't bootstrap)
  * When create_app is called without an auth_check (e.g. tests, or
    --no-auth daemon mode), the middleware is not installed at all
"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


# Test seam: a 1-arg auth_check that approves only "good-token".
async def _accept_only_good_token(presented: str | None) -> bool:
    return presented == "good-token"


@pytest.fixture
def app_with_auth():
    return create_app(config={}, auth_check=_accept_only_good_token)


@pytest.fixture
def app_no_auth():
    """No auth_check → middleware is not installed; all routes open."""
    return create_app(config={})


# ── 401 paths ──────────────────────────────────────────────────────────


def test_api_route_without_token_returns_401(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get("/api/v2/sessions")
    assert r.status_code == 401
    body = r.json()
    assert body.get("error") == "unauthorized"


def test_api_route_with_wrong_token_returns_401(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get("/api/v2/sessions?token=wrong")
    assert r.status_code == 401


def test_api_route_with_wrong_bearer_returns_401(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get(
            "/api/v2/sessions",
            headers={"Authorization": "Bearer wrong"},
        )
    assert r.status_code == 401


# ── happy paths ────────────────────────────────────────────────────────


def test_api_route_with_query_token_passes(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get("/api/v2/sessions?token=good-token")
    # The session router may return an empty list / 500-on-no-store on a
    # bare app; both are non-401, which is what we're asserting.
    assert r.status_code != 401


def test_api_route_with_bearer_token_passes(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get(
            "/api/v2/sessions",
            headers={"Authorization": "Bearer good-token"},
        )
    assert r.status_code != 401


# ── allowlist ──────────────────────────────────────────────────────────


def test_health_is_allowed_without_token(app_with_auth) -> None:
    with TestClient(app_with_auth) as client:
        r = client.get("/health")
    assert r.status_code == 200


def test_pair_endpoint_is_allowed_without_token(app_with_auth) -> None:
    """The Web UI calls /api/v2/pair to FETCH the token; if that endpoint
    required a token first the UI could never bootstrap."""
    with TestClient(app_with_auth) as client:
        r = client.get("/api/v2/pair")
    assert r.status_code == 200


# ── --no-auth mode ─────────────────────────────────────────────────────


def test_no_auth_mode_leaves_routes_open(app_no_auth) -> None:
    with TestClient(app_no_auth) as client:
        r = client.get("/api/v2/sessions")
    # Without auth_check the middleware is not installed; whatever the
    # router returns is fine — just must not be 401.
    assert r.status_code != 401
