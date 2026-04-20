"""Phase E6 regression tests: message-level user feedback (Plan v2 PR-E6-1).

Pins:

* **PR-E6-1 store** — ``SQLiteStore.upsert_user_feedback`` /
  ``get_user_feedback_by_turns`` / ``get_recent_user_feedback`` round-trip
  a 👍/👎 verdict and enforce the ``thumb`` domain.
* **PR-E6-1 api** — ``POST /api/agent/{id}/turns/{tid}/feedback`` accepts
  valid bodies, rejects invalid ``thumb`` values with 400, and emits the
  ``USER_FEEDBACK_RECORDED`` event.
* **PR-E6-1 reflection** — ``_annotate_with_user_feedback`` joins rows
  onto the in-memory turn history; ``_format_history`` renders the
  thumb + note marker; ``_summarize_user_feedback`` produces a
  top-of-prompt human-verdict block.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.memory.sqlite_store import SQLiteStore


def _unique_agent_id(tag: str) -> str:
    return f"agent_e6_{tag}_{uuid.uuid4().hex[:8]}"


def _turn_id(i: int = 0) -> str:
    return f"turn_{uuid.uuid4().hex[:10]}_{i}"


# ── Store round-trip ────────────────────────────────────────────────────────

def test_upsert_and_lookup_by_turn(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.db")
    agent_id = _unique_agent_id("store")
    tid = _turn_id()

    store.upsert_user_feedback(agent_id, tid, "up", note="helped me ship")
    fetched = store.get_user_feedback_by_turns(agent_id, [tid])
    assert tid in fetched
    assert fetched[tid]["thumb"] == "up"
    assert fetched[tid]["note"] == "helped me ship"


def test_upsert_is_last_write_wins(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.db")
    agent_id = _unique_agent_id("overwrite")
    tid = _turn_id()

    store.upsert_user_feedback(agent_id, tid, "up")
    store.upsert_user_feedback(agent_id, tid, "down", note="changed my mind")
    fetched = store.get_user_feedback_by_turns(agent_id, [tid])
    assert fetched[tid]["thumb"] == "down"
    assert fetched[tid]["note"] == "changed my mind"


def test_upsert_rejects_invalid_thumb(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.db")
    with pytest.raises(ValueError, match="thumb must be"):
        store.upsert_user_feedback("agent", "turn", "maybe")


def test_get_recent_sorts_newest_first(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.db")
    agent_id = _unique_agent_id("recent")
    # Insert out of order
    for i in range(3):
        store.upsert_user_feedback(agent_id, f"turn_{i}", "up")

    rows = store.get_recent_user_feedback(agent_id, limit=10)
    assert len(rows) == 3
    # created_at descending means rowid DESC for same-second rows.
    # The last insert must be the first row back.
    assert rows[0]["turn_id"] == "turn_2"


def test_get_recent_scopes_to_agent(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.db")
    store.upsert_user_feedback("agent_A", "tA", "up")
    store.upsert_user_feedback("agent_B", "tB", "down")

    rows_a = store.get_recent_user_feedback("agent_A")
    rows_b = store.get_recent_user_feedback("agent_B")
    assert [r["turn_id"] for r in rows_a] == ["tA"]
    assert [r["turn_id"] for r in rows_b] == ["tB"]


# ── HTTP endpoint ───────────────────────────────────────────────────────────

def test_feedback_endpoint_rejects_bad_thumb():
    from xmclaw.daemon.server import app
    agent_id = _unique_agent_id("api_bad")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/turns/turn_x/feedback",
            json={"thumb": "sideways"},
        )
        assert resp.status_code == 400
        assert "thumb" in resp.json()["error"]


def test_feedback_endpoint_rejects_non_string_note():
    from xmclaw.daemon.server import app
    agent_id = _unique_agent_id("api_note")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/turns/turn_x/feedback",
            json={"thumb": "up", "note": 42},
        )
        assert resp.status_code == 400


def test_feedback_endpoint_round_trip():
    from xmclaw.daemon.server import app, orchestrator
    agent_id = _unique_agent_id("api_rt")
    tid = _turn_id()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/turns/{tid}/feedback",
            json={"thumb": "up", "note": "fast"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["turn_id"] == tid
        assert body["thumb"] == "up"

        # Recent-feedback GET must see the same row.
        listing = client.get(f"/api/agent/{agent_id}/feedback/recent")
        assert listing.status_code == 200
        rows = listing.json()["feedback"]
        assert any(r["turn_id"] == tid and r["thumb"] == "up" for r in rows)

        # And the store is consistent directly (belt and suspenders).
        # Must stay inside the TestClient context — the lifespan exit
        # closes the daemon's SQLite connection.
        fetched = orchestrator.memory.sqlite.get_user_feedback_by_turns(
            agent_id, [tid],
        )
        assert fetched[tid]["thumb"] == "up"


# ── Reflection integration ─────────────────────────────────────────────────

class _FakeMemory:
    """Stand-in for MemoryManager that only exposes the attr reflection reads."""
    def __init__(self, store: SQLiteStore):
        self.sqlite = store


def test_reflection_annotates_history_with_feedback(tmp_path: Path):
    from xmclaw.core.reflection import ReflectionEngine

    store = SQLiteStore(tmp_path / "mem.db")
    agent_id = _unique_agent_id("refl_annotate")

    # Two turns; only one has feedback attached.
    t1 = _turn_id(1)
    t2 = _turn_id(2)
    store.upsert_user_feedback(agent_id, t2, "down", note="wrong answer")

    engine = ReflectionEngine(llm_router=None, memory=_FakeMemory(store))
    history = [
        {"user": "q1", "assistant": "a1", "tool_calls": [], "turn_id": t1},
        {"user": "q2", "assistant": "a2", "tool_calls": [], "turn_id": t2},
    ]
    annotated = engine._annotate_with_user_feedback(agent_id, history)

    assert "user_feedback" not in annotated[0]
    assert annotated[1]["user_feedback"]["thumb"] == "down"
    assert annotated[1]["user_feedback"]["note"] == "wrong answer"


def test_format_history_renders_feedback_marker(tmp_path: Path):
    from xmclaw.core.reflection import ReflectionEngine

    engine = ReflectionEngine(llm_router=None, memory=_FakeMemory(
        SQLiteStore(tmp_path / "mem.db"),
    ))
    history = [
        {
            "user": "hi", "assistant": "hello world", "tool_calls": [],
            "user_feedback": {"thumb": "up", "note": "warm"},
        },
    ]
    text = engine._format_history(history)
    assert "👍 human approved" in text
    assert '"warm"' in text


def test_summarize_user_feedback_counts_and_hints():
    from xmclaw.core.reflection import ReflectionEngine

    history = [
        {"user": "a", "assistant": "x", "tool_calls": [],
         "user_feedback": {"thumb": "up"}},
        {"user": "b", "assistant": "y", "tool_calls": [],
         "user_feedback": {"thumb": "down", "note": "hallucination"}},
        {"user": "c", "assistant": "z", "tool_calls": [],
         "user_feedback": {"thumb": "down"}},
    ]
    summary = ReflectionEngine._summarize_user_feedback(history)
    assert "1 个 👍" in summary
    assert "2 个 👎" in summary
    assert "下行反馈居多" in summary
    assert "hallucination" in summary


def test_summarize_empty_when_no_feedback():
    from xmclaw.core.reflection import ReflectionEngine

    history = [
        {"user": "a", "assistant": "x", "tool_calls": []},
        {"user": "b", "assistant": "y", "tool_calls": []},
    ]
    assert ReflectionEngine._summarize_user_feedback(history) == ""


# ── PR-E6-3: SOUL/PROFILE/AGENTS md editor API ─────────────────────────────

def test_md_read_and_write_round_trip():
    """PUT must persist to disk and GET must see the new content."""
    from xmclaw.daemon.server import app
    with TestClient(app) as client:
        original = client.get("/api/agent/default/md/profile").json()
        assert original["kind"] == "profile"

        new_content = "# Profile override test\nhello"
        resp = client.put(
            "/api/agent/default/md/profile",
            json={"content": new_content},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        fetched = client.get("/api/agent/default/md/profile").json()
        assert fetched["content"] == new_content

        # Restore to avoid polluting the workspace copy.
        client.put(
            "/api/agent/default/md/profile",
            json={"content": original.get("content", "")},
        )


def test_md_rejects_unknown_kind():
    from xmclaw.daemon.server import app
    with TestClient(app) as client:
        resp = client.get("/api/agent/default/md/secrets")
        assert resp.status_code == 400


def test_md_rejects_missing_agent():
    from xmclaw.daemon.server import app
    with TestClient(app) as client:
        resp = client.get("/api/agent/does_not_exist_12345/md/soul")
        assert resp.status_code == 400


def test_md_rejects_non_string_content():
    from xmclaw.daemon.server import app
    with TestClient(app) as client:
        resp = client.put(
            "/api/agent/default/md/profile",
            json={"content": ["not", "a", "string"]},
        )
        assert resp.status_code == 400


def test_md_rejects_oversized_content():
    from xmclaw.daemon.server import app
    with TestClient(app) as client:
        resp = client.put(
            "/api/agent/default/md/profile",
            json={"content": "a" * (100 * 1024 + 1)},
        )
        assert resp.status_code == 400
