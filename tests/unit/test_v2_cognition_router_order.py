"""Cognition router route-order regression.

Pre-fix bug: ``/api/v2/cognition/tasks/graph`` returned 404 because
``/tasks/{task_id}`` was registered BEFORE ``/tasks/graph`` in the
file, so FastAPI matched ``/tasks/graph`` to the parameterized route
with ``task_id="graph"``, looked up the (non-existent) task, and
returned 404 — masking the existence of the DAG endpoint entirely.

**Testing rule (post-2026-05-09)**: every test for a feature that
spans frontend + backend MUST exercise the full HTTP path the
frontend actually uses (TestClient.get(real_url)), not just inspect
internal router state. Pure router inspection misses real-world
matching bugs (this one), prefix collisions, and request validation
mismatches.

This file ships TWO layers:
  1. Router inspection — quick, catches registration-order regressions
  2. End-to-end TestClient — pings the real URL the UI calls, asserts
     the response actually reaches the intended handler
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.cognition.graph_runtime import GraphState
from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.cognition import router


def _route_order(path_prefix: str) -> list[str]:
    """Return the ordered list of route paths under ``path_prefix``
    (e.g. "/api/v2/cognition/tasks") in the order they were registered."""
    out: list[str] = []
    for r in router.routes:
        path = getattr(r, "path", "")
        if path.startswith(path_prefix):
            out.append(path)
    return out


def test_cognition_router_tasks_graph_before_task_id() -> None:
    """``/tasks/graph`` MUST be registered before ``/tasks/{task_id}``
    so the DAG endpoint isn't masked by the parameterized route.
    """
    paths = _route_order("/api/v2/cognition/tasks")
    assert "/api/v2/cognition/tasks/graph" in paths, (
        "/tasks/graph route missing entirely from cognition router"
    )
    assert "/api/v2/cognition/tasks/{task_id}" in paths, "/tasks/{task_id} route missing"
    graph_idx = paths.index("/api/v2/cognition/tasks/graph")
    param_idx = paths.index("/api/v2/cognition/tasks/{task_id}")
    assert graph_idx < param_idx, (
        f"Route order bug: ``/tasks/graph`` (idx {graph_idx}) is "
        f"registered AFTER ``/tasks/{{task_id}}`` (idx {param_idx}). "
        f"FastAPI matches in registration order — concrete routes "
        f"must precede parameterized siblings.\n"
        f"Full /tasks order: {paths}"
    )


def test_cognition_router_tasks_graph_state_before_task_id() -> None:
    paths = _route_order("/api/v2/cognition/tasks")
    assert "/api/v2/cognition/tasks/graph-state" in paths
    assert "/api/v2/cognition/tasks/{task_id}" in paths
    assert paths.index("/api/v2/cognition/tasks/graph-state") < paths.index(
        "/api/v2/cognition/tasks/{task_id}"
    )


def test_cognition_router_concrete_subpaths_before_param() -> None:
    """General invariant: any concrete sub-route under a
    parameterized prefix must come first. Currently only checks the
    ``/tasks/*`` family — extend if new collisions appear.
    """
    families = [
        ("/api/v2/cognition/tasks", "/api/v2/cognition/tasks/{task_id}"),
        # Future-proof slot: when /goals/<concrete> is added it must
        # come before /goals/{goal_id} (currently only /goals exists).
    ]
    for prefix, param_route in families:
        paths = _route_order(prefix)
        if param_route not in paths:
            continue
        param_idx = paths.index(param_route)
        # Any path that is /<prefix>/<concrete> (no curly braces)
        # registered AFTER param_idx is a bug.
        for i, p in enumerate(paths):
            if i <= param_idx:
                continue
            if p.startswith(prefix + "/") and "{" not in p[len(prefix) + 1:]:
                raise AssertionError(
                    f"Concrete route {p!r} (idx {i}) is registered "
                    f"AFTER parameterized {param_route!r} (idx "
                    f"{param_idx}). Move {p!r} ABOVE the parameterized "
                    f"route."
                )


# ── Layer 2: end-to-end TestClient (frontend's actual request) ──


@pytest.fixture
def client_with_scheduler() -> TestClient:
    """Build a real app with a fake TaskScheduler attached to
    ``app.state.task_scheduler`` so the cognition routes resolve.
    The scheduler is mocked — we don't care about real DAG output,
    only that the URL routes to the DAG handler (not the
    parametrized get_task handler that returns 404)."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    fake_scheduler = MagicMock()
    fake_scheduler.list_tasks = AsyncMock(return_value=[])
    fake_scheduler.get_task = AsyncMock(return_value=None)
    fake_scheduler.snapshot_graph_state = AsyncMock(
        return_value=GraphState(thread_id="test", run_id="test")
    )
    app.state.task_scheduler = fake_scheduler

    # CognitiveState too — `/state` route checks app.state.cognitive_state
    fake_state = MagicMock()
    fake_state.current_goals = []
    fake_state.attention_focus = []
    fake_state.fatigue = {}
    fake_state.salience_threshold = 0.3
    fake_state.attention_capacity = 7
    app.state.cognitive_state = fake_state

    return TestClient(app)


def test_cognition_tasks_graph_reaches_dag_handler_not_404(
    client_with_scheduler: TestClient,
) -> None:
    """Hit the SAME url the frontend uses. Response must come from the
    DAG handler (200 with ``{nodes, edges}``), NOT from the
    parametrized handler that 404s on missing task_id="graph".

    This is the test that would have caught the original bug at
    PR-time, before a daemon restart was needed to discover it."""
    # Pre-bug: this returned 404 ``{"error": "not found"}``.
    r = client_with_scheduler.get("/api/v2/cognition/tasks/graph")
    assert r.status_code == 200, (
        f"GET /api/v2/cognition/tasks/graph returned {r.status_code} "
        f"(expected 200). Body: {r.text!r}. Most likely cause: route "
        f"order regression — ``/tasks/graph`` is being shadowed by "
        f"``/tasks/{{task_id}}``."
    )
    body = r.json()
    assert "nodes" in body, f"missing 'nodes' key — body={body!r}"
    assert "edges" in body, f"missing 'edges' key — body={body!r}"
    # No-tasks scheduler → empty DAG, but keys must exist.
    assert isinstance(body["nodes"], list)
    assert isinstance(body["edges"], list)


def test_cognition_tasks_graph_state_reaches_graph_state_handler(
    client_with_scheduler: TestClient,
) -> None:
    r = client_with_scheduler.get("/api/v2/cognition/tasks/graph-state")
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "test"
    assert body["run_id"] == "test"
    assert "subtasks" in body


def test_cognition_tasks_concrete_task_id_still_works(
    client_with_scheduler: TestClient,
) -> None:
    """Confirm the route-order fix didn't break the parameterized
    handler — a real task_id like ``my-task-1`` must still hit
    ``get_task`` (which 404s only because the mocked scheduler returns
    None, NOT because of route mismatching)."""
    r = client_with_scheduler.get("/api/v2/cognition/tasks/my-task-1")
    # This is the EXPECTED 404: the route resolved correctly to
    # get_task, which couldn't find the (non-existent) task.
    assert r.status_code == 404
    assert r.json().get("error") == "not found"


def test_cognition_state_endpoint_reachable_from_ui_url(
    client_with_scheduler: TestClient,
) -> None:
    """The frontend Cognition page hits 5 endpoints in parallel
    (apiGet calls in pages/Cognition.js loadAll). Smoke-check that
    each routes correctly. This is the front-back integration test
    that pure router inspection cannot do."""
    ui_urls = [
        "/api/v2/cognition/state",
        "/api/v2/cognition/tasks",
        "/api/v2/cognition/proposals",
        "/api/v2/cognition/graph/stats",
        "/api/v2/cognition/tasks/graph",
        "/api/v2/cognition/tasks/graph-state",
        "/api/v2/cognition/daemon",
        "/api/v2/cognition/daemon/history",
        "/api/v2/cognition/daemon/health",
        "/api/v2/cognition/experiments",
    ]
    failures: list[tuple[str, int]] = []
    for url in ui_urls:
        r = client_with_scheduler.get(url)
        # Allow 200 (handler ran) OR 503 (handler ran + said "not
        # wired" — which means the route resolved, just no underlying
        # subsystem). NOT allowed: 404 (route mismatch — the bug).
        if r.status_code == 404:
            failures.append((url, 404))
    assert not failures, (
        f"frontend pages/Cognition.js loadAll() URL(s) returned 404 "
        f"(route mismatch — should never happen): {failures}"
    )


# ── Phase D: daemon + experiment observability endpoints ───────────────


def test_daemon_status_returns_daemon_shape(client_with_scheduler: TestClient) -> None:
    """GET /daemon must return tick_count + running + config keys."""
    r = client_with_scheduler.get("/api/v2/cognition/daemon")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert body["ok"] is True
        assert "running" in body
        assert "tick_count" in body
        assert "config" in body
        assert "autonomy_level" in body["config"]


def test_experiments_list_returns_ok_when_wired(client_with_scheduler: TestClient) -> None:
    """GET /experiments must return 200 with an experiments list."""
    r = client_with_scheduler.get("/api/v2/cognition/experiments")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body.get("experiments"), list)
        assert "count" in body


def test_experiment_by_id_404_for_unknown(client_with_scheduler: TestClient) -> None:
    """GET /experiments/{id} must 404 for a non-existent experiment."""
    r = client_with_scheduler.get("/api/v2/cognition/experiments/nope")
    assert r.status_code in (404, 503)
    if r.status_code == 404:
        assert r.json().get("ok") is False


def test_daemon_history_returns_ticks_shape(client_with_scheduler: TestClient) -> None:
    """GET /daemon/history must return a ticks list (or 503 if store
    missing)."""
    r = client_with_scheduler.get("/api/v2/cognition/daemon/history")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body.get("ticks"), list)
        assert "count" in body


def test_daemon_health_returns_status(client_with_scheduler: TestClient) -> None:
    """GET /daemon/health must return status + tick_count + last_tick."""
    r = client_with_scheduler.get("/api/v2/cognition/daemon/health")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert body["ok"] is True
        assert body["status"] in ("healthy", "degraded", "unhealthy")
        assert "running" in body
        assert "tick_count" in body
        # last_tick may be absent if no tick has run yet.
        if "last_tick" in body:
            assert "tick" in body["last_tick"]
            assert "latency_ms" in body["last_tick"]
            assert "errors" in body["last_tick"]
        # memory_mb is optional (only when psutil is installed).
        if "memory_mb" in body:
            assert isinstance(body["memory_mb"], (int, float))
