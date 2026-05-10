"""Per-page front-back integration tests.

Each page section asserts:
1. The exact URLs the page calls all resolve (no 404 / 5xx / 405)
2. The response shape matches what the page's reducer expects
3. Edge cases: empty state, auth absent, malformed body

This file complements ``test_v2_ui_endpoint_smoke.py`` (URL-resolution
canary across all pages). Where smoke says "route resolves", this
says "the response is also what the UI actually parses without
crashing the reducer".

**Standing rule (post-2026-05-09)**: tests for cross-cutting features
that span frontend + backend MUST exercise the full HTTP path the
frontend actually uses. Pure router inspection misses real-world
matching bugs (e.g. ``/tasks/graph`` shadowed by ``/tasks/{task_id}``),
prefix collisions, and request validation mismatches.

Pages covered (one ``Test*Page`` class each):
  * Skills        → ``/api/v2/skills``, ``/api/v2/skills/installed``
  * Marketplace   → ``/api/v2/skills/marketplace``, ``.../install``,
                    ``.../installed/{id}`` DELETE
  * Tools         → ``/api/v2/status`` (consumed for ``tools`` +
                    ``mcp_servers`` keys)
  * Workspace     → ``/api/v2/workspace``, ``/api/v2/files/roots``,
                    ``/api/v2/profiles/active``
  * Sessions      → ``/api/v2/sessions``, ``/api/v2/sessions/search``
  * Cron          → ``/api/v2/cron``

All cases use a fresh ``TestClient(create_app(...))`` per the canonical
pattern in ``tests/unit/test_v2_cognition_router_order.py`` — no real
network, no real disk-state mutation across tests (data dirs isolated
under ``tmp_path`` via ``XMC_DATA_DIR``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


# ── shared fixtures ─────────────────────────────────────────────────


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin every XMclaw data location into a per-test tmp dir so tests
    don't read/write the real ``~/.xmclaw`` and don't bleed into each
    other through ``sessions.db`` / ``cron/jobs.json`` / etc."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XMC_V2_USER_SKILLS_DIR", str(tmp_path / "skills_user"))
    return tmp_path


@pytest.fixture
def base_client(isolated_data_dir: Path) -> TestClient:
    """Plain ``TestClient`` with no extra orchestrator/scheduler/etc.
    state. Pages that require richer state build their own client via
    ``_build_client`` below — this fixture is for the simplest cases."""
    bus = InProcessEventBus()
    return TestClient(create_app(bus=bus, config={}))


def _build_client(
    *,
    config: dict[str, Any] | None = None,
    state_overrides: dict[str, Any] | None = None,
) -> TestClient:
    """Helper: create_app + optional ``app.state.X`` injections."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config=config or {})
    for k, v in (state_overrides or {}).items():
        setattr(app.state, k, v)
    return TestClient(app)


# ── Skills page ─────────────────────────────────────────────────────


class TestSkillsPage:
    """Skills.js calls ``GET /api/v2/skills`` to populate ``skills``
    and ``pending_restarts``. The reducer reads ``d.skills``,
    ``d.pending_restarts`` (see Skills.js lines ~149-159)."""

    def test_skills_list_returns_array(self, base_client: TestClient) -> None:
        r = base_client.get("/api/v2/skills")
        assert r.status_code == 200, r.text
        body = r.json()
        # Reducer reads d.skills — must be a list (empty OK on no orch).
        assert "skills" in body
        assert isinstance(body["skills"], list)
        # Pre-B-341 the page would crash on missing pending_restarts —
        # post-fix the key is always present.
        assert "pending_restarts" in body
        assert isinstance(body["pending_restarts"], list)

    def test_skills_list_with_orchestrator_shapes_versions(
        self, isolated_data_dir: Path,
    ) -> None:
        """When orchestrator IS wired, each skill row must carry the
        keys the Skills.js render uses: ``id``, ``head_version``,
        ``source``, ``versions`` with ``version``/``is_head``/``manifest``."""
        # Build a fake registry so the route hits the populated branch.
        manifest = MagicMock()
        manifest.to_dict = MagicMock(
            return_value={"description": "fake summarize"},
        )
        manifest.created_by = "human"
        ref = MagicMock()
        ref.manifest = manifest
        skill_obj = MagicMock()
        skill_obj.__class__ = type(
            "FakeSkillCls", (), {"__module__": "xmclaw.skills.fake"},
        )
        registry = MagicMock()
        registry.list_skill_ids = MagicMock(return_value=["summarize"])
        registry.active_version = MagicMock(return_value=1)
        registry.list_versions = MagicMock(return_value=[1])
        registry.ref = MagicMock(return_value=ref)
        registry.get = MagicMock(return_value=skill_obj)
        orch = MagicMock()
        orch.registry = registry

        client = _build_client(state_overrides={"orchestrator": orch})
        r = client.get("/api/v2/skills")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["evolution_enabled"] is True
        assert body["skills"], "expected the seeded summarize skill"
        row = body["skills"][0]
        assert row["id"] == "summarize"
        assert row["head_version"] == 1
        assert "source" in row
        assert isinstance(row["versions"], list)
        v0 = row["versions"][0]
        assert v0["version"] == 1
        assert v0["is_head"] is True
        assert "manifest" in v0

    def test_skills_installed_shape(self, base_client: TestClient) -> None:
        """Marketplace / Skills page also calls
        ``GET /api/v2/skills/installed``. Reducer reads ``r.skills``
        and indexes by ``id``."""
        r = base_client.get("/api/v2/skills/installed")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert "skills" in body
        assert isinstance(body["skills"], list)


