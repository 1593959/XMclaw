"""Phase E4 regression tests: meta-evaluation feedback loop.

Pins three contracts:

* **PR-E4-1** — ``EvolutionJournal.snapshot_active_artifacts`` must return
  a consistent shape for every promoted/shadow artifact, with verdicts
  derived the same way as ``get_artifact_health``.
* **PR-E4-2** — ``ReflectionEngine.reflect(artifact_health=[...])`` must
  inject the snapshot into the LLM prompt so the next insight is
  informed by how prior cycles performed.
* **PR-E4-3** — ``EvolutionEngine._generate_skill`` must skip the forge
  when a live (promoted/shadow) skill already exists for the same concept
  name. Rolled-back or retired skills don't block re-forging — they
  already failed.
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


@pytest.fixture
def store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "memory.db")


@pytest.fixture
def journal(store) -> EvolutionJournal:
    return EvolutionJournal(store, agent_id="agent_meta")


async def _seed(j: EvolutionJournal, artifact_id: str, status: str = STATUS_PROMOTED):
    cid = await j.open_cycle(trigger="test")
    await j.record_artifact(cid, KIND_SKILL, artifact_id, status=STATUS_SHADOW)
    if status != STATUS_SHADOW:
        await j.update_artifact_status(artifact_id, status)


# ── PR-E4-1: snapshot_active_artifacts ─────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_includes_promoted_and_shadow(journal):
    await _seed(journal, "skill_promoted", STATUS_PROMOTED)
    await _seed(journal, "skill_shadow", STATUS_SHADOW)
    snap = await journal.snapshot_active_artifacts()
    ids = {s["artifact_id"] for s in snap}
    assert "skill_promoted" in ids
    assert "skill_shadow" in ids


@pytest.mark.asyncio
async def test_snapshot_excludes_retired_and_rolled_back(journal):
    await _seed(journal, "skill_alive", STATUS_PROMOTED)
    await _seed(journal, "skill_gone", STATUS_ROLLED_BACK)
    snap = await journal.snapshot_active_artifacts()
    ids = {s["artifact_id"] for s in snap}
    assert "skill_alive" in ids
    assert "skill_gone" not in ids, "rolled-back artifacts must not appear in active snapshot"


@pytest.mark.asyncio
async def test_snapshot_verdicts(journal):
    # healthy: 3 helpful / 1 harmful, matched 4
    await _seed(journal, "skill_healthy", STATUS_PROMOTED)
    for _ in range(3):
        await journal.increment_metric("skill_healthy", "matched_count")
        await journal.increment_metric("skill_healthy", "helpful_count")
    await journal.increment_metric("skill_healthy", "matched_count")
    await journal.increment_metric("skill_healthy", "harmful_count")

    # dead: promoted but never matched
    await _seed(journal, "skill_dead", STATUS_PROMOTED)

    # suspect: 1 helpful / 3 harmful, matched 4
    await _seed(journal, "skill_suspect", STATUS_PROMOTED)
    await journal.increment_metric("skill_suspect", "matched_count")
    await journal.increment_metric("skill_suspect", "helpful_count")
    for _ in range(3):
        await journal.increment_metric("skill_suspect", "matched_count")
        await journal.increment_metric("skill_suspect", "harmful_count")

    snap = {s["artifact_id"]: s for s in await journal.snapshot_active_artifacts()}
    assert snap["skill_healthy"]["verdict"] == "healthy"
    assert snap["skill_dead"]["verdict"] == "dead"
    assert snap["skill_suspect"]["verdict"] == "suspect"


@pytest.mark.asyncio
async def test_snapshot_respects_max_items(journal):
    for i in range(5):
        await _seed(journal, f"skill_{i:02d}", STATUS_PROMOTED)
    snap = await journal.snapshot_active_artifacts(max_items=3)
    assert len(snap) == 3


# ── PR-E4-2: reflection consumes artifact_health ───────────────────────────

@pytest.mark.asyncio
async def test_reflection_injects_health_into_prompt(monkeypatch):
    """Reflection must prepend an 'active artifacts' block to the prompt
    when artifact_health is passed. The LLM sees it BEFORE the reflection
    instructions so it biases the output."""
    from xmclaw.core.reflection import ReflectionEngine, ReflectionTrigger

    captured: dict = {}

    class _FakeLLM:
        # Reflection consumes raw text via .complete() (not stream() — that
        # yields JSON event envelopes, see xmclaw.core.reflection).
        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            return '{"success": true, "summary": "ok", "problems": [], "lessons": [], "improvements": []}'

    class _FakeMem:
        sessions = None

        def save_insight(self, *a, **kw):
            pass

        async def add_memory(self, *a, **kw):
            pass

    # Stub auto_improver so we don't need a real LLM/journal for save.
    from xmclaw.evolution import auto_improver as auto_mod

    class _NoImprover:
        async def improve_from_reflection(self, *a, **kw):
            return {"status": "noop"}

    monkeypatch.setattr(auto_mod, "AutoImprover", _NoImprover)

    engine = ReflectionEngine(llm_router=_FakeLLM(), memory=_FakeMem())
    history = [{"user": "hi", "assistant": "hey", "tool_calls": []}]
    health = [
        {"artifact_id": "skill_ok", "kind": "skill", "status": "promoted",
         "matched": 3, "helpful": 2, "harmful": 1, "verdict": "healthy"},
        {"artifact_id": "skill_bad", "kind": "skill", "status": "promoted",
         "matched": 4, "helpful": 0, "harmful": 4, "verdict": "suspect"},
    ]
    result = await engine.reflect(
        "agent_meta", history,
        trigger=ReflectionTrigger.CONVERSATION_END,
        artifact_health=health,
    )
    assert result["status"] == "ok"
    prompt = captured["messages"][-1]["content"]
    assert "当前活跃的进化产物" in prompt
    assert "skill_ok" in prompt and "skill_bad" in prompt
    assert "[healthy]" in prompt and "[suspect]" in prompt


@pytest.mark.asyncio
async def test_reflection_without_health_produces_classic_prompt(monkeypatch):
    """With no artifact_health, the prompt must NOT include the new block —
    we don't want to regress runs that happen before the journal is up."""
    from xmclaw.core.reflection import ReflectionEngine

    captured: dict = {}

    class _FakeLLM:
        # Reflection uses .complete() (stream() yields JSON event envelopes).
        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            return '{"success": true, "summary": "ok"}'

    class _FakeMem:
        sessions = None
        def save_insight(self, *a, **kw): pass
        async def add_memory(self, *a, **kw): pass

    from xmclaw.evolution import auto_improver as auto_mod
    class _NoImprover:
        async def improve_from_reflection(self, *a, **kw): return {}
    monkeypatch.setattr(auto_mod, "AutoImprover", _NoImprover)

    engine = ReflectionEngine(llm_router=_FakeLLM(), memory=_FakeMem())
    history = [{"user": "hi", "assistant": "hey", "tool_calls": []}]
    await engine.reflect("agent_meta", history)
    prompt = captured["messages"][-1]["content"]
    assert "当前活跃的进化产物" not in prompt


