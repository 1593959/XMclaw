"""Phase E5 regression tests: safety policy + gene match telemetry.

Pins:

* **PR-E5-1** — ``safety_policy.check_skill_concept`` /
  ``check_gene_concept`` are pure predicates. Every reject path is
  exercised individually so changes to the rule set don't silently
  loosen.
* **PR-E5-2** — ``EvolutionEngine._generate_skill`` and ``_generate_gene``
  must run policy checks BEFORE any forge writes, emit
  ``EVOLUTION_REJECTED`` with a ``policy:<reason>`` slug, and return None.
* **PR-E5-3** — ``GeneManager.match`` must bump ``matched_count`` on the
  lineage row of every gene it returns. Genes without a lineage row
  (pre-journal) are tolerated silently — the increment is a no-op.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xmclaw.evolution import safety_policy
from xmclaw.evolution.journal import (
    EvolutionJournal,
    KIND_GENE,
    KIND_SKILL,
    STATUS_SHADOW,
)
from xmclaw.memory.sqlite_store import SQLiteStore


# ── PR-E5-1: safety_policy pure predicates ─────────────────────────────────

def test_skill_policy_accepts_well_formed():
    ok, reason = safety_policy.check_skill_concept({
        "name": "auto_summarize_logs",
        "description": "summarize logs",
    })
    assert ok and reason is None


@pytest.mark.parametrize("name,expected_reason", [
    ("", "name_empty"),
    ("   ", "name_empty"),
    (None, "name_not_string"),
    (123, "name_not_string"),
    ("x" * 121, "name_length_out_of_bounds"),
    ("bash", "name_collision_with_builtin"),
    ("BASH", "name_collision_with_builtin"),
    ("web_search", "name_collision_with_builtin"),
    ("skill_new", "name_uses_reserved_prefix"),
    ("gene_new", "name_uses_reserved_prefix"),
])
def test_skill_policy_name_rejects(name, expected_reason):
    ok, reason = safety_policy.check_skill_concept({"name": name})
    assert not ok
    assert reason == expected_reason, f"wrong reject reason for name={name!r}"


def test_skill_policy_rejects_long_description():
    ok, reason = safety_policy.check_skill_concept({
        "name": "auto_ok",
        "description": "x" * 2001,
    })
    assert not ok and reason == "description_too_long"


def test_gene_policy_accepts_well_formed_keyword():
    ok, reason = safety_policy.check_gene_concept({
        "name": "UrgencyNoticer",
        "description": "notices urgent input",
        "trigger": "urgent",
        "trigger_type": "keyword",
    })
    assert ok and reason is None


@pytest.mark.parametrize("concept,expected_reason", [
    ({"name": "OK", "trigger": "", "trigger_type": "keyword"}, "empty_trigger"),
    ({"name": "OK", "trigger": "   ", "trigger_type": "keyword"}, "empty_trigger"),
    ({"name": "OK", "trigger": "x" * 300, "trigger_type": "keyword"}, "trigger_too_long"),
    ({"name": "OK", "trigger": "[", "trigger_type": "regex"}, "invalid_regex_trigger"),
    ({"name": "OK", "trigger": "(", "trigger_type": "regex"}, "invalid_regex_trigger"),
    ({"name": "OK", "trigger": "", "trigger_type": "intent", "intents": []}, "intent_trigger_requires_intents"),
    ({"name": "OK", "trigger": "", "trigger_type": "intent", "intents": ["  ", ""]}, "intent_trigger_requires_intents"),
    ({"name": "OK", "trigger": "", "trigger_type": "intent"}, "intent_trigger_requires_intents"),
])
def test_gene_policy_trigger_rejects(concept, expected_reason):
    ok, reason = safety_policy.check_gene_concept(concept)
    assert not ok
    assert reason == expected_reason, f"wrong reject reason for concept={concept!r}"


def test_gene_policy_intent_trigger_accepts_with_nonempty_intents():
    ok, reason = safety_policy.check_gene_concept({
        "name": "IntentMatcher",
        "trigger": "",
        "trigger_type": "intent",
        "intents": ["help_request", "troubleshoot"],
    })
    assert ok and reason is None


def test_gene_policy_regex_must_compile():
    ok, reason = safety_policy.check_gene_concept({
        "name": "UrgencyRegex",
        "trigger": r"(urgent|critical|asap)",
        "trigger_type": "regex",
    })
    assert ok and reason is None


# ── PR-E5-2: engine honours the policy ─────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_skill_rejected_by_policy(tmp_path, monkeypatch):
    """A skill whose concept name collides with a built-in must be rejected
    BEFORE forge, with an EVOLUTION_REJECTED event carrying reason=policy:…"""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_safety")
    engine._journal = None  # skip journal wiring — test is about rejection path
    engine._current_cycle_id = "cycle_test"

    # VFM should NEVER run for a policy-rejected concept.
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
        # Insight title "bash" → concept name "auto_bash", which is NOT a
        # collision. Use a title that yields a direct collision.
        # concept name = f"auto_{insight.title.replace(' ', '_').lower()}",
        # so we need an insight title that produces something like "bash".
        # Easier: short-circuit by using an explicit insight title that
        # happens to be a built-in tool name after normalization. Since
        # "auto_" is prepended, a collision via title is hard — so we
        # craft one via the shape check instead: title = "" makes
        # concept name "auto_" which passes shape.
        #
        # Use empty title to get concept name "auto_" (non-empty, not a
        # builtin) — doesn't fail. So instead use a title that makes the
        # name exceed the length bound.
        long_title = " ".join(["x"] * 80)  # 80 two-char tokens → > 120 chars
        insight = {"title": long_title, "description": "d", "source": "s"}
        result = await engine._generate_skill({"type": "skill", "insight": insight})
        await asyncio.sleep(0.05)

        assert result is None, "policy must block long-name skill"
        assert ran_vfm["n"] == 0, "VFM must not run after policy rejection"
        assert any(
            e["payload"].get("reason", "").startswith("policy:")
            for e in seen
        ), f"EVOLUTION_REJECTED with policy:<reason> not emitted (saw: {seen})"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_generate_gene_rejected_for_empty_trigger(tmp_path, monkeypatch):
    """A gene concept with empty keyword trigger must be rejected before
    VFM runs. The LLM occasionally returns ``trigger: ""`` — policy catches
    it so the forge never writes an unmatched dead gene."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_safety")
    engine._journal = None
    engine._current_cycle_id = "cycle_test"

    async def _fake_llm_complete(messages):
        # Valid JSON, but trigger is empty — this must trip the gene policy.
        import json as _j
        return _j.dumps({
            "name": "DeadGene",
            "description": "triggers on nothing",
            "trigger": "",
            "trigger_type": "keyword",
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
        insight = {"title": "notice urgent stuff", "description": "d", "source": "s"}
        result = await engine._generate_gene({"type": "gene", "insight": insight})
        await asyncio.sleep(0.05)

        assert result is None
        assert ran_vfm["n"] == 0
        assert any(
            e["payload"].get("reason") == "policy:empty_trigger"
            for e in seen
        ), f"expected policy:empty_trigger reject (saw: {seen})"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_generate_gene_rejected_for_bad_regex(tmp_path, monkeypatch):
    """regex-type trigger with an uncompilable pattern must be rejected."""
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)
    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_safety")
    engine._journal = None
    engine._current_cycle_id = "cycle_test"

    async def _fake_llm_complete(messages):
        import json as _j
        return _j.dumps({
            "name": "BrokenRegex",
            "description": "d",
            "trigger": "(unclosed",
            "trigger_type": "regex",
        })
    engine.llm.complete = _fake_llm_complete  # type: ignore[method-assign]

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub = bus.subscribe(EventType.EVOLUTION_REJECTED.value, _h)

    try:
        insight = {"title": "regex gene", "description": "d", "source": "s"}
        result = await engine._generate_gene({"type": "gene", "insight": insight})
        await asyncio.sleep(0.05)
        assert result is None
        assert any(
            e["payload"].get("reason") == "policy:invalid_regex_trigger"
            for e in seen
        ), f"expected invalid_regex_trigger reject (saw: {seen})"
    finally:
        bus.unsubscribe(sub)


# ── PR-E5-3: gene match telemetry ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_gene_match_bumps_lineage_matched_count(tmp_path, monkeypatch):
    """Every gene returned by GeneManager.match must have its lineage row's
    matched_count incremented."""
    monkeypatch.setattr("xmclaw.memory.sqlite_store", __import__(
        "xmclaw.memory.sqlite_store", fromlist=[""]
    ))  # no-op, kept to assert the import path exists
    # Use a tmp DB for isolation.
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(db_path)

    # Seed a gene + its lineage row.
    gene_id = "gene_urgent001"
    store.insert_gene("agent_s", {
        "id": gene_id,
        "name": "UrgencyNoticer",
        "description": "urgent detector",
        "trigger": "urgent",
        "trigger_type": "keyword",
        "action": "flag",
        "priority": 5,
        "enabled": True,
        "intents": [],
        "regex_pattern": "",
    })
    # Create a cycle + lineage row so the increment isn't a no-op.
    journal = EvolutionJournal(store, agent_id="agent_s")
    cid = await journal.open_cycle(trigger="t")
    await journal.record_artifact(cid, KIND_GENE, gene_id, status=STATUS_SHADOW)

    # Point GeneManager at the same DB.
    from xmclaw.genes.manager import GeneManager
    mgr = GeneManager(agent_id="agent_s")
    mgr.db = store  # reuse the same store to avoid opening a second connection

    matched = mgr.match("this is an urgent problem")
    assert any(g["id"] == gene_id for g in matched), f"match didn't return the seeded gene: {matched}"

    row = store.lineage_for_artifact(gene_id)
    assert row is not None
    assert row["matched_count"] == 1, f"matched_count not bumped (row={row})"

    # Second match → counter increments to 2.
    mgr.match("urgent again")
    row = store.lineage_for_artifact(gene_id)
    assert row["matched_count"] == 2


@pytest.mark.asyncio
async def test_gene_match_without_lineage_does_not_raise(tmp_path):
    """Pre-journal genes have no lineage row. The telemetry increment is a
    no-op — it must NOT raise and must NOT break the match path."""
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(db_path)

    gene_id = "gene_oldg"
    store.insert_gene("agent_s", {
        "id": gene_id,
        "name": "OldGene",
        "description": "pre-journal",
        "trigger": "old",
        "trigger_type": "keyword",
        "action": "noop",
        "priority": 1,
        "enabled": True,
        "intents": [],
        "regex_pattern": "",
    })
    # Deliberately DO NOT create a lineage row.

    from xmclaw.genes.manager import GeneManager
    mgr = GeneManager(agent_id="agent_s")
    mgr.db = store

    # Must not raise.
    matched = mgr.match("this is old")
    assert any(g["id"] == gene_id for g in matched)
    # No lineage row was ever created.
    assert store.lineage_for_artifact(gene_id) is None


def test_gene_match_without_any_hit_does_not_call_telemetry(tmp_path, monkeypatch):
    """If no gene matches, the telemetry helper must not be invoked — saves
    work on every no-match turn of the agent loop."""
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(db_path)
    from xmclaw.genes.manager import GeneManager
    mgr = GeneManager(agent_id="agent_s")
    mgr.db = store

    calls = {"n": 0}
    def _spy(genes):
        calls["n"] += 1
    monkeypatch.setattr(mgr, "_bump_gene_matched", _spy)

    matched = mgr.match("nothing matches here")
    assert matched == []
    assert calls["n"] == 0
