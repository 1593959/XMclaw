"""Phase E6 regression tests: semantic coherence checks.

Pins:

* **PR-E6-1** — ``coherence.check_gene_coherence`` /
  ``check_skill_coherence`` are pure predicates over a concept + a
  snapshot of live artifacts. Overlapping triggers and near-duplicate
  skill descriptions must be caught; genuinely new artifacts must pass.
* **PR-E6-2** — ``EvolutionEngine._generate_gene`` / ``_generate_skill``
  run coherence AFTER safety policy and BEFORE VFM, and emit
  ``EVOLUTION_REJECTED`` with a ``coherence:<reason>`` slug on rejection.
* **PR-E6-3** — ``SQLiteStore.get_skills`` returns every installed skill
  for the agent so the coherence snapshot covers the live set.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.evolution import coherence


# ── PR-E6-1: pure predicates ────────────────────────────────────────────────

def test_gene_coherence_accepts_disjoint_trigger():
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "reboot", "trigger_type": "keyword"},
        [{"trigger": "shutdown", "trigger_type": "keyword", "enabled": True}],
    )
    assert ok and reason is None


@pytest.mark.parametrize("proposed_trigger,expected", [
    ("help", "gene_keyword_duplicate"),   # exact
    ("HELP", "gene_keyword_duplicate"),   # case-insensitive
    ("  help  ", "gene_keyword_duplicate"),  # whitespace-insensitive
])
def test_gene_coherence_keyword_duplicate_rejected(proposed_trigger, expected):
    existing = [{"trigger": "help", "trigger_type": "keyword", "enabled": True}]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": proposed_trigger, "trigger_type": "keyword"},
        existing,
    )
    assert not ok and reason == expected


def test_gene_coherence_regex_duplicate_rejected():
    existing = [{"trigger": r"^error:\s", "trigger_type": "regex", "enabled": True}]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": r"^error:\s", "trigger_type": "regex"},
        existing,
    )
    assert not ok and reason == "gene_regex_duplicate"


def test_gene_coherence_regex_different_pattern_passes():
    """We don't reason about regex equivalence — only literal match."""
    existing = [{"trigger": r"^error:\s", "trigger_type": "regex", "enabled": True}]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": r"^ERROR:\s", "trigger_type": "regex"},
        existing,
    )
    assert ok and reason is None


def test_gene_coherence_intent_overlap_rejected():
    existing = [{
        "trigger": "",
        "trigger_type": "intent",
        "intents": json.dumps(["help_request", "troubleshoot"]),
        "enabled": True,
    }]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "", "trigger_type": "intent", "intents": ["troubleshoot", "deploy"]},
        existing,
    )
    assert not ok and reason == "gene_intent_overlap"


def test_gene_coherence_intent_disjoint_passes():
    existing = [{
        "trigger": "",
        "trigger_type": "intent",
        "intents": ["alpha", "beta"],
        "enabled": True,
    }]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "", "trigger_type": "intent", "intents": ["gamma"]},
        existing,
    )
    assert ok and reason is None


def test_gene_coherence_ignores_disabled_existing():
    existing = [{"trigger": "help", "trigger_type": "keyword", "enabled": False}]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "help", "trigger_type": "keyword"},
        existing,
    )
    assert ok and reason is None


def test_gene_coherence_keyword_vs_regex_do_not_collide():
    """Different trigger_types are independent namespaces."""
    existing = [{"trigger": "help", "trigger_type": "keyword", "enabled": True}]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "help", "trigger_type": "regex"},
        existing,
    )
    assert ok and reason is None


def test_gene_coherence_intents_as_json_string_handled():
    """DB returns intents as a JSON string; coherence must normalise."""
    existing = [{
        "trigger": "",
        "trigger_type": "intent",
        "intents": '["x","y"]',
        "enabled": True,
    }]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "", "trigger_type": "intent", "intents": ["x"]},
        existing,
    )
    assert not ok and reason == "gene_intent_overlap"


def test_gene_coherence_intents_garbage_json_tolerated():
    existing = [{
        "trigger": "",
        "trigger_type": "intent",
        "intents": "not json",
        "enabled": True,
    }]
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "", "trigger_type": "intent", "intents": ["x"]},
        existing,
    )
    assert ok and reason is None


def test_gene_coherence_empty_existing_passes():
    ok, reason = coherence.check_gene_coherence(
        {"trigger": "help", "trigger_type": "keyword"},
        [],
    )
    assert ok and reason is None


def test_skill_coherence_rejects_near_duplicate_description():
    existing = [{
        "name": "auto_summarize_daily_logs",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }]
    concept = {
        # Slightly different name — so E4 dedup doesn't catch it.
        "name": "auto_summarize_daily_log_entries",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }
    ok, reason = coherence.check_skill_coherence(concept, existing)
    assert not ok and reason == "skill_description_near_duplicate"


def test_skill_coherence_accepts_disjoint_descriptions():
    existing = [{
        "name": "auto_summarize_daily_logs",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }]
    concept = {
        "name": "auto_format_chart_titles",
        "description": "format chart titles to match the corporate style guide",
    }
    ok, reason = coherence.check_skill_coherence(concept, existing)
    assert ok and reason is None


def test_skill_coherence_short_description_passes():
    existing = [{
        "name": "auto_x",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }]
    concept = {"name": "auto_y", "description": "do a thing"}
    ok, reason = coherence.check_skill_coherence(concept, existing)
    assert ok and reason is None


