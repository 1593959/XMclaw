"""POST /api/v2/cognition/suggestions/{id}/{approve,reject} +
GET /suggestions — R5 front-back contract test (2026-05-10).

Per CLAUDE.md (2026-05-09 standing rule), front-back tests must hit
the real ``create_app`` via TestClient. Pins:

  * Routes registered (no shadowing).
  * GET /suggestions filters by status + returns count + payload.
  * POST .../approve flips pending → approved.
  * POST .../reject flips pending → rejected.
  * Re-deciding a non-pending row is a no-op (idempotent).
  * 503 when the inbox isn't wired (cognition not enabled).
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.cognition.suggestion_inbox import (
    Suggestion,
    SuggestionInbox,
)
from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


class _CtxClient:
    """Mirrors the pattern from test_v2_cognition_plan_endpoint —
    inject test fixtures AFTER lifespan startup so lifespan-built
    state can't clobber our test wiring."""

    def __init__(self, *, inbox: SuggestionInbox | None = None) -> None:
        bus = InProcessEventBus()
        self._app = create_app(
            bus=bus, config={"cognition": {"enabled": True}},
        )
        self._inbox = inbox
        self._tc: TestClient | None = None

    def __enter__(self) -> TestClient:
        self._tc = TestClient(self._app)
        self._tc.__enter__()
        # Replace post-lifespan.
        self._app.state.suggestion_inbox = self._inbox
        return self._tc

    def __exit__(self, *exc) -> None:
        if self._tc is not None:
            self._tc.__exit__(*exc)
            self._tc = None


def _client_with(inbox: Any | None) -> _CtxClient:
    return _CtxClient(inbox=inbox)


# ── Route registration ───────────────────────────────────────────


def test_routes_registered() -> None:
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    paths = {r.path for r in app.routes}
    assert "/api/v2/cognition/suggestions" in paths
    assert "/api/v2/cognition/suggestions/{sg_id}/approve" in paths
    assert "/api/v2/cognition/suggestions/{sg_id}/reject" in paths


# ── GET /suggestions ─────────────────────────────────────────────


def test_get_pending_returns_pending_only(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    s1 = inbox.add(Suggestion(kind="curriculum_edit", summary="a"))
    s2 = inbox.add(Suggestion(kind="preference_update", summary="b"))
    inbox.decide(s1, status="approved")
    with _client_with(inbox) as tc:
        r = tc.get("/api/v2/cognition/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["pending_total"] == 1
    assert {s["id"] for s in body["suggestions"]} == {s2}
    assert body["suggestions"][0]["kind"] == "preference_update"


def test_get_all_returns_everything(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    s1 = inbox.add(Suggestion(kind="x", summary="a"))
    s2 = inbox.add(Suggestion(kind="y", summary="b"))
    inbox.decide(s1, status="approved")
    with _client_with(inbox) as tc:
        r = tc.get("/api/v2/cognition/suggestions?status=all")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2


def test_get_by_status_approved(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    s1 = inbox.add(Suggestion(kind="x", summary="a"))
    inbox.add(Suggestion(kind="y", summary="b"))
    inbox.decide(s1, status="approved")
    with _client_with(inbox) as tc:
        r = tc.get("/api/v2/cognition/suggestions?status=approved")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["suggestions"][0]["status"] == "approved"


def test_get_unknown_status_returns_400(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    with _client_with(inbox) as tc:
        r = tc.get("/api/v2/cognition/suggestions?status=banana")
    assert r.status_code == 400


def test_get_returns_503_when_no_inbox() -> None:
    with _client_with(inbox=None) as tc:
        r = tc.get("/api/v2/cognition/suggestions")
    # _not_wired returns a structured 503.
    assert r.status_code == 503


# ── POST approve / reject ────────────────────────────────────────


def test_approve_flips_pending_to_approved(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    with _client_with(inbox) as tc:
        r = tc.post(f"/api/v2/cognition/suggestions/{sid}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "approved"
    # State persisted.
    sg = inbox.get(sid)
    assert sg.status == "approved"


def test_reject_flips_pending_to_rejected(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    with _client_with(inbox) as tc:
        r = tc.post(f"/api/v2/cognition/suggestions/{sid}/reject")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert inbox.get(sid).status == "rejected"


def test_re_decide_returns_ok_false(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    with _client_with(inbox) as tc:
        # First approve OK.
        r1 = tc.post(f"/api/v2/cognition/suggestions/{sid}/approve")
        # Second approve should be a no-op (already not pending).
        r2 = tc.post(f"/api/v2/cognition/suggestions/{sid}/approve")
    assert r1.json()["ok"] is True
    # 200 status (graceful), but ok=False so the UI knows nothing
    # changed.
    assert r2.status_code == 200
    assert r2.json()["ok"] is False


def test_decide_unknown_id_returns_ok_false(tmp_path) -> None:  # noqa: ANN001
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    with _client_with(inbox) as tc:
        r = tc.post("/api/v2/cognition/suggestions/ghost-id/approve")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_decide_returns_503_when_no_inbox() -> None:
    with _client_with(inbox=None) as tc:
        r = tc.post("/api/v2/cognition/suggestions/x/approve")
    assert r.status_code == 503
