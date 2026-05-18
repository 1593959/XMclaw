"""Unit tests for Sprint 2 Wave 6 — /api/v2/dashboard/overview.

Per testing rule (post-2026-05-09) the dashboard is the kind of
front+back feature where pure backend unit tests miss real-world
matching bugs. So we exercise the full HTTP path the frontend will
call (TestClient.get(real_url) against the real create_app), plus a
router-inspection layer for registration-order regressions.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

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
    — we don't want every tool_call clogging the timeline. Wave 20
    introduced a second bus.query for cost_today, so we scan all
    calls and verify ONE of them carries the timeline-events filter.
    """
    client_with_event_bus.get("/api/v2/dashboard/overview")
    fake_bus = client_with_event_bus.app.state.bus
    all_calls_types = [
        (c.kwargs.get("types") or [])
        for c in fake_bus.query.call_args_list
    ]
    flattened = {t for types in all_calls_types for t in types}
    # Spot-check a few must-be-included types from the timeline filter.
    assert "proactive_proposal" in flattened
    assert "reflection_cycle_ran" in flattened
    assert "metacognition_proposal" in flattened
    # tool_call is intentionally NOT in either whitelist.
    assert "tool_call" not in flattened


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


# ── Wave 20: cost_today block ─────────────────────────────────────


def test_overview_includes_cost_today_key(
    empty_client: TestClient,
) -> None:
    body = empty_client.get("/api/v2/dashboard/overview").json()
    assert "cost_today" in body
    # In-memory bus → None (no .query)
    assert body["cost_today"] is None


