"""Phase E3 regression tests: skill execution telemetry + auto-rollback.

Pins the contract for the canary loop:

* **PR-E3-1** — every invocation of a generated skill (`skill_*` tool name)
  must increment `matched_count` on its lineage row, plus `helpful_count`
  on success or `harmful_count` on failure. Telemetry MUST be a side
  channel — a broken journal cannot break the tool loop.
* **PR-E3-2** — once harmful metrics cross the configured threshold the
  promoted artifact is retired from the active dir, its lineage status
  flips to `rolled_back`, and an `EVOLUTION_ROLLBACK` event fires so the
  Live panel can update.
* **PR-E3-3** — `EvolutionJournal.get_artifact_health` summarises a
  single artifact for meta-evaluation and the UI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xmclaw.evolution.journal import (
    EvolutionJournal,
    KIND_SKILL,
    STATUS_PROMOTED,
    STATUS_ROLLED_BACK,
    STATUS_SHADOW,
)
from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.tools.base import Tool
from xmclaw.tools.registry import ToolRegistry


class _OKSkill(Tool):
    name = "skill_ok123"
    description = "test skill that returns OK"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "done"


class _ErrorSkill(Tool):
    name = "skill_err123"
    description = "test skill that always raises"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        raise RuntimeError("boom")


class _ErrorStringSkill(Tool):
    name = "skill_errstr123"
    description = "test skill that returns an [Error ...] sentinel"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "[Error: this looks like a success but the base class treats it as failure]"


@pytest.fixture
def journal_db(tmp_path, monkeypatch):
    """Re-point the get_journal() factory at an in-tmpdir sqlite file so
    tests don't touch the real shared/memory.db. Also resets the per-
    agent cache between tests."""
    from xmclaw.evolution import journal as journal_mod
    journal_mod.reset_journal_cache()
    # Patch the factory to point SQLiteStore at tmp_path instead of BASE_DIR.
    original = journal_mod.get_journal

    def _factory(agent_id: str) -> EvolutionJournal:
        cached = journal_mod._JOURNAL_CACHE.get(agent_id)
        if cached is not None:
            return cached
        store = SQLiteStore(tmp_path / "memory.db")
        j = EvolutionJournal(store, agent_id=agent_id)
        journal_mod._JOURNAL_CACHE[agent_id] = j
        return j

    monkeypatch.setattr(journal_mod, "get_journal", _factory)
    yield _factory
    journal_mod.reset_journal_cache()


async def _seed_promoted(journal: EvolutionJournal, artifact_id: str) -> None:
    """Insert a cycle + promoted lineage row we can increment against."""
    cycle_id = await journal.open_cycle(trigger="test")
    await journal.record_artifact(cycle_id, KIND_SKILL, artifact_id, status=STATUS_SHADOW)
    await journal.update_artifact_status(artifact_id, STATUS_PROMOTED)


# ── PR-E3-1: telemetry increments ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_success_increments_matched_and_helpful(journal_db, monkeypatch):
    journal = journal_db("agent_a")
    await _seed_promoted(journal, "skill_ok123")

    registry = ToolRegistry()
    registry._tools = {"skill_ok123": _OKSkill()}

    result = await registry.execute("skill_ok123", {}, agent_id="agent_a")
    assert result == "done"

    # Give any pending writes a tick — the store is sync, so this is
    # actually synchronous, but the explicit await keeps intent clear.
    await asyncio.sleep(0)
    row = await journal.get_artifact("skill_ok123")
    assert row is not None
    assert row["matched_count"] == 1
    assert row["helpful_count"] == 1
    assert row["harmful_count"] == 0


@pytest.mark.asyncio
async def test_skill_exception_increments_matched_and_harmful(journal_db):
    journal = journal_db("agent_b")
    await _seed_promoted(journal, "skill_err123")

    registry = ToolRegistry()
    registry._tools = {"skill_err123": _ErrorSkill()}

    result = await registry.execute("skill_err123", {}, agent_id="agent_b")
    assert result.startswith("[Error executing skill_err123")

    row = await journal.get_artifact("skill_err123")
    assert row is not None
    # Retry logic runs 3 attempts — but telemetry is recorded exactly once,
    # after the final failure.
    assert row["matched_count"] == 1
    assert row["helpful_count"] == 0
    assert row["harmful_count"] == 1


@pytest.mark.asyncio
async def test_skill_error_sentinel_counts_as_harmful(journal_db):
    """A skill that 'returns' a string starting with `[Error` must still
    be counted as harmful. Otherwise skills that catch their own exceptions
    and return a fake-success string would evade the canary."""
    journal = journal_db("agent_c")
    await _seed_promoted(journal, "skill_errstr123")

    registry = ToolRegistry()
    registry._tools = {"skill_errstr123": _ErrorStringSkill()}

    await registry.execute("skill_errstr123", {}, agent_id="agent_c")

    row = await journal.get_artifact("skill_errstr123")
    assert row is not None
    assert row["matched_count"] == 1
    assert row["harmful_count"] == 1
    assert row["helpful_count"] == 0


@pytest.mark.asyncio
async def test_builtin_tools_do_not_hit_journal(journal_db, monkeypatch):
    """Only generated skills (`skill_*`) produce telemetry. A built-in tool
    must not open a journal handle, even with agent_id set."""
    from xmclaw.evolution import journal as journal_mod

    class _Builtin(Tool):
        name = "bash"
        description = "test builtin"
        parameters = {}
        async def execute(self, **kwargs):
            return "hi"

    called = {"n": 0}
    original = journal_mod.get_journal
    def _spy(aid):
        called["n"] += 1
        return original(aid)
    monkeypatch.setattr(journal_mod, "get_journal", _spy)

    registry = ToolRegistry()
    registry._tools = {"bash": _Builtin()}
    await registry.execute("bash", {}, agent_id="agent_d")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_break_tool(journal_db, monkeypatch):
    """The tool loop must NEVER be broken by a broken journal. If every
    write raises, the tool still returns its result to the caller."""
    from xmclaw.evolution import journal as journal_mod

    class _ExplodingJournal:
        async def increment_metric(self, *a, **kw):
            raise RuntimeError("journal is broken")
        async def get_artifact(self, *a, **kw):
            raise RuntimeError("also broken")

    monkeypatch.setattr(journal_mod, "get_journal", lambda aid: _ExplodingJournal())

    registry = ToolRegistry()
    registry._tools = {"skill_ok123": _OKSkill()}
    result = await registry.execute("skill_ok123", {}, agent_id="agent_e")
    # Tool result is unchanged.
    assert result == "done"


# ── PR-E3-2: auto-rollback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_rollback_absolute_threshold(journal_db, tmp_path, monkeypatch):
    """After harmful_count ≥ threshold AND > helpful_count, the promoted
    skill must be rolled back: active file deleted, lineage flipped to
    rolled_back, EVOLUTION_ROLLBACK emitted."""
    # Point BASE_DIR at tmp_path so rollback deletes our test file, not prod.
    monkeypatch.setattr("xmclaw.tools.registry.BASE_DIR", tmp_path)
    # Make sure config's `auto_rollback` knob is honoured — use the default
    # threshold (3) so the test mirrors production behaviour.

    journal = journal_db("agent_f")
    await _seed_promoted(journal, "skill_err123")

    # Seed the active dir with a file we expect to be deleted.
    active_dir = tmp_path / "shared" / "skills"
    active_dir.mkdir(parents=True)
    active_py = active_dir / "skill_err123.py"
    active_py.write_text("# pretend skill")

    # Capture EVOLUTION_ROLLBACK events.
    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _handler(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub_id = bus.subscribe(EventType.EVOLUTION_ROLLBACK.value, _handler)

    try:
        registry = ToolRegistry()
        registry._tools = {"skill_err123": _ErrorSkill()}

        # Run three failing calls — default threshold is 3.
        for _ in range(3):
            await registry.execute("skill_err123", {}, agent_id="agent_f")

        # Let event bus flush.
        await asyncio.sleep(0.05)

        row = await journal.get_artifact("skill_err123")
        assert row is not None
        assert row["status"] == STATUS_ROLLED_BACK
        assert not active_py.exists(), "active .py must be deleted on rollback"
        assert any(
            e["payload"].get("artifact_id") == "skill_err123"
            for e in seen
        ), f"EVOLUTION_ROLLBACK not emitted (saw: {seen})"
    finally:
        bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_auto_rollback_does_not_trigger_below_threshold(journal_db, tmp_path, monkeypatch):
    """Two harmful calls with default threshold=3 must NOT roll back."""
    monkeypatch.setattr("xmclaw.tools.registry.BASE_DIR", tmp_path)

    journal = journal_db("agent_g")
    await _seed_promoted(journal, "skill_err123")

    active_dir = tmp_path / "shared" / "skills"
    active_dir.mkdir(parents=True)
    active_py = active_dir / "skill_err123.py"
    active_py.write_text("# still here")

    registry = ToolRegistry()
    registry._tools = {"skill_err123": _ErrorSkill()}
    for _ in range(2):
        await registry.execute("skill_err123", {}, agent_id="agent_g")

    row = await journal.get_artifact("skill_err123")
    assert row["status"] == STATUS_PROMOTED, "must not rollback below threshold"
    assert active_py.exists(), "active .py must survive below threshold"


@pytest.mark.asyncio
async def test_auto_rollback_skipped_when_disabled(journal_db, tmp_path, monkeypatch):
    """Operators can disable auto-rollback via config. Without it, harmful
    counts still accrue but the active artifact stays put."""
    monkeypatch.setattr("xmclaw.tools.registry.BASE_DIR", tmp_path)

    # Stub DaemonConfig.load to return a config with auto_rollback=False.
    from xmclaw.daemon import config as cfg_mod
    class _FakeCfg:
        evolution = {"auto_rollback": False}
    monkeypatch.setattr(cfg_mod.DaemonConfig, "load", classmethod(lambda cls: _FakeCfg()))

    journal = journal_db("agent_h")
    await _seed_promoted(journal, "skill_err123")

    active_dir = tmp_path / "shared" / "skills"
    active_dir.mkdir(parents=True)
    active_py = active_dir / "skill_err123.py"
    active_py.write_text("# stays")

    registry = ToolRegistry()
    registry._tools = {"skill_err123": _ErrorSkill()}
    for _ in range(5):
        await registry.execute("skill_err123", {}, agent_id="agent_h")

    row = await journal.get_artifact("skill_err123")
    assert row["harmful_count"] == 5
    assert row["status"] == STATUS_PROMOTED
    assert active_py.exists()


# ── PR-E3-3: get_artifact_health ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_artifact_health_dead_when_never_matched(journal_db):
    journal = journal_db("agent_i")
    await _seed_promoted(journal, "skill_dead")
    health = await journal.get_artifact_health("skill_dead")
    assert health is not None
    assert health["verdict"] == "dead"
    assert health["matched"] == 0
    assert health["helpful_ratio"] is None


@pytest.mark.asyncio
async def test_artifact_health_healthy_when_helpful_dominates(journal_db):
    journal = journal_db("agent_j")
    await _seed_promoted(journal, "skill_good")
    # 3 helpful, 1 harmful → healthy
    for _ in range(3):
        await journal.increment_metric("skill_good", "matched_count")
        await journal.increment_metric("skill_good", "helpful_count")
    await journal.increment_metric("skill_good", "matched_count")
    await journal.increment_metric("skill_good", "harmful_count")

    health = await journal.get_artifact_health("skill_good")
    assert health["verdict"] == "healthy"
    assert health["helpful_ratio"] == pytest.approx(0.75)
    assert health["harmful_ratio"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_artifact_health_suspect_when_harmful_dominates(journal_db):
    journal = journal_db("agent_k")
    await _seed_promoted(journal, "skill_bad")
    # 1 helpful, 3 harmful, matched=4 → suspect
    await journal.increment_metric("skill_bad", "matched_count")
    await journal.increment_metric("skill_bad", "helpful_count")
    for _ in range(3):
        await journal.increment_metric("skill_bad", "matched_count")
        await journal.increment_metric("skill_bad", "harmful_count")

    health = await journal.get_artifact_health("skill_bad")
    assert health["verdict"] == "suspect"


@pytest.mark.asyncio
async def test_artifact_health_unused_after_rollback(journal_db):
    journal = journal_db("agent_l")
    await _seed_promoted(journal, "skill_gone")
    await journal.update_artifact_status("skill_gone", STATUS_ROLLED_BACK)
    health = await journal.get_artifact_health("skill_gone")
    assert health["verdict"] == "unused"


@pytest.mark.asyncio
async def test_artifact_health_returns_none_when_unknown(journal_db):
    journal = journal_db("agent_m")
    assert await journal.get_artifact_health("skill_never_existed") is None
