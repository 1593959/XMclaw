"""Tests for xmclaw.cli.session_report and its ``xmclaw session`` typer group."""
from __future__ import annotations

import json
import sqlite3
import time

from typer.testing import CliRunner

from xmclaw.cli.main import app as cli_app
from xmclaw.cli.session_report import (
    SessionReportGenerator,
    format_markdown,
    run_session_list,
    run_session_report,
)


def _make_db(tmp_path):
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        correlation_id TEXT,
        parent_id TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        started_ts REAL NOT NULL,
        last_ts REAL NOT NULL,
        event_count INTEGER NOT NULL DEFAULT 0
    );
    """)
    return conn, db


def _insert_event(conn, session_id, agent_id, etype, payload, ts=None):
    ts = ts or time.time()
    conn.execute(
        "INSERT INTO events (id, ts, session_id, agent_id, type, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (f"evt-{ts}", ts, session_id, agent_id, etype, json.dumps(payload)),
    )
    conn.execute(
        """INSERT INTO sessions (session_id, agent_id, started_ts, last_ts, event_count)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(session_id) DO UPDATE SET
            last_ts = excluded.last_ts,
            event_count = event_count + 1""",
        (session_id, agent_id, ts, ts),
    )


class TestSessionReportGenerator:
    def test_turn_splitting(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "user_message", {"content": "hello"}, base)
        _insert_event(conn, "sess-1", "agent", "llm_response", {"content": "hi", "prompt_tokens": 10, "completion_tokens": 5, "latency_ms": 100}, base + 1)
        _insert_event(conn, "sess-1", "agent", "user_message", {"content": "bye"}, base + 2)
        _insert_event(conn, "sess-1", "agent", "llm_response", {"content": "cya", "prompt_tokens": 8, "completion_tokens": 3, "latency_ms": 80}, base + 3)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        assert report is not None
        assert len(report.turns) == 2
        assert report.turns[0].user_message == "hello"
        assert report.turns[1].user_message == "bye"

    def test_grader_extraction(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "user_message", {"content": "do X"}, base)
        _insert_event(conn, "sess-1", "agent", "llm_response", {"content": "ok"}, base + 1)
        _insert_event(conn, "sess-1", "agent", "grader_verdict", {"score": 0.85, "quality": "good", "side_effect_observable": True}, base + 2)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        assert report.turns[0].grader is not None
        assert report.turns[0].grader.score == 0.85
        assert report.turns[0].grader.side_effect_observable is True

    def test_cost_aggregation(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "cost_tick", {"cost_usd": 0.001}, base)
        _insert_event(conn, "sess-1", "agent", "cost_tick", {"cost_usd": 0.002}, base + 1)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        assert report.cost_summary["total_usd"] == 0.003

    def test_evolution_events(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "skill_promoted", {"skill_name": "github-code-review", "version": 3}, base)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        assert len(report.evolution_events) == 1
        assert report.evolution_events[0].type == "skill_promoted"

    def test_tool_invocations(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "user_message", {"content": "read file"}, base)
        _insert_event(conn, "sess-1", "agent", "tool_invocation_finished", {"name": "file_read", "args": {"path": "/tmp/a.txt"}, "ok": True, "latency_ms": 12}, base + 1)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        assert len(report.turns[0].tools) == 1
        assert report.turns[0].tools[0].name == "file_read"
        assert report.turns[0].tools[0].ok is True

    def test_list_recent(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        for i in range(5):
            _insert_event(conn, f"sess-{i}", "agent", "user_message", {"content": str(i)}, base + i)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        recent = gen.list_recent(limit=3)
        assert len(recent) == 3
        # newest first
        assert recent[0]["session_id"] == "sess-4"

    def test_format_markdown(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-1", "agent", "user_message", {"content": "hello"}, base)
        _insert_event(conn, "sess-1", "agent", "llm_response", {"content": "hi", "prompt_tokens": 10, "completion_tokens": 5, "latency_ms": 100}, base + 1)
        conn.commit()
        conn.close()

        gen = SessionReportGenerator(db)
        report = gen.generate("sess-1")
        md = format_markdown(report)
        assert "Session Report: sess-1" in md
        assert "Turn 1" in md
        assert "hello" in md

    def test_missing_session_returns_none(self, tmp_path):
        db = tmp_path / "events.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE events (id TEXT PRIMARY KEY, ts REAL, session_id TEXT, agent_id TEXT, type TEXT, payload TEXT);
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, agent_id TEXT, started_ts REAL, last_ts REAL, event_count INTEGER);
        """)
        conn.close()

        gen = SessionReportGenerator(db)
        assert gen.generate("nonexistent") is None