def test_skill_coherence_skips_same_name():
    """Identity collisions belong to E4. Same-name must not shadow a more
    informative reject path via ``duplicate_concept``."""
    existing = [{
        "name": "auto_same",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }]
    concept = {
        "name": "auto_same",
        "description": "summarize daily logs produced by the ingestion pipeline for operators",
    }
    ok, reason = coherence.check_skill_coherence(concept, existing)
    assert ok and reason is None


# ── PR-E6-2: engine wiring ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_gene_rejected_for_keyword_duplicate(tmp_path, monkeypatch):
    """Proposing a gene whose keyword trigger matches a live gene must
    emit EVOLUTION_REJECTED with a coherence:<reason> slug and skip VFM."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_coherence")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine._journal = None
    engine._current_cycle_id = "cycle_test"

    # Plant an existing live gene that will collide.
    from xmclaw.memory.sqlite_store import SQLiteStore
    store = SQLiteStore(engine.db_path)
    store.insert_gene("agent_coherence", {
        "id": "gene_existing",
        "name": "Existing",
        "description": "d",
        "trigger": "help",
        "trigger_type": "keyword",
        "action": "respond kindly",
        "priority": 5,
        "enabled": True,
        "intents": [],
    })

    async def _fake_llm_complete(messages):
        return json.dumps({
            "name": "NewHelpGene",
            "description": "same trigger as existing",
            "trigger": "help",
            "trigger_type": "keyword",
            "action": "be helpful",
        })
    engine.llm.complete = _fake_llm_complete  # type: ignore[method-assign]

    ran_vfm = {"n": 0}
    def _vfm(_c):
        ran_vfm["n"] += 1
        return {"total": 99.0}
    engine.vfm.score_gene = _vfm  # type: ignore[method-assign]

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []

    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub = bus.subscribe(EventType.EVOLUTION_REJECTED.value, _h)

    try:
        result = await engine._generate_gene({
            "type": "gene",
            "insight": {"title": "help trigger", "description": "d", "source": "s"},
        })
        await asyncio.sleep(0.05)
        assert result is None
        assert ran_vfm["n"] == 0, "VFM must not run after coherence rejection"
        matched = [e for e in seen if e["payload"].get("reason") == "coherence:gene_keyword_duplicate"]
        assert matched, f"expected coherence:gene_keyword_duplicate; saw {seen}"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_generate_skill_rejected_for_near_duplicate_description(tmp_path, monkeypatch):
    """Skill with a new name but near-identical description to a live skill
    must be rejected by coherence BEFORE VFM runs."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_coherence_skill")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine._journal = None
    engine._current_cycle_id = "cycle_test"

    # Install an existing skill with a sidecar description JSON so
    # _live_skill_snapshot can pick up the description.
    from xmclaw.memory.sqlite_store import SQLiteStore
    store = SQLiteStore(engine.db_path)

    existing_path = tmp_path / "active" / "auto_summarize_daily_logs.py"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text("# stub", encoding="utf-8")
    sidecar = existing_path.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "id": "skill_existing",
        "name": "auto_summarize_daily_logs",
        "description": (
            "Detected repeated requests to summarize daily log files from the "
            "ingestion pipeline. Consider creating a specialized skill for "
            "operator-visible log digestion."
        ),
    }), encoding="utf-8")
    store.insert_skill("agent_coherence_skill", {
        "id": "skill_existing",
        "name": "auto_summarize_daily_logs",
        "category": "auto",
        "version": "v1",
        "path": str(existing_path),
    })

    ran_vfm = {"n": 0}
    def _vfm(_c):
        ran_vfm["n"] += 1
        return {"total": 99.0}
    engine.vfm.score_skill = _vfm  # type: ignore[method-assign]

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []

    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub = bus.subscribe(EventType.EVOLUTION_REJECTED.value, _h)

    try:
        # Same description content, slightly different name → E4 dedup does
        # not fire; E6 coherence should.
        insight = {
            "title": "summarize log entries for operators",
            "description": (
                "Detected repeated requests to summarize daily log files from the "
                "ingestion pipeline. Consider creating a specialized skill for "
                "operator-visible log digestion."
            ),
            "source": "repeated_request",
        }
        result = await engine._generate_skill({"type": "skill", "insight": insight})
        await asyncio.sleep(0.05)
        assert result is None, "near-duplicate skill must be rejected"
        assert ran_vfm["n"] == 0
        matched = [
            e for e in seen
            if e["payload"].get("reason") == "coherence:skill_description_near_duplicate"
        ]
        assert matched, f"expected coherence:skill_description_near_duplicate; saw {seen}"
    finally:
        bus.unsubscribe(sub)


# ── PR-E6-3: SQLiteStore.get_skills ────────────────────────────────────────

def test_get_skills_returns_all_for_agent(tmp_path):
    from xmclaw.memory.sqlite_store import SQLiteStore
    db = tmp_path / "mem.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db)
    store.insert_skill("agent_A", {
        "id": "skill_a1", "name": "one", "category": "auto",
        "version": "v1", "path": "/tmp/one.py",
    })
    store.insert_skill("agent_A", {
        "id": "skill_a2", "name": "two", "category": "auto",
        "version": "v1", "path": "/tmp/two.py",
    })
    store.insert_skill("agent_B", {
        "id": "skill_b1", "name": "b_one", "category": "auto",
        "version": "v1", "path": "/tmp/b_one.py",
    })

    a = store.get_skills("agent_A")
    b = store.get_skills("agent_B")
    empty = store.get_skills("agent_missing")

    assert {r["id"] for r in a} == {"skill_a1", "skill_a2"}
    assert [r["id"] for r in b] == ["skill_b1"]
    assert empty == []
