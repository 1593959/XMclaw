"""Tests for `xmclaw evo replay` / `xmclaw evo cycles` (PR-E7-2).

These are post-mortem tools. Operators fire them cold, usually weeks after
the cycle was recorded, so the acceptance bar is:

- **No surprises.** An unknown cycle id must exit non-zero with a clear
  message, not tracebacks.
- **JSON is machine-parsable.** The ``--json`` flag is the contract tests,
  dashboards, and downstream tooling rely on — it must round-trip the
  cycle and lineage rows intact.
- **Plain-text is greppable.** The default rendering must include the
  cycle id, verdict, and every artifact id so grep and copy-paste work.

We bypass the shared singleton journal cache by constructing a throwaway
SQLiteStore and patching ``get_journal`` — the CLI would otherwise pick
up the real ``shared/memory.db`` and leak state between tests.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from xmclaw.cli.main import app
from xmclaw.evolution.journal import (
    CYCLE_PASSED,
    EvolutionJournal,
    KIND_SKILL,
    STATUS_PROMOTED,
)
from xmclaw.memory.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Every CliRunner.invoke in this file triggers ``asyncio.run`` in the
    CLI command, which calls ``set_event_loop(None)`` on exit. Later test
    modules still use the (deprecated) ``asyncio.get_event_loop()`` pattern
    and will raise "no current event loop" if we leave the thread bare.

    Reinstate a fresh loop after every test as a blanket safety net.
    """
    yield
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass


@pytest.fixture
def seeded_journal(tmp_path):
    """Build a journal with one complete cycle and one promoted artifact.

    Yielding the agent_id keeps the CLI call site self-contained — the
    command is invoked with --agent and reads from this exact DB.
    """
    store = SQLiteStore(tmp_path / "mem.db")
    agent_id = "replay_test"
    journal = EvolutionJournal(store, agent_id=agent_id)

    async def _seed() -> str:
        cid = await journal.open_cycle(trigger="pattern_threshold")
        await journal.record_inputs(cid, {
            "observations": [{"tool": "bash", "n": 5}],
            "reflection": {"summary": "bash is common"},
        })
        await journal.record_decisions(cid, {
            "forge_skills": [{"name": "run_bash"}],
        })
        await journal.record_artifact(cid, KIND_SKILL, "skill_abc")
        await journal.update_artifact_status("skill_abc", STATUS_PROMOTED)
        await journal.set_commit_sha("skill_abc", "promote_commit_sha", "c" * 40)
        await journal.close_cycle(cid, verdict=CYCLE_PASSED, metrics={"forged": 1})
        return cid

    # asyncio.get_event_loop() on 3.10+ emits a DeprecationWarning when
    # there's no running loop; using a fresh loop and leaving it installed
    # as the thread default keeps downstream tests (which may call
    # ``asyncio.get_event_loop()``) happy. Don't just ``close()`` it:
    # closing the thread-default loop without replacing it makes the next
    # ``get_event_loop()`` raise "no current event loop".
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cid = loop.run_until_complete(_seed())

    def _fake_get_journal(aid: str):
        # Ignore aid — the CLI passes --agent and we honor it by matching
        # the one journal we actually constructed. Tests that pass a
        # different agent still hit this function but only return this
        # journal; that's intentional because the Typer command uses the
        # passed agent_id to instantiate the journal, not to key a cache.
        return journal

    with patch("xmclaw.evolution.journal.get_journal", _fake_get_journal):
        yield agent_id, cid, journal

    store.close()
    # The CLI command uses asyncio.run(), which clears the thread-default
    # event loop when it exits. Other test modules call the deprecated
    # asyncio.get_event_loop() and rely on one being present, so re-install
    # a fresh loop to avoid leaking "no current event loop" failures into
    # later tests.
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_evo_replay_unknown_cycle(seeded_journal):
    """Unknown cycle id must exit 1 with a friendly message, no traceback."""
    agent_id, _, _ = seeded_journal
    runner = CliRunner()
    result = runner.invoke(
        app, ["evo", "replay", "cycle_does_not_exist", "--agent", agent_id],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_evo_replay_json_round_trip(seeded_journal):
    """``--json`` must emit a parseable payload with cycle + lineage keys."""
    agent_id, cid, _ = seeded_journal
    runner = CliRunner()
    result = runner.invoke(
        app, ["evo", "replay", cid, "--agent", agent_id, "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cycle"]["cycle_id"] == cid
    assert payload["cycle"]["verdict"] == "passed"
    assert payload["cycle"]["inputs"]["reflection"]["summary"] == "bash is common"
    artifacts = payload["lineage"]
    assert len(artifacts) == 1
    assert artifacts[0]["artifact_id"] == "skill_abc"
    assert artifacts[0]["status"] == "promoted"
    assert artifacts[0]["promote_commit_sha"] == "c" * 40


def test_evo_replay_plain_text(seeded_journal):
    """Default rendering must be greppable — ids, verdict, sha all present."""
    agent_id, cid, _ = seeded_journal
    runner = CliRunner()
    result = runner.invoke(app, ["evo", "replay", cid, "--agent", agent_id])
    assert result.exit_code == 0, result.output
    # Cycle header info.
    assert cid in result.output
    assert "passed" in result.output
    # Artifact table.
    assert "skill_abc" in result.output
    # Truncated promote sha (first 10 chars of 40 'c's).
    assert "cccccccccc" in result.output


def test_evo_cycles_lists_seeded_cycle(seeded_journal):
    """``evo cycles`` must show the seeded cycle and its verdict."""
    agent_id, cid, _ = seeded_journal
    runner = CliRunner()
    result = runner.invoke(app, ["evo", "cycles", "--agent", agent_id])
    assert result.exit_code == 0, result.output
    assert cid in result.output
    assert "passed" in result.output


def test_evo_cycles_empty_db(tmp_path):
    """No cycles → friendly message, not a crash or empty table."""
    store = SQLiteStore(tmp_path / "empty.db")
    journal = EvolutionJournal(store, agent_id="nobody")
    try:
        with patch(
            "xmclaw.evolution.journal.get_journal", lambda aid: journal,
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["evo", "cycles", "--agent", "nobody"])
            assert result.exit_code == 0
            assert "no cycles recorded" in result.output
    finally:
        store.close()
        # See fixture: asyncio.run() clears the loop on exit; re-install.
        asyncio.set_event_loop(asyncio.new_event_loop())