# ── run_* entry points (pre-typer) ─────────────────────────────────────────


class TestRunEntryPoints:
    def test_run_session_report_ok_markdown(self, tmp_path, capsys):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-md", "agent", "user_message", {"content": "hi"}, base)
        _insert_event(conn, "sess-md", "agent", "llm_response", {"content": "ok"}, base + 1)
        conn.commit()
        conn.close()

        rc = run_session_report("sess-md", db=db, as_json=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Session Report: sess-md" in out
        assert "Turn 1" in out

    def test_run_session_report_ok_json(self, tmp_path, capsys):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-js", "agent", "user_message", {"content": "hi"}, base)
        conn.commit()
        conn.close()

        rc = run_session_report("sess-js", db=db, as_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["session_id"] == "sess-js"
        assert payload["turns"][0]["user_message"] == "hi"

    def test_run_session_report_unknown_session_exits_1(self, tmp_path, capsys):
        conn, db = _make_db(tmp_path)
        conn.commit()
        conn.close()

        rc = run_session_report("does-not-exist", db=db)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_run_session_report_missing_db_exits_1(self, tmp_path, capsys):
        rc = run_session_report("sess-1", db=tmp_path / "nope.db")
        assert rc == 1
        err = capsys.readouterr().err
        assert "no event log" in err

    def test_run_session_list_populated(self, tmp_path, capsys):
        conn, db = _make_db(tmp_path)
        base = time.time()
        for i in range(3):
            _insert_event(conn, f"sess-{i}", "agent", "user_message", {"content": str(i)}, base + i)
        conn.commit()
        conn.close()

        rc = run_session_list(db=db, limit=10, as_json=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "sess-0" in out
        assert "sess-2" in out

    def test_run_session_list_empty_db_prints_notice(self, tmp_path, capsys):
        # An existing but empty sessions table should say "no sessions
        # recorded yet" rather than emitting a header with zero rows.
        conn, db = _make_db(tmp_path)
        conn.commit()
        conn.close()

        rc = run_session_list(db=db)
        assert rc == 0
        assert "no sessions recorded yet" in capsys.readouterr().out

    def test_run_session_list_json_shape(self, tmp_path, capsys):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-j1", "agent", "user_message", {"content": "x"}, base)
        conn.commit()
        conn.close()

        rc = run_session_list(db=db, as_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload[0]["session_id"] == "sess-j1"
        assert payload[0]["event_count"] == 1


# ── typer CLI integration (CliRunner) ──────────────────────────────────────


class TestSessionCli:
    def test_cli_report_markdown(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-cli", "agent", "user_message", {"content": "hey"}, base)
        conn.commit()
        conn.close()

        result = CliRunner().invoke(
            cli_app, ["session", "report", "sess-cli", "--db", str(db)]
        )
        assert result.exit_code == 0, result.output
        assert "Session Report: sess-cli" in result.output

    def test_cli_report_json(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-cli-json", "agent", "user_message", {"content": "x"}, base)
        conn.commit()
        conn.close()

        result = CliRunner().invoke(
            cli_app,
            ["session", "report", "sess-cli-json", "--db", str(db), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["session_id"] == "sess-cli-json"

    def test_cli_report_unknown_session_exits_1(self, tmp_path):
        conn, db = _make_db(tmp_path)
        conn.commit()
        conn.close()

        result = CliRunner().invoke(
            cli_app, ["session", "report", "nope", "--db", str(db)]
        )
        assert result.exit_code == 1

    def test_cli_list_populated(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        _insert_event(conn, "sess-cli-list", "agent", "user_message", {"content": "x"}, base)
        conn.commit()
        conn.close()

        result = CliRunner().invoke(
            cli_app, ["session", "list", "--db", str(db)]
        )
        assert result.exit_code == 0, result.output
        assert "sess-cli-list" in result.output

    def test_cli_list_limit_flag(self, tmp_path):
        conn, db = _make_db(tmp_path)
        base = time.time()
        for i in range(5):
            _insert_event(conn, f"sess-{i}", "agent", "user_message", {"content": str(i)}, base + i)
        conn.commit()
        conn.close()

        result = CliRunner().invoke(
            cli_app, ["session", "list", "--db", str(db), "-n", "2"]
        )
        assert result.exit_code == 0, result.output
        # Only the 2 most recent sessions should appear in the body.
        assert "sess-4" in result.output
        assert "sess-3" in result.output
        assert "sess-0" not in result.output
