"""Approvals REST router — `/api/v2/approvals`.

Coverage gap surfaced by audit B-P0-3: the router was only exercised
indirectly through factory imports, leaving its three endpoints (list /
approve / deny) effectively untested. This file pins down:

  * ``GET  /api/v2/approvals``                 — list, all + filtered
  * ``POST /api/v2/approvals/{id}/approve``    — happy path + 404
  * ``POST /api/v2/approvals/{id}/deny``       — happy path + 404
  * Round-trip with ``ApprovalService`` so the response payload shape
    (request_id / session_id / tool_name / status / created_at /
    findings_summary) doesn't drift from what the front-end renders.

We hit the live ``app.state.approval_service`` rather than mocking the
service — the whole point of the test is that the wiring is correct.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


@pytest.fixture
def client() -> TestClient:
    bus = InProcessEventBus()
    return TestClient(create_app(bus=bus))


def _seed(client: TestClient, *, session_id: str, tool_name: str,
          params: dict[str, Any] | None = None,
          summary: str = "synthetic finding") -> str:
    """Push a pending approval directly through the live service so the
    REST tests don't have to drive a full guard scan to get a record
    in the queue. Returns the minted request_id.

    ``ApprovalService.create`` is async; we drive it from sync test
    code by running it on a fresh event loop inside a worker thread
    (the TestClient already owns the main asyncio loop, so we can't
    reuse it here).
    """
    import asyncio
    import threading

    svc = client.app.state.approval_service

    box: list[str] = []
    err: list[BaseException] = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(
                svc.create(session_id, tool_name, params or {}, summary)
            ))
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            loop.close()

    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    if err:
        raise err[0]
    return box[0]


# ── list_approvals ─────────────────────────────────────────────────────


def test_list_empty_when_no_pending(client: TestClient) -> None:
    resp = client.get("/api/v2/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"pending": []}


def test_list_returns_serialized_record(client: TestClient) -> None:
    request_id = _seed(
        client,
        session_id="sid-A",
        tool_name="execute_shell_command",
        params={"command": "rm -rf /home/foo"},
        summary="HIGH severity dangerous rm",
    )
    resp = client.get("/api/v2/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["pending"]) == 1
    rec = body["pending"][0]
    # The serializer pins the JSON shape the front-end depends on. If
    # this changes, _both_ the router and the UI need updating.
    assert set(rec.keys()) == {
        "request_id",
        "session_id",
        "tool_name",
        "status",
        "created_at",
        "findings_summary",
    }
    assert rec["request_id"] == request_id
    assert rec["session_id"] == "sid-A"
    assert rec["tool_name"] == "execute_shell_command"
    assert rec["status"] == "pending"
    assert rec["findings_summary"] == "HIGH severity dangerous rm"
    assert isinstance(rec["created_at"], (int, float))


def test_list_filters_by_session_id(client: TestClient) -> None:
    a_id = _seed(client, session_id="sid-A", tool_name="t1")
    b_id = _seed(client, session_id="sid-B", tool_name="t2")

    resp_a = client.get("/api/v2/approvals", params={"session_id": "sid-A"})
    resp_b = client.get("/api/v2/approvals", params={"session_id": "sid-B"})
    resp_all = client.get("/api/v2/approvals")

    ids_a = {r["request_id"] for r in resp_a.json()["pending"]}
    ids_b = {r["request_id"] for r in resp_b.json()["pending"]}
    ids_all = {r["request_id"] for r in resp_all.json()["pending"]}

    assert ids_a == {a_id}
    assert ids_b == {b_id}
    assert ids_all == {a_id, b_id}


# ── approve ────────────────────────────────────────────────────────────


def test_approve_success_moves_record_out_of_pending(client: TestClient) -> None:
    request_id = _seed(client, session_id="sid-A", tool_name="t1")
    resp = client.post(f"/api/v2/approvals/{request_id}/approve")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # No longer pending.
    listing = client.get("/api/v2/approvals").json()
    assert listing["pending"] == []


def test_approve_unknown_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v2/approvals/does-not-exist/approve")
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body


def test_approve_twice_second_is_404(client: TestClient) -> None:
    """First approve succeeds and pops the record; the second call
    sees nothing and returns 404. Protects the idempotency contract
    the WS layer relies on when retrying."""
    request_id = _seed(client, session_id="sid-A", tool_name="t1")
    first = client.post(f"/api/v2/approvals/{request_id}/approve")
    second = client.post(f"/api/v2/approvals/{request_id}/approve")
    assert first.status_code == 200
    assert second.status_code == 404


# ── deny ───────────────────────────────────────────────────────────────


def test_deny_success_moves_record_out_of_pending(client: TestClient) -> None:
    request_id = _seed(client, session_id="sid-A", tool_name="t1")
    resp = client.post(f"/api/v2/approvals/{request_id}/deny")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    listing = client.get("/api/v2/approvals").json()
    assert listing["pending"] == []


def test_deny_unknown_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v2/approvals/does-not-exist/deny")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_deny_after_approve_is_404(client: TestClient) -> None:
    """Once approved, a record is no longer pending — denying it must
    not retroactively flip the approval. The router's ``svc.deny`` only
    pops from ``_pending`` and returns False for completed records."""
    request_id = _seed(client, session_id="sid-A", tool_name="t1")
    client.post(f"/api/v2/approvals/{request_id}/approve")
    resp = client.post(f"/api/v2/approvals/{request_id}/deny")
    assert resp.status_code == 404