# ── PR-E4-3: forge dedup guard ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_skill_skips_when_live_skill_exists(tmp_path, monkeypatch):
    """A second cycle whose concept name matches an already-promoted skill
    must NOT forge a duplicate. The engine emits EVOLUTION_REJECTED with
    reason=duplicate_concept instead."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine

    engine = EvolutionEngine(agent_id="agent_meta")
    # Give it a real journal pointed at the tmp DB.
    store = SQLiteStore(engine.db_path)
    journal = EvolutionJournal(store, agent_id="agent_meta")
    engine._journal = journal
    engine._current_cycle_id = await journal.open_cycle(trigger="test_cycle")

    # Seed: a previously-forged skill with concept name "auto_cache_curl_responses"
    # and a lineage row marking it as promoted.
    concept_name = "auto_cache_curl_responses"
    existing_skill = {
        "id": "skill_existing",
        "name": concept_name,
        "category": "auto",
        "version": "v1",
        "path": str(tmp_path / "shared" / "skills" / "skill_existing.py"),
    }
    store.insert_skill("agent_meta", existing_skill)
    # Record lineage as promoted so it's considered 'live'.
    await journal.record_artifact(
        engine._current_cycle_id, KIND_SKILL, "skill_existing", status=STATUS_SHADOW,
    )
    await journal.update_artifact_status("skill_existing", STATUS_PROMOTED)

    # Stub VFM + forge — they must never be called if the guard fires.
    fire_vfm = {"n": 0}
    def _vfm_score(concept):
        fire_vfm["n"] += 1
        return {"total": 99.0}
    engine.vfm.score_skill = _vfm_score  # type: ignore[method-assign]

    # Capture EVOLUTION_REJECTED.
    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _handler(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub = bus.subscribe(EventType.EVOLUTION_REJECTED.value, _handler)

    try:
        insight = {
            "title": "cache curl responses",  # name() will match concept_name
            "description": "cache responses",
            "source": "tool_usage_analysis",
        }
        result = await engine._generate_skill({"type": "skill", "insight": insight})
        await asyncio.sleep(0.05)  # let event bus flush

        assert result is None, "forge must be skipped when a live skill exists"
        assert fire_vfm["n"] == 0, "VFM must not even run when dedup guard hits"
        assert any(
            e["payload"].get("reason") == "duplicate_concept"
            and e["payload"].get("concept") == concept_name
            for e in seen
        ), f"EVOLUTION_REJECTED with duplicate_concept not emitted (saw: {seen})"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_generate_skill_allows_reforge_after_rollback(tmp_path, monkeypatch):
    """A previously-promoted but now-rolled-back skill should NOT block a
    re-forge. Rollback is the system saying 'that one was wrong'; the next
    cycle must be allowed to try again."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine

    engine = EvolutionEngine(agent_id="agent_meta")
    store = SQLiteStore(engine.db_path)
    journal = EvolutionJournal(store, agent_id="agent_meta")
    engine._journal = journal
    engine._current_cycle_id = await journal.open_cycle(trigger="test_cycle")

    concept_name = "auto_flaky_attempt"
    store.insert_skill("agent_meta", {
        "id": "skill_old",
        "name": concept_name,
        "category": "auto",
        "version": "v1",
        "path": str(tmp_path / "shared" / "skills" / "skill_old.py"),
    })
    await journal.record_artifact(
        engine._current_cycle_id, KIND_SKILL, "skill_old", status=STATUS_SHADOW,
    )
    await journal.update_artifact_status("skill_old", STATUS_ROLLED_BACK)

    # The dedup guard should NOT trigger — confirm by calling the helper.
    res = await engine._find_live_skill_for_concept(concept_name)
    assert res is None, "rolled-back skill must not count as live"


@pytest.mark.asyncio
async def test_find_live_skill_returns_promoted_row(tmp_path, monkeypatch):
    """Direct test of the helper — promoted status counts as live."""
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)
    from xmclaw.evolution.engine import EvolutionEngine

    engine = EvolutionEngine(agent_id="agent_meta")
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(engine.db_path)
    journal = EvolutionJournal(store, agent_id="agent_meta")
    engine._journal = journal
    cid = await journal.open_cycle(trigger="t")

    store.insert_skill("agent_meta", {
        "id": "skill_p", "name": "auto_foo", "category": "auto",
        "version": "v1", "path": "/tmp/skill_p.py",
    })
    await journal.record_artifact(cid, KIND_SKILL, "skill_p", status=STATUS_SHADOW)
    await journal.update_artifact_status("skill_p", STATUS_PROMOTED)

    row = await engine._find_live_skill_for_concept("auto_foo")
    assert row is not None
    assert row["id"] == "skill_p"
