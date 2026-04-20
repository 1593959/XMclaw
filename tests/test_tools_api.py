"""HTTP tests for the /api/tools catalog endpoint.

Pins the contract the web UI relies on:

* returns ``{tools: [...], total: N}``
* each tool has ``name``, ``description``, ``parameters``, ``source``
* ``source`` is one of ``builtin``/``skill``/``plugin`` — skills are
  anything whose name begins with ``skill_``
* built-in entries sort ahead of skills/plugins
* responds (with empty list) even if the orchestrator hasn't fully
  initialized — the UI shouldn't 500 on a fresh install
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon import server as server_module
from xmclaw.daemon.server import app


def test_list_tools_shape():
    client = TestClient(app)
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    assert "total" in body
    assert isinstance(body["tools"], list)
    assert body["total"] == len(body["tools"])


def test_list_tools_entry_fields():
    client = TestClient(app)
    body = client.get("/api/tools").json()
    if not body["tools"]:
        pytest.skip("orchestrator has no tools loaded in this test env")
    entry = body["tools"][0]
    assert "name" in entry
    assert "description" in entry
    assert "parameters" in entry
    assert "source" in entry
    assert entry["source"] in {"builtin", "skill", "plugin"}


def test_list_tools_empty_when_orchestrator_missing(monkeypatch):
    """If orchestrator.tools is None (fresh process before load_all),
    the endpoint must respond with an empty list rather than 500."""
    original = server_module.orchestrator.tools
    try:
        server_module.orchestrator.tools = None
        client = TestClient(app)
        r = client.get("/api/tools")
        assert r.status_code == 200
        assert r.json() == {"tools": [], "total": 0}
    finally:
        server_module.orchestrator.tools = original


def test_builtin_tools_sort_first():
    """UI grouping relies on built-ins showing up ahead of skills and
    plugins so the most common agent tools are immediately visible."""
    client = TestClient(app)
    body = client.get("/api/tools").json()
    if not body["tools"]:
        pytest.skip("orchestrator has no tools loaded in this test env")
    seen_non_builtin = False
    for t in body["tools"]:
        if t["source"] != "builtin":
            seen_non_builtin = True
        elif seen_non_builtin:
            pytest.fail(f"built-in {t['name']} appeared after non-builtin tools")
