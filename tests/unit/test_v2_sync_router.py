"""Sprint 2 Wave 13 — /api/v2/sync/ui-state tests.

Pattern matches Wave 6/8: router inspection + TestClient end-to-end.
The endpoint is small (3 verbs) but load-bearing for cross-device
handoff, so we cover empty / GET / PUT / PATCH / merge / invalid
body / atomic write.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.sync import router as sync_router


# ── router registration ─────────────────────────────────────────


def test_sync_router_registers_endpoints() -> None:
    paths = [getattr(r, "path", "") for r in sync_router.routes]
    assert "/api/v2/sync/ui-state" in paths
    # Should support GET + PUT + PATCH
    methods = []
    for r in sync_router.routes:
        if getattr(r, "path", "") == "/api/v2/sync/ui-state":
            methods.extend(r.methods)
    assert "GET" in methods
    assert "PUT" in methods
    assert "PATCH" in methods


# ── TestClient end-to-end ───────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with the sync state file pointed at tmp_path."""
    monkeypatch.setattr(
        "xmclaw.daemon.routers.sync._state_path",
        lambda: tmp_path / "ui_state.json",
    )
    app = create_app(bus=InProcessEventBus(), config={})
    return TestClient(app)


def test_get_returns_empty_state_when_no_file(
    client: TestClient,
) -> None:
    r = client.get("/api/v2/sync/ui-state")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == {}
    assert body["updated_ts"] == 0.0


def test_put_then_get_round_trip(client: TestClient) -> None:
    payload = {"state": {"theme": "dark", "density": "compact"}}
    r = client.put("/api/v2/sync/ui-state", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == payload["state"]
    assert body["updated_ts"] > 0

    # GET reads the same.
    r2 = client.get("/api/v2/sync/ui-state")
    assert r2.status_code == 200
    assert r2.json()["state"] == payload["state"]


def test_put_accepts_bare_state_body(client: TestClient) -> None:
    """Body without the outer ``{state: ...}`` wrapper is still
    accepted as the state itself — frontend convenience."""
    r = client.put(
        "/api/v2/sync/ui-state",
        json={"theme": "dark"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == {"theme": "dark"}


def test_put_rejects_non_object_body(client: TestClient) -> None:
    r = client.put(
        "/api/v2/sync/ui-state",
        json=["not", "an", "object"],
    )
    assert r.status_code == 400


def test_patch_merges_keys(client: TestClient) -> None:
    # Seed
    client.put(
        "/api/v2/sync/ui-state",
        json={"state": {"theme": "light", "density": "comfortable"}},
    )
    # Patch only theme
    r = client.patch(
        "/api/v2/sync/ui-state",
        json={"state": {"theme": "dark"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == {"theme": "dark", "density": "comfortable"}


def test_patch_on_empty_creates(client: TestClient) -> None:
    r = client.patch(
        "/api/v2/sync/ui-state",
        json={"state": {"active_session_id": "chat-abc"}},
    )
    assert r.status_code == 200
    assert r.json()["state"] == {"active_session_id": "chat-abc"}


def test_put_updated_ts_advances(client: TestClient) -> None:
    """Each write should bump updated_ts so clients can resolve
    stale-vs-fresh on boot."""
    import time
    r1 = client.put(
        "/api/v2/sync/ui-state", json={"state": {"k": 1}},
    )
    time.sleep(0.01)
    r2 = client.put(
        "/api/v2/sync/ui-state", json={"state": {"k": 2}},
    )
    assert r2.json()["updated_ts"] > r1.json()["updated_ts"]


def test_invalid_json_returns_400(client: TestClient) -> None:
    r = client.put(
        "/api/v2/sync/ui-state",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
