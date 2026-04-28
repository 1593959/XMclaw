"""B-75: request body size cap on /api/v2/* (OOM defence).

Pins three behaviours:
  * Oversized POST returns 413 (rejected before parse).
  * Reasonable-size POST passes through.
  * Non-/api/v2 routes are not size-capped (uploads, static, etc).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


@pytest.fixture
def app():
    # No auth_check → keeps middleware order minimal; we're testing the
    # body-size middleware in isolation, not its interaction with auth.
    return create_app(config={})


def test_oversized_post_to_api_returns_413(app) -> None:
    # 11 MB of "a" — over the 10 MB default cap.
    big_body = "a" * (11 * 1024 * 1024)
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/some-note",
            json={"content": big_body},
        )
    assert r.status_code == 413
    assert "too large" in (r.json().get("error") or "")


def test_normal_post_to_api_passes_through(app) -> None:
    # 1 KB body — well under the cap.
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/memory/some-note",
            json={"content": "hello world"},
        )
    # Whatever the router returns (200 / 4xx / 5xx) is fine — what matters
    # is that we did NOT short-circuit with 413.
    assert r.status_code != 413


def test_oversized_post_outside_api_is_not_capped(app) -> None:
    """Non-/api/v2/ routes (UI assets, /health) bypass the cap.

    /health doesn't accept POST so it'll return 405; what matters is
    that the body-size middleware did NOT pre-empt with 413.
    """
    big_body = "a" * (11 * 1024 * 1024)
    with TestClient(app) as client:
        r = client.post("/health", content=big_body)
    assert r.status_code != 413
