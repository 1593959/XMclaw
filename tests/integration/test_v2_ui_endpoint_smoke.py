"""Front-back smoke audit: every URL the UI pages call must NOT 404 / 500.

Built per the 2026-05-09 standing rule: tests for cross-cutting features
must exercise the full HTTP path the frontend uses (TestClient.get
against the real create_app), not just inspect router internals.

This file iterates through every distinct API URL the static UI pages
call (extracted via grep on the apiGet/apiPost calls in
``xmclaw/daemon/static/pages/*.js``) and asserts each route resolves
to the right handler. We allow:

  * **200** — handler ran cleanly
  * **400** — handler reached + complained about missing payload (the
    common POST-without-body case; not a routing bug)
  * **422** — FastAPI request-validation rejected (also routing-OK)
  * **503** — handler reached + said the underlying subsystem isn't
    wired (e.g. memory backend not configured in the test app); this
    is correct degradation behaviour
  * **401 / 403** — auth said no (token not provided in test;
    routing-OK)

We REJECT:

  * **404** — frontend URL doesn't reach a handler (route mismatch /
    typo / order bug — exactly what bit us in /tasks/graph)
  * **5xx other than 503** — handler crashed (5xx, 502, 504...)
  * **405** — wrong method (frontend POSTs to a GET-only or vice versa)

When this test fails it lists every broken URL with its status; the
fix is per-URL targeted.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.cognition.graph_runtime import GraphState
from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


# ── extracted from `grep -rn apiGet/apiPost xmclaw/daemon/static/pages/`
# (canonical UI URL inventory as of 2026-05-09). When you add a new UI
# call, add the URL here — the smoke covers it automatically.
#
# Each entry: (METHOD, url, expected_status_set, optional_body)
# ``expected_status_set`` is the set of status codes that mean the
# route resolved correctly (handler reached). 404 / 5xx-other-than-503
# / 405 are ALWAYS the failure set.

UI_ENDPOINT_INVENTORY: list[tuple[str, str, set[int]]] = [
    # ── cognition (Phase 6) ─────────────────────
    ("GET", "/api/v2/cognition/state", {200, 503}),
    ("GET", "/api/v2/cognition/tasks", {200, 503}),
    ("GET", "/api/v2/cognition/tasks/graph", {200, 503}),
    ("GET", "/api/v2/cognition/tasks/graph-state", {200, 503}),
    ("GET", "/api/v2/cognition/proposals", {200, 503}),
    ("GET", "/api/v2/cognition/graph/stats", {200, 503}),
    # ── memory ─────────────────────────────────
    ("GET", "/api/v2/memory", {200, 401, 503}),
    ("GET", "/api/v2/memory/pinned", {200, 401, 503}),
    ("GET", "/api/v2/memory/providers", {200, 401, 503}),
    ("GET", "/api/v2/memory/providers/available", {200, 401, 503}),
    ("GET", "/api/v2/memory/dream/backups", {200, 401, 503}),
    ("GET", "/api/v2/memory/dream/status", {200, 401, 503}),
    ("GET", "/api/v2/memory/indexer_status", {200, 401, 503}),
    ("GET", "/api/v2/memory/relevant_picker/status", {200, 401, 503}),
    # POST /unified_query needs a body — empty body 400 is OK; what we
    # check is that the route resolves (no 404 from /{filename} bleed).
    ("POST", "/api/v2/memory/unified_query", {200, 400, 401, 503}),
    # POST /unified_put — §3.3.4 atomic write. Empty body 400 is OK;
    # the test guards against /{filename} catch-all shadowing.
    ("POST", "/api/v2/memory/unified_put", {200, 400, 401, 503}),
    # ── core daemon ────────────────────────────
    ("GET", "/api/v2/status", {200, 401}),
    ("GET", "/api/v2/config", {200, 401}),
    ("GET", "/api/v2/agents", {200, 401}),
    ("GET", "/api/v2/skills", {200, 401}),
    ("GET", "/api/v2/skills/installed", {200, 401}),
    ("GET", "/api/v2/llm/profiles", {200, 401}),
    ("GET", "/api/v2/sessions?limit=200", {200, 401}),
    # ── tools / scheduler ──────────────────────
    ("GET", "/api/v2/cron", {200, 401}),
    ("GET", "/api/v2/channels", {200, 401}),
    ("GET", "/api/v2/approvals", {200, 401}),
    ("GET", "/api/v2/backup", {200, 401}),
    # ── files / docs / workspace ──────────────
    ("GET", "/api/v2/files/roots", {200, 401}),
    ("GET", "/api/v2/docs", {200, 401, 404}),     # docs index optional; 404 OK
    # ── observability / journal ───────────────
    ("GET", "/api/v2/events?limit=200", {200, 401}),
    ("GET", "/api/v2/journal", {200, 401, 503}),
    ("GET", "/api/v2/profiles/active", {200, 401, 404}),  # may not be set
    ("GET", "/api/v2/analytics?days=7", {200, 401, 503}),
    ("GET", "/api/v2/logs?file=daemon&lines=20", {200, 401, 404}),
    # ── R2 HTN + R5 SuggestionInbox + R6 Mind panels (2026-05-10) ──
    # The plan endpoint requires an LLM; without one wired the test
    # daemon returns 503 — accept that. With LLM 200; bad body 400.
    ("POST", "/api/v2/cognition/goals/plan", {200, 400, 503}),
    ("GET", "/api/v2/cognition/suggestions", {200, 503}),
    ("GET", "/api/v2/cognition/suggestions?status=pending", {200, 400, 503}),
    ("GET", "/api/v2/cognition/suggestions?status=all", {200, 503}),
    # InnerMonologue panel just hits /events with R1+R3 type filters,
    # already covered above. Duplicate one here so the inventory is
    # explicit about the R6 mind tab.
    (
        "GET",
        "/api/v2/events?types=inner_monologue,reflection_cycle_ran&limit=200",
        {200, 401},
    ),
    # ── 2026-05-10 P2 (3): aggregated evolution-chain feed ────
    ("GET", "/api/v2/evolution/proposals", {200, 401}),
    ("GET", "/api/v2/evolution/proposals?since=0&limit=10", {200, 401}),
]


# React Mission Control inventory extracted from ``webui/src``. Dynamic
# resource URLs allow 404 because the handler may legitimately report "not
# found" for the synthetic id; the canary still rejects 405 and unexpected 5xx.
REACT_UI_ENDPOINT_INVENTORY: list[tuple[str, str, set[int], dict | None]] = [
    ("GET", "/api/v2/pair", {200}, None),
    ("GET", "/api/v2/status", {200, 401}, None),
    ("GET", "/api/v2/tasks?limit=100", {200, 401}, None),
    ("GET", "/api/v2/sessions/demo-session", {200, 401, 404}, None),
    ("DELETE", "/api/v2/sessions/demo-session", {200, 401, 404}, None),
    ("GET", "/api/v2/pending_questions", {200, 401}, None),
    ("GET", "/api/v2/pending_fanout_reviews", {200, 401}, None),
    ("GET", "/api/v2/llm/profiles", {200, 401}, None),
    ("POST", "/api/v2/voice/tts", {200, 400, 401, 422, 503}, {"text": "hello"}),
    ("GET", "/api/v2/system/health", {200, 401, 503}, None),
    ("GET", "/api/v2/logs?limit=120", {200, 401, 404}, None),
    ("GET", "/api/v2/skills", {200, 401}, None),
    ("GET", "/api/v2/evolution/snapshot", {200, 401, 503}, None),
    ("GET", "/api/v2/skills/demo-skill/history", {200, 401, 404}, None),
    ("POST", "/api/v2/skills/demo-skill/rollback", {200, 400, 401, 404, 422}, {"version": 1}),
    ("PATCH", "/api/v2/llm/profiles/demo-profile/enabled", {200, 400, 401, 404, 422}, {"enabled": True}),
    ("GET", "/api/v2/session_workspaces/demo-session/commits", {200, 401, 404, 503}, None),
    ("GET", "/api/v2/session_workspaces/demo-session/diff?commit=demo", {200, 400, 401, 404, 503}, None),
    ("GET", "/api/v2/session_workspaces/demo-session/tree", {200, 401, 404, 503}, None),
    ("GET", "/api/v2/session_workspaces/demo-session/file?path=README.md", {200, 400, 401, 404, 503}, None),
    ("GET", "/api/v2/session_workspaces/demo-session/raw?path=README.md", {200, 400, 401, 404, 503}, None),
    ("GET", "/api/v2/memory/v2/overview", {200, 401, 503}, None),
    ("GET", "/api/v2/memory/v2/facts?limit=80&include_superseded=true", {200, 401, 503}, None),
    ("POST", "/api/v2/memory/v2/facts/demo-fact/promote", {200, 400, 401, 404, 422, 503}, {}),
    ("POST", "/api/v2/memory/v2/facts/demo-fact/archive", {200, 400, 401, 404, 422, 503}, {}),
    ("GET", "/api/v2/memory/v2/graph?limit=80", {200, 401, 503}, None),
    ("GET", "/api/v2/memory/v2/graph_positions", {200, 401, 503}, None),
    ("GET", "/api/v2/cognition/state", {200, 503}, None),
    ("GET", "/api/v2/cognition/tasks", {200, 503}, None),
    ("GET", "/api/v2/cognition/tasks/graph-state", {200, 503}, None),
    ("GET", "/api/v2/cron", {200, 401}, None),
    ("POST", "/api/v2/cron", {200, 400, 401, 422}, {"name": "demo"}),
    ("POST", "/api/v2/cron/demo-job/pause", {200, 400, 401, 404, 422}, {}),
    ("POST", "/api/v2/cron/demo-job/resume", {200, 400, 401, 404, 422}, {}),
    ("POST", "/api/v2/cron/demo-job/trigger", {200, 400, 401, 404, 422}, {}),
    ("DELETE", "/api/v2/cron/demo-job", {200, 401, 404}, None),
    ("POST", "/api/v2/llm/endpoints/discover", {200, 400, 401, 422, 503}, {}),
    ("POST", "/api/v2/llm/endpoints/probe_vision", {200, 400, 401, 422, 503}, {}),
    ("POST", "/api/v2/llm/endpoints/hotload", {200, 400, 401, 422, 503}, {}),
    ("POST", "/api/v2/llm/profiles", {200, 400, 401, 422}, {}),
    ("DELETE", "/api/v2/llm/profiles/demo-profile", {200, 401, 404}, None),
    ("GET", "/api/v2/files/roots", {200, 401}, None),
    ("GET", "/api/v2/files?path=.", {200, 400, 401, 404}, None),
    ("PUT", "/api/v2/files", {200, 400, 401, 403, 422}, {"path": "demo.txt", "content": "hello"}),
]


@pytest.fixture
def smoke_client() -> TestClient:
    """Boot the daemon with cognition enabled + minimal scheduler/state
    so cognition routes don't 503 in the test. Auth is OFF (no token
    plumbed) so endpoints will either:
    - resolve and return 200 / 503 (good), or
    - return 401 (auth-gated; we accept since route resolved)
    """
    bus = InProcessEventBus()
    app = create_app(
        bus=bus,
        config={
            "cognition": {
                "enabled": True,
                "continuous_loop": {"enabled": False},
            },
        },
    )
    # Minimal cognition fakes so routes don't all 503.
    fake_state = MagicMock()
    fake_state.current_goals = []
    fake_state.attention_focus = []
    fake_state.fatigue = {}
    fake_state.salience_threshold = 0.3
    fake_state.attention_capacity = 7
    app.state.cognitive_state = fake_state
    fake_sched = MagicMock()
    from unittest.mock import AsyncMock
    fake_sched.list_tasks = AsyncMock(return_value=[])
    fake_sched.get_task = AsyncMock(return_value=None)
    fake_sched.snapshot_graph_state = AsyncMock(
        return_value=GraphState(thread_id="smoke", run_id="smoke")
    )
    app.state.task_scheduler = fake_sched
    fake_evol = MagicMock()
    # cognition router calls .list_pending() — match the real surface
    fake_evol.list_pending = AsyncMock(return_value=[])
    fake_evol.list_proposals = AsyncMock(return_value=[])
    app.state.evolution_loop = fake_evol
    fake_graph = MagicMock()
    fake_graph.stats = AsyncMock(return_value={"nodes": 0, "edges": 0})
    app.state.memory_graph = fake_graph
    return TestClient(app)


def _failure_kind(status: int, expected: set[int]) -> str | None:
    """Return failure label or None if status is in expected set."""
    if status in expected:
        return None
    if status == 404:
        return "ROUTE_MISMATCH (404)"
    if status == 405:
        return "METHOD_MISMATCH (405)"
    if 500 <= status < 600 and status != 503:
        return f"HANDLER_CRASH ({status})"
    return f"UNEXPECTED ({status})"


def test_smoke_every_ui_url_resolves(smoke_client: TestClient) -> None:
    """Every URL the static/pages/*.js calls must reach a handler
    (200 OK / 401 auth / 503 not-wired all acceptable). 404 / 500 /
    405 are bugs."""
    failures: list[tuple[str, str, int, str]] = []
    for method, url, expected_set in UI_ENDPOINT_INVENTORY:
        if method == "GET":
            r = smoke_client.get(url)
        elif method == "POST":
            r = smoke_client.post(url, json={})
        else:
            r = smoke_client.request(method, url)
        kind = _failure_kind(r.status_code, expected_set)
        if kind is not None:
            failures.append((method, url, r.status_code, kind))
    assert not failures, (
        "UI endpoint inventory has broken routes (front-back smoke):\n  "
        + "\n  ".join(
            f"{m} {url} → {st} [{kind}]"
            for m, url, st, kind in failures
        )
        + "\n\nFix each per its kind:\n"
        "  - ROUTE_MISMATCH (404): check route order, prefix, slug\n"
        "  - METHOD_MISMATCH (405): frontend method ≠ backend decorator\n"
        "  - HANDLER_CRASH (5xx≠503): inspect handler stack trace\n"
        "  - UNEXPECTED: check expected-status-set in inventory"
    )


def _request_inventory_entry(
    client: TestClient,
    method: str,
    url: str,
    body: dict | None,
):
    if method == "GET":
        return client.get(url)
    if method == "POST":
        return client.post(url, json=body or {})
    if method == "PATCH":
        return client.patch(url, json=body or {})
    if method == "PUT":
        return client.put(url, json=body or {})
    if method == "DELETE":
        return client.delete(url)
    return client.request(method, url, json=body)


def test_smoke_every_react_mission_control_url_resolves(smoke_client: TestClient) -> None:
    """Every API URL used by ``webui/src`` should reach a backend handler."""
    failures: list[tuple[str, str, int, str]] = []
    for method, url, expected_set, body in REACT_UI_ENDPOINT_INVENTORY:
        r = _request_inventory_entry(smoke_client, method, url, body)
        kind = _failure_kind(r.status_code, expected_set)
        if kind is not None:
            failures.append((method, url, r.status_code, kind))
    assert not failures, (
        "React Mission Control endpoint inventory has broken routes:\n  "
        + "\n  ".join(
            f"{m} {url} -> {st} [{kind}]"
            for m, url, st, kind in failures
        )
    )
