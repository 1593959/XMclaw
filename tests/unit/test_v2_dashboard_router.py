"""Unit tests for Sprint 2 Wave 6 — /api/v2/dashboard/overview.

Per testing rule (post-2026-05-09) the dashboard is the kind of
front+back feature where pure backend unit tests miss real-world
matching bugs. So we exercise the full HTTP path the frontend will
call (TestClient.get(real_url) against the real create_app), plus a
router-inspection layer for registration-order regressions.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.dashboard import router as dashboard_router


# ── Layer 1: router inspection ────────────────────────────────────


def test_dashboard_router_registers_overview() -> None:
    paths = [getattr(r, "path", "") for r in dashboard_router.routes]
    assert "/api/v2/dashboard/overview" in paths


# ── Layer 2: end-to-end TestClient ────────────────────────────────


@pytest.fixture
def empty_client() -> TestClient:
    """App with NO subsystems wired — every sub-block should fall
    through to a None / empty default. The endpoint itself must still
    return 200."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    app.state.boot_ts = time.time() - 120.0  # 2 min uptime
    app.state.lifespan_startup_duration_s = 1.23
    return TestClient(app)


def test_overview_returns_200_with_minimal_state(
    empty_client: TestClient,
) -> None:
    r = empty_client.get("/api/v2/dashboard/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    # Shape contract: all 7 top-level keys present, even if None.
    for key in (
        "now", "uptime", "proactive", "autobio",
        "cognition", "suggestions", "tasks", "storage",
    ):
        assert key in body, f"missing top-level key: {key}"


def test_overview_uptime_block(empty_client: TestClient) -> None:
    body = empty_client.get("/api/v2/dashboard/overview").json()
    u = body["uptime"]
    assert u["uptime_s"] is not None
    assert u["uptime_s"] >= 110.0  # We set boot_ts 120s ago, allow drift
    assert u["startup_duration_s"] == 1.23
    assert "version" in u  # may be None depending on import path


def test_overview_unwired_subsystems_return_null(
    empty_client: TestClient,
) -> None:
    """Sanity-check: a config={} app reports None for every subsystem
    the dashboard surfaces. The UI keys off this to render '未启用'."""
    body = empty_client.get("/api/v2/dashboard/overview").json()
    assert body["proactive"] is None
    assert body["autobio"] is None
    # cognition might still be present (CognitiveState constructed
    # lazily on agent attach) — accept None OR error-flagged dict
    assert body["suggestions"] is None
    assert body["tasks"] is None
    # storage is always returned (just file-size lookups)
    assert isinstance(body["storage"], dict)


# ── Subsystem-wired variants ──────────────────────────────────────


@pytest.fixture
def client_with_autobio(empty_client: TestClient) -> TestClient:
    """Attach a fake AutobiographicalMemory."""
    from xmclaw.cognition.autobiographical_memory import Person, Project
    fake = MagicMock()
    fake.people.return_value = [
        Person(
            id="p1", name="阿黄", relationship="朋友",
            importance=0.7, last_seen_ts=time.time() - 3600,
            notes={},
        ),
    ]
    fake.projects.return_value = [
        Project(
            id="pr1", name="XMclaw", status="active",
            current_focus="dashboard",
            last_touch_ts=time.time() - 60,
        ),
    ]
    empty_client.app.state.autobio_memory = fake
    return empty_client


def test_overview_with_autobio_returns_counts_and_recents(
    client_with_autobio: TestClient,
) -> None:
    body = client_with_autobio.get("/api/v2/dashboard/overview").json()
    assert body["autobio"] is not None
    assert body["autobio"]["people_count"] == 1
    assert body["autobio"]["project_count"] == 1
    assert body["autobio"]["recent_people"][0]["name"] == "阿黄"
    assert body["autobio"]["recent_projects"][0]["name"] == "XMclaw"
    assert (
        body["autobio"]["recent_projects"][0]["current_focus"]
        == "dashboard"
    )


@pytest.fixture
def client_with_proactive(empty_client: TestClient) -> TestClient:
    fake = MagicMock()

    class _T:
        name = "idle_check_in"
        cooldown_s = 3600.0

    fake._triggers = [_T()]
    fake._last_proposal_ts = time.time() - 90.0
    fake._last_user_message_ts = time.time() - 30.0
    fake._last_agent_message_ts = None
    fake._tick_interval_s = 30.0
    fake._is_quiet_hours_active = MagicMock(return_value=False)
    empty_client.app.state.proactive_agent = fake
    return empty_client


def test_overview_with_proactive_returns_triggers(
    client_with_proactive: TestClient,
) -> None:
    body = client_with_proactive.get("/api/v2/dashboard/overview").json()
    assert body["proactive"] is not None
    assert body["proactive"]["tick_interval_s"] == 30.0
    assert body["proactive"]["quiet_hours_active"] is False
    assert len(body["proactive"]["triggers"]) == 1
    assert body["proactive"]["triggers"][0]["name"] == "idle_check_in"
    assert body["proactive"]["triggers"][0]["cooldown_s"] == 3600.0
    assert body["proactive"]["last_proposal_ts"] is not None


def test_overview_storage_block_always_present(
    empty_client: TestClient,
) -> None:
    body = empty_client.get("/api/v2/dashboard/overview").json()
    s = body["storage"]
    assert "events_db_bytes" in s
    assert "memory_db_bytes" in s
    assert "autobio_db_bytes" in s
    # On a fresh test, the DB files won't exist — bytes should be None.
    assert "data_dir" in s
    assert isinstance(s["data_dir"], str)


def test_overview_resilient_to_failing_subsystem(
    empty_client: TestClient,
) -> None:
    """One subsystem raising must not 500 the whole endpoint."""
    bad = MagicMock()
    bad.people.side_effect = RuntimeError("db is wedged")
    bad.projects.side_effect = RuntimeError("db is wedged")
    empty_client.app.state.autobio_memory = bad

    r = empty_client.get("/api/v2/dashboard/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["autobio"] is not None
    assert "error" in body["autobio"]
    # Sibling blocks unaffected.
    assert "storage" in body


# ── Wave 8: recent_events timeline ────────────────────────────────


def test_overview_includes_recent_events_key(
    empty_client: TestClient,
) -> None:
    """Top-level key exists even when no SQLite bus is wired (in-memory
    test bus has no .query attribute)."""
    body = empty_client.get("/api/v2/dashboard/overview").json()
    assert "recent_events" in body
    # InProcessEventBus doesn't expose .query, so None is correct.
    assert body["recent_events"] is None


@pytest.fixture
def client_with_event_bus(empty_client: TestClient) -> TestClient:
    """Attach a fake bus with .query() returning realistic events."""

    class _Ev:
        def __init__(self, ev_type, payload, ts, ev_id):
            self.type = ev_type
            self.payload = payload
            self.ts = ts
            self.id = ev_id
            self.session_id = "s1"

    now = time.time()
    fake_bus = MagicMock()
    fake_bus.query.return_value = [
        _Ev(
            "proactive_proposal",
            {"trigger": "idle_check_in", "message": "你刚才忙完了吗？"},
            now - 60.0, "e1",
        ),
        _Ev(
            "reflection_cycle_ran",
            {"scope": "recent", "actions_taken": ["propose_skill"]},
            now - 30.0, "e2",
        ),
        _Ev(
            "goals_groomed",
            {"before": 5, "after": 3, "completed_archived": 2},
            now - 10.0, "e3",
        ),
    ]
    empty_client.app.state.bus = fake_bus
    return empty_client


def test_overview_recent_events_returns_summaries(
    client_with_event_bus: TestClient,
) -> None:
    body = client_with_event_bus.get(
        "/api/v2/dashboard/overview",
    ).json()
    evs = body["recent_events"]
    assert isinstance(evs, list)
    assert len(evs) == 3
    # Reversed = newest first.
    assert evs[0]["type"] == "goals_groomed"
    assert evs[1]["type"] == "reflection_cycle_ran"
    assert evs[2]["type"] == "proactive_proposal"
    # Human-readable summaries are baked server-side.
    assert "目标梳理" in evs[0]["summary"]
    assert "反思周期" in evs[1]["summary"]
    assert "主动发声" in evs[2]["summary"]
    assert "idle_check_in" in evs[2]["summary"]


def test_overview_recent_events_filters_by_type(
    client_with_event_bus: TestClient,
) -> None:
    """The endpoint must pass the curated whitelist into bus.query()
    — we don't want every tool_call clogging the timeline."""
    client_with_event_bus.get("/api/v2/dashboard/overview")
    fake_bus = client_with_event_bus.app.state.bus
    call_kwargs = fake_bus.query.call_args.kwargs
    types = call_kwargs.get("types") or []
    # Spot-check a few must-be-included types.
    assert "proactive_proposal" in types
    assert "reflection_cycle_ran" in types
    assert "metacognition_proposal" in types
    # tool_call is intentionally NOT in the whitelist.
    assert "tool_call" not in types


def test_overview_recent_events_resilient_to_bus_failure(
    empty_client: TestClient,
) -> None:
    """A failing bus.query() must not 500 the whole endpoint."""
    bad = MagicMock()
    bad.query.side_effect = RuntimeError("db locked")
    empty_client.app.state.bus = bad

    r = empty_client.get("/api/v2/dashboard/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    evs = body["recent_events"]
    assert isinstance(evs, list)
    assert len(evs) == 1
    assert "error" in evs[0]
