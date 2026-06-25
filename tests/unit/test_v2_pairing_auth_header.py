from __future__ import annotations

from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


async def _accept_only_good_token(presented: str | None) -> bool:
    return presented == "good-token"


def test_api_route_accepts_x_xmc_token_header() -> None:
    app = create_app(config={}, auth_check=_accept_only_good_token)
    with TestClient(app) as client:
        response = client.get(
            "/api/v2/sessions",
            headers={"X-XMC-Token": "good-token"},
        )

    assert response.status_code != 401


def test_api_route_rejects_wrong_x_xmc_token_header() -> None:
    app = create_app(config={}, auth_check=_accept_only_good_token)
    with TestClient(app) as client:
        response = client.get(
            "/api/v2/sessions",
            headers={"X-XMC-Token": "wrong-token"},
        )

    assert response.status_code == 401