@pytest.fixture
def client_with_cost_events(empty_client: TestClient) -> TestClient:
    """Bus that returns a mix of COST_TICK events from a couple
    different models, simulating ~24h of agent activity."""

    class _Ev:
        def __init__(self, payload):
            self.type = "cost_tick"
            self.payload = payload
            self.ts = time.time() - 60.0

    fake_bus = MagicMock()
    fake_bus.query.return_value = [
        _Ev({
            "model": "claude-opus-4-7",
            "cost_usd": 0.012,
            "prompt_tokens": 1500,
            "completion_tokens": 400,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 800,
        }),
        _Ev({
            "model": "claude-opus-4-7",
            "cost_usd": 0.008,
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 200,
        }),
        _Ev({
            "model": "kimi-k2.6",
            "cost_usd": 0.001,
            "prompt_tokens": 800,
            "completion_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }),
    ]
    empty_client.app.state.bus = fake_bus
    return empty_client


def test_cost_today_aggregates_totals(
    client_with_cost_events: TestClient,
) -> None:
    body = client_with_cost_events.get(
        "/api/v2/dashboard/overview",
    ).json()
    c = body["cost_today"]
    assert c is not None
    assert c["call_count"] == 3
    assert c["total_usd"] == 0.021  # 0.012 + 0.008 + 0.001
    assert c["prompt_tokens"] == 3300
    assert c["completion_tokens"] == 900


def test_cost_today_groups_by_model_descending(
    client_with_cost_events: TestClient,
) -> None:
    body = client_with_cost_events.get(
        "/api/v2/dashboard/overview",
    ).json()
    by_model = body["cost_today"]["by_model"]
    assert len(by_model) == 2
    assert by_model[0]["model"] == "claude-opus-4-7"
    assert by_model[0]["calls"] == 2
    assert by_model[0]["cost_usd"] == 0.02
    assert by_model[1]["model"] == "kimi-k2.6"
    assert by_model[1]["calls"] == 1


def test_cost_today_cache_hit_rate(
    client_with_cost_events: TestClient,
) -> None:
    body = client_with_cost_events.get(
        "/api/v2/dashboard/overview",
    ).json()
    # Wave-30 formula change (2026-05-18): cache_hit_rate is now
    # ``read / total_input`` where total_input = prompt + read +
    # creation. Old formula was ``read / (read + creation)`` which
    # reported ~100% as soon as any cache existed.
    #
    # Fixture totals:
    #   prompt:    1500 + 1000 + 800 = 3300
    #   creation:                       0
    #   read:       800 +  200 +   0 = 1000
    #   total_input = 3300 + 0 + 1000 = 4300
    #   hit_rate    = 1000 / 4300 ≈ 0.233
    assert body["cost_today"]["cache_hit_rate"] == 0.233


def test_cost_today_no_cache_returns_zero_hit_rate(
    empty_client: TestClient,
) -> None:
    """When every call has 0 cache tokens but real prompt_tokens
    exist, hit_rate is 0.0 (not None). The earlier ``None``
    silently hid the metric from the dashboard exactly when the
    user most needs to know cache isn't working — a fresh-install
    misconfiguration of OpenAI / DeepSeek / unknown shims where
    cache_control would be ignored. ``cache_hit_rate: 0.0``
    surfaces as a 0.0% widget so the operator can react."""
    class _Ev:
        def __init__(self):
            self.type = "cost_tick"
            self.payload = {
                "model": "gpt-4o",
                "cost_usd": 0.005,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            self.ts = time.time()

    fake_bus = MagicMock()
    fake_bus.query.return_value = [_Ev()]
    empty_client.app.state.bus = fake_bus

    body = empty_client.get("/api/v2/dashboard/overview").json()
    assert body["cost_today"]["cache_hit_rate"] == 0.0


def test_cost_today_hit_rate_none_when_zero_total_input(
    empty_client: TestClient,
) -> None:
    """The None branch is reserved for the genuinely empty case —
    no LLM activity at all (no prompts, no cache). Without this
    division-by-zero would crash."""
    class _Ev:
        def __init__(self):
            self.type = "cost_tick"
            self.payload = {
                "model": "?",
                "cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            self.ts = time.time()

    fake_bus = MagicMock()
    fake_bus.query.return_value = [_Ev()]
    empty_client.app.state.bus = fake_bus

    body = empty_client.get("/api/v2/dashboard/overview").json()
    assert body["cost_today"]["cache_hit_rate"] is None


def test_cost_today_resilient_to_bus_failure(
    empty_client: TestClient,
) -> None:
    bad = MagicMock()
    # bus.query for "proactive_proposal" types (recent_events) returns
    # OK; bus.query for "cost_tick" throws. Cleanest way to test cost
    # block isolation is to make query unconditionally raise — both
    # blocks degrade gracefully.
    bad.query.side_effect = RuntimeError("db wedged")
    empty_client.app.state.bus = bad

    r = empty_client.get("/api/v2/dashboard/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cost_today"] is not None
    assert "error" in body["cost_today"]


def test_tasks_block_awaits_async_list_tasks(
    empty_client: TestClient,
) -> None:
    """Regression: production TaskScheduler.list_tasks is async.
    The dashboard block must await it, not pass back a coroutine
    that the downstream list() chokes on."""

    class _T:
        status = "running"

    sched = MagicMock()
    sched.list_tasks = AsyncMock(return_value=[_T(), _T(), _T()])
    empty_client.app.state.task_scheduler = sched

    r = empty_client.get("/api/v2/dashboard/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["tasks"] is not None
    assert "error" not in body["tasks"]
    assert body["tasks"]["total"] == 3
    assert body["tasks"]["by_status"] == {"running": 3}


def test_cost_today_empty_events_returns_zeroes(
    empty_client: TestClient,
) -> None:
    fake_bus = MagicMock()
    fake_bus.query.return_value = []
    empty_client.app.state.bus = fake_bus

    body = empty_client.get("/api/v2/dashboard/overview").json()
    c = body["cost_today"]
    assert c is not None
    assert c["call_count"] == 0
    assert c["total_usd"] == 0.0
    assert c["prompt_tokens"] == 0
    assert c["by_model"] == []