# ── Marketplace page (B-390) ─────────────────────────────────────────


class TestMarketplacePage:
    """Marketplace.js wires three URLs (loadAll + onInstall + onRemove):
      * ``GET /api/v2/skills/marketplace``  — index
      * ``GET /api/v2/skills/installed``    — installed map
      * ``POST /api/v2/skills/install``     body ``{id}``
      * ``DELETE /api/v2/skills/installed/{id}``

    Reducer reads ``mk.index.skills`` and ``inst.skills``."""

    def test_marketplace_index_loads(self, base_client: TestClient) -> None:
        r = base_client.get("/api/v2/skills/marketplace")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert "index" in body
        idx = body["index"]
        # Marketplace.js does ``mk.index.skills`` — that key MUST be
        # a list (even if empty) for the reducer not to crash.
        assert "skills" in idx
        assert isinstance(idx["skills"], list)

    def test_marketplace_refresh_flag_is_accepted(
        self, base_client: TestClient,
    ) -> None:
        """``?refresh=1`` is the cache-bust path the UI hits when the
        user clicks "刷新"; route must accept it without 422."""
        r = base_client.get("/api/v2/skills/marketplace?refresh=1")
        assert r.status_code == 200, r.text

    def test_marketplace_install_validates_id(
        self, base_client: TestClient,
    ) -> None:
        """Empty body → 400 with ``error_code='missing_id'``. Reducer
        reads ``r.error`` for the toast."""
        r = base_client.post("/api/v2/skills/install", json={})
        assert r.status_code == 400
        body = r.json()
        assert body.get("ok") is False
        assert body.get("error_code") == "missing_id"
        assert "error" in body

    def test_marketplace_install_invalid_body_400(
        self, base_client: TestClient,
    ) -> None:
        """Body that isn't a JSON object → 400, not 5xx."""
        r = base_client.post(
            "/api/v2/skills/install",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        body = r.json()
        assert body.get("error_code") == "invalid_json"

    def test_marketplace_install_unknown_id_404(
        self, base_client: TestClient,
    ) -> None:
        """Asking to install a skill that isn't in the index → 404
        with ``error_code='skill_not_found'``. The frontend Marketplace
        page shows this as ``toast.error("安装失败：" + r.error)``."""
        r = base_client.post(
            "/api/v2/skills/install",
            json={"id": "definitely-not-a-real-skill-zzz"},
        )
        assert r.status_code == 404
        body = r.json()
        assert body.get("ok") is False
        assert body.get("error_code") == "skill_not_found"

    def test_marketplace_uninstall_404_on_missing(
        self, base_client: TestClient,
    ) -> None:
        """DELETE on a never-installed id → 404 with
        ``skill_not_installed`` (not 500, not silent 200)."""
        r = base_client.delete(
            "/api/v2/skills/installed/never-installed-skill-zzz",
        )
        assert r.status_code == 404
        body = r.json()
        assert body.get("error_code") == "skill_not_installed"


# ── Tools page ──────────────────────────────────────────────────────


class TestToolsPage:
    """Tools.js (only) calls ``GET /api/v2/status`` and reads
    ``data.tools`` + ``data.mcp_servers``. There is no dedicated
    ``/api/v2/tools`` route today — the page is a status-derived view.
    """

    def test_status_shape_for_tools_page(
        self, base_client: TestClient,
    ) -> None:
        r = base_client.get("/api/v2/status")
        assert r.status_code == 200, r.text
        body = r.json()
        # Tools.js reducer pulls these two keys directly. Their absence
        # would render "0 tools, 0 MCP servers" silently — but a NON-
        # array value would make ``.length`` undefined and ``.map``
        # crash. Pin both shapes.
        assert "tools" in body
        assert isinstance(body["tools"], list)
        assert "mcp_servers" in body
        assert isinstance(body["mcp_servers"], list)

    def test_status_lists_default_keys(
        self, base_client: TestClient,
    ) -> None:
        """Other Tools-adjacent keys the page will read in future
        revisions (mcp_status / sandbox_allowed_dirs) must already
        be present so the page doesn't crash on a refactor."""
        body = base_client.get("/api/v2/status").json()
        for key in ("mcp_status", "sandbox_allowed_dirs"):
            assert key in body, f"/api/v2/status missing {key!r}"


# ── Workspace page ──────────────────────────────────────────────────


class TestWorkspacePage:
    """Workspace.js loadAll() runs three GETs in parallel:
      * ``/api/v2/files/roots``    → reducer reads ``d.roots``
      * ``/api/v2/workspace``      → reducer reads ``d.roots[].path`` +
                                     ``d.primary_index``
      * ``/api/v2/profiles/active``→ reducer reads ``d.profile_id``,
                                     ``d.files`` (best-effort)

    PUT /api/v2/workspace is also wired for switch-folder; we cover
    the validation path without writing real workspace state."""

    def test_files_roots_listing_shape(
        self, base_client: TestClient,
    ) -> None:
        r = base_client.get("/api/v2/files/roots")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "roots" in body
        assert isinstance(body["roots"], list)
        # Each root row must have key/label/path/exists for the
        # BlockLink renderer (Workspace.js lines ~43-53).
        for row in body["roots"]:
            assert "key" in row
            assert "label" in row
            assert "path" in row
            assert "exists" in row
            assert isinstance(row["exists"], bool)

    def test_workspace_state_shape(
        self, base_client: TestClient,
    ) -> None:
        r = base_client.get("/api/v2/workspace")
        assert r.status_code == 200, r.text
        body = r.json()
        # Reducer reads d.roots[d.primary_index].path — both keys must
        # be present and well-typed.
        assert "roots" in body
        assert isinstance(body["roots"], list)
        assert "primary_index" in body
        assert isinstance(body["primary_index"], int)
        for r0 in body["roots"]:
            # Workspace.js inspects path / name / exists / looks_temp
            for k in ("path", "name", "exists", "looks_temp"):
                assert k in r0, f"workspace row missing {k!r}: {r0}"

    def test_profiles_active_optional_404(
        self, base_client: TestClient,
    ) -> None:
        """``/api/v2/profiles/active`` may 200 with profile data OR 404
        on a fresh install where no persona has been created yet.
        Workspace.js wraps this in ``.catch`` so either is fine — we
        just need to NOT see a 5xx."""
        r = base_client.get("/api/v2/profiles/active")
        assert r.status_code in (200, 404), (
            f"unexpected {r.status_code}: {r.text!r}"
        )
        if r.status_code == 200:
            body = r.json()
            # When present, ``profile_id`` is required (Workspace.js +
            # memory_identity panel both read it).
            assert "profile_id" in body

    def test_workspace_put_rejects_unknown_action(
        self, base_client: TestClient,
    ) -> None:
        """PUT /api/v2/workspace with bogus action → 400, not 5xx."""
        r = base_client.put(
            "/api/v2/workspace",
            json={"action": "no-such-action"},
        )
        assert r.status_code == 400
        body = r.json()
        assert "error" in body

    def test_workspace_put_add_requires_path(
        self, base_client: TestClient,
    ) -> None:
        """PUT add without path → 400 ``path required``."""
        r = base_client.put(
            "/api/v2/workspace",
            json={"action": "add"},
        )
        assert r.status_code == 400
        assert "path" in r.json().get("error", "").lower()


# ── Sessions page ───────────────────────────────────────────────────


class TestSessionsPage:
    """Sessions.js hits two URLs:
      * ``GET /api/v2/sessions?limit=200``    — main list, reads
        ``d.sessions`` and renders SessionRow cards
      * ``GET /api/v2/sessions/search?q=…&limit=50`` — debounced
        server-side search; reads ``d.sessions`` (each row has
        ``session_id`` keyed by reducer)

    ``GET /api/v2/sessions/{sid}`` is hit on expand — covered by
    ``tests/integration/test_v2_daemon_app.py`` already; we don't
    duplicate here."""

    def test_sessions_list_paginated(
        self, base_client: TestClient,
    ) -> None:
        r = base_client.get("/api/v2/sessions?limit=200")
        assert r.status_code == 200, r.text
        body = r.json()
        # Reducer reads d.sessions || [] — must be a list, no 5xx
        # even when the store is fresh-empty.
        assert "sessions" in body
        assert isinstance(body["sessions"], list)

    def test_sessions_list_default_limit(
        self, base_client: TestClient,
    ) -> None:
        """Without ``?limit=`` the route must still resolve (default
        50 in router) — guards against a refactor that makes the param
        required and silently 422s the UI."""
        r = base_client.get("/api/v2/sessions")
        assert r.status_code == 200
        assert "sessions" in r.json()

    def test_sessions_search_returns_query_echo(
        self, base_client: TestClient,
    ) -> None:
        """Search route must accept ``?q=`` and return ``sessions``
        list + echo the query (per B-339 router contract). Reducer
        in Sessions.js builds an id→row map from ``d.sessions``."""
        r = base_client.get("/api/v2/sessions/search?q=hello&limit=10")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "sessions" in body
        assert isinstance(body["sessions"], list)
        assert body.get("query") == "hello"

    def test_sessions_search_empty_query_ok(
        self, base_client: TestClient,
    ) -> None:
        """Empty query is the explicit "no results" path the router
        ships — must NOT 400. The UI's debounce skips q<2 chars but
        the route itself still needs to return cleanly."""
        r = base_client.get("/api/v2/sessions/search?q=&limit=10")
        assert r.status_code == 200
        body = r.json()
        assert body["sessions"] == []


# ── Cron page ───────────────────────────────────────────────────────


class TestCronPage:
    """Cron.js calls one URL on load:
      * ``GET /api/v2/cron`` — reducer reads ``d.jobs || []`` and
        each row's ``id`` / ``name`` / ``schedule`` / ``enabled`` /
        ``next_run_at``.

    Mutations (DELETE /{id}, POST /{id}/pause / resume / trigger,
    POST / for create) are wired in tests/integration/
    test_v2_daemon_app.py and unit/test_v2_scheduler_*. We only cover
    the LIST endpoint here since that's what the page calls on every
    nav-in."""

    def test_cron_list_returns_jobs(
        self, base_client: TestClient,
    ) -> None:
        r = base_client.get("/api/v2/cron")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "jobs" in body
        assert isinstance(body["jobs"], list)

    def test_cron_create_validates_required_fields(
        self, base_client: TestClient,
    ) -> None:
        """POST /api/v2/cron without required fields → 400, not 5xx.
        The Cron page surfaces this as a toast.error so the contract
        must be a clean 4xx with ``error`` field."""
        r = base_client.post("/api/v2/cron", json={})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_cron_delete_missing_404(
        self, base_client: TestClient,
    ) -> None:
        """DELETE on never-created job_id → 404 ``not found``."""
        r = base_client.delete("/api/v2/cron/no-such-job-zzz")
        assert r.status_code == 404
        assert r.json().get("error") == "not found"

    def test_cron_pause_missing_404(
        self, base_client: TestClient,
    ) -> None:
        """POST pause on missing job → 404; route must not crash."""
        r = base_client.post("/api/v2/cron/no-such-job-zzz/pause")
        assert r.status_code == 404


# ── Cross-page sanity: every URL the inventory points at returns
#     a JSON body the reducer would not crash on ─────────────────────


class TestEveryListEndpointReturnsJson:
    """Catch-all: every "list" GET the UI uses on first paint must
    return a JSON object (not bare list, not text/plain) — Marketplace
    and Skills both do ``await apiGet(...)`` and destructure ``r.skills``
    so a non-object response would NPE the reducer."""

    @pytest.mark.parametrize(
        "url",
        [
            "/api/v2/skills",
            "/api/v2/skills/installed",
            "/api/v2/skills/marketplace",
            "/api/v2/cron",
            "/api/v2/sessions",
            "/api/v2/files/roots",
            "/api/v2/workspace",
            "/api/v2/status",
        ],
    )
    def test_returns_json_object(
        self, base_client: TestClient, url: str,
    ) -> None:
        r = base_client.get(url)
        assert r.status_code == 200, f"{url} → {r.status_code}: {r.text!r}"
        assert r.headers.get("content-type", "").startswith(
            "application/json"
        ), f"{url} returned non-JSON content-type: {r.headers.get('content-type')!r}"
        body = r.json()
        assert isinstance(body, dict), (
            f"{url} returned non-object JSON: {type(body).__name__}"
        )
