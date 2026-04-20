"""HTTP tests for the memory view endpoints.

Pins:

* ``GET /api/agent/{id}/sessions`` lists JSONL files newest-first with
  size and preview.
* ``GET /api/agent/{id}/session`` reads a single file with paging, rejects
  anything not matching the filename whitelist (path-traversal defense),
  and 404s on missing files.
* ``GET /api/agent/{id}/insights`` returns rows from SQLite or an empty
  list when memory isn't initialized — it must not 500.

Sessions currently live under ``agents/default/memory/sessions`` because
of the hard-coded agent in ``MemoryManager`` (tracked as a known bug);
these tests write to that real location under unique filenames per run
so they don't step on production data.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.server import app
from xmclaw.utils.paths import get_agent_dir


def _write_session(name: str, turns: list[dict]) -> Path:
    d = get_agent_dir("default") / "memory" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def unique_session():
    name = f"test_mv_{uuid.uuid4().hex[:8]}.jsonl"
    yield name
    try:
        (get_agent_dir("default") / "memory" / "sessions" / name).unlink()
    except FileNotFoundError:
        pass


def test_list_sessions_sorted_by_mtime(unique_session):
    """The newest-modified file must appear before older ones — filename
    sort used to put ``test_vec.jsonl`` above ``default.jsonl`` even when
    default was actively in use."""
    older = f"older_{uuid.uuid4().hex[:8]}.jsonl"
    try:
        _write_session(older, [{"user": "old", "assistant": "old-a"}])
        # Force the older file's mtime to be clearly in the past so a
        # same-second write doesn't tie with the newer one.
        older_path = get_agent_dir("default") / "memory" / "sessions" / older
        past = time.time() - 3600
        import os
        os.utime(older_path, (past, past))

        _write_session(unique_session, [{"user": "new", "assistant": "new-a"}])

        client = TestClient(app)
        r = client.get("/api/agent/default/sessions")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["sessions"]]
        assert unique_session in names
        assert older in names
        assert names.index(unique_session) < names.index(older)
    finally:
        try:
            (get_agent_dir("default") / "memory" / "sessions" / older).unlink()
        except FileNotFoundError:
            pass


def test_list_sessions_includes_size(unique_session):
    _write_session(unique_session, [{"user": "x" * 50, "assistant": "y" * 50}])
    client = TestClient(app)
    data = client.get("/api/agent/default/sessions").json()
    entry = next((s for s in data["sessions"] if s["name"] == unique_session), None)
    assert entry is not None
    assert entry["size"] > 0
    assert entry["turn_count"] == 1


def test_read_session_happy_path(unique_session):
    turns = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(5)]
    _write_session(unique_session, turns)
    client = TestClient(app)
    r = client.get(f"/api/agent/default/session?name={unique_session}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["records"]) == 5
    assert body["records"][0]["user"] == "q0"


def test_read_session_paging(unique_session):
    turns = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(10)]
    _write_session(unique_session, turns)
    client = TestClient(app)
    r = client.get(f"/api/agent/default/session?name={unique_session}&offset=3&limit=4")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10
    assert body["offset"] == 3
    assert len(body["records"]) == 4
    assert body["records"][0]["user"] == "q3"
    assert body["records"][-1]["user"] == "q6"


def test_read_session_paging_clamp(unique_session):
    turns = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(3)]
    _write_session(unique_session, turns)
    client = TestClient(app)
    # offset past end must not explode — the API clamps to [0, total].
    r = client.get(f"/api/agent/default/session?name={unique_session}&offset=99&limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["records"] == []


@pytest.mark.parametrize("bad_name", [
    "../agent.json",
    "..\\agent.json",
    "foo/bar.jsonl",
    "foo.json",          # wrong extension
    "foo bar.jsonl",     # whitespace isn't in the whitelist
    "",
])
def test_read_session_rejects_unsafe_names(bad_name):
    """Anything not matching the filename whitelist must 400, not leak a
    file off the sessions dir."""
    client = TestClient(app)
    r = client.get(f"/api/agent/default/session?name={bad_name}")
    assert r.status_code == 400


def test_read_session_missing():
    client = TestClient(app)
    r = client.get(f"/api/agent/default/session?name=does_not_exist_{uuid.uuid4().hex}.jsonl")
    assert r.status_code == 404


def test_insights_endpoint_shape():
    """Endpoint must return ``{insights, total}`` even when memory hasn't
    been initialized — we don't want the UI blowing up before the first
    reflection run."""
    client = TestClient(app)
    r = client.get("/api/agent/default/insights?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "insights" in body
    assert "total" in body
    assert isinstance(body["insights"], list)
    assert body["total"] == len(body["insights"])


def test_insights_limit_clamp():
    """``limit`` is clamped to [1, 500] so a huge value doesn't scan the
    whole table."""
    client = TestClient(app)
    r = client.get("/api/agent/default/insights?limit=9999")
    assert r.status_code == 200
    # No assertion on count — just that it doesn't 500 or hang.
