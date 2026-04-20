"""Phase E7 regression tests: risk gate + human-in-the-loop approval.

Pins:

* **PR-E7-1** — ``risk.assess_skill_risk`` / ``assess_gene_risk`` are
  pure predicates. They MUST flag dangerous code substrings in a
  skill's action_body, sensitive-domain keywords in either kind, and
  greedy regexes / high priorities in genes.
* **PR-E7-2** — ``EvolutionJournal`` accepts ``STATUS_NEEDS_APPROVAL``
  and surfaces it as ``verdict='pending_approval'`` in health snapshots;
  ``EVOLUTION_APPROVAL_REQUESTED`` / ``EVOLUTION_APPROVAL_DECIDED`` are
  registered event types with wire-name mappings.
* **PR-E7-3** — High-risk artifacts are parked in shadow. The engine's
  ``approve_artifact`` method atomically promotes or retires them in
  response to a user decision, and is idempotent on re-entry.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.evolution import risk
from xmclaw.evolution.journal import (
    STATUS_NEEDS_APPROVAL,
    STATUS_PROMOTED,
    STATUS_RETIRED,
)


# ── PR-E7-1: pure predicates ────────────────────────────────────────────────

def test_skill_risk_accepts_harmless():
    level, reasons = risk.assess_skill_risk(
        {"name": "auto_format_logs", "description": "format log lines"},
        action_body="        return 'formatted'",
    )
    assert level == "low" and reasons == []


@pytest.mark.parametrize("body,expected_substr", [
    ("        import subprocess; subprocess.run(['ls'])", "code:subprocess"),
    ("        os.system('ls')", "code:os.system"),
    ("        eval(user_input)", "code:eval("),
    ("        exec(user_input)", "code:exec("),
    ("        shutil.rmtree('/tmp/x')", "code:shutil.rmtree"),
    ("        __import__('os')", "code:__import__"),
    ("        import os; os.remove(path)", "code:os.remove"),
])
def test_skill_risk_flags_dangerous_code(body, expected_substr):
    level, reasons = risk.assess_skill_risk(
        {"name": "auto_maybe", "description": "does a thing"},
        action_body=body,
    )
    assert level == "high"
    assert any(r.startswith(expected_substr) for r in reasons), reasons


@pytest.mark.parametrize("name,description", [
    ("auto_password_resetter", "helps"),
    ("auto_helper", "reset user passwords across the system"),
    ("auto_helper", "store credit_card numbers for later"),
    ("auto_sudo_runner", "runs a command"),
    ("auto_helper", "delete_all records matching query"),
])
def test_skill_risk_flags_sensitive_domain(name, description):
    level, reasons = risk.assess_skill_risk(
        {"name": name, "description": description},
        action_body="        return 'ok'",
    )
    assert level == "high"
    assert any(r.startswith("domain:") for r in reasons), reasons


def test_gene_risk_accepts_harmless():
    level, reasons = risk.assess_gene_risk({
        "name": "UrgencyNoticer",
        "description": "notice urgent user requests",
        "trigger": "urgent",
        "trigger_type": "keyword",
        "priority": 5,
    })
    assert level == "low" and reasons == []


@pytest.mark.parametrize("trigger,reason_prefix", [
    (".*", "regex:greedy_literal"),
    (".+", "regex:greedy_literal"),
    ("^$", "regex:greedy_literal"),
    ("", "regex:greedy_literal"),
])
def test_gene_risk_flags_greedy_regex_literal(trigger, reason_prefix):
    level, reasons = risk.assess_gene_risk({
        "name": "Greedy", "trigger": trigger, "trigger_type": "regex",
        "priority": 5,
    })
    assert level == "high"
    assert reason_prefix in reasons


def test_gene_risk_flags_regex_that_matches_empty_string():
    # "a*" compiles and matches the empty string — classic over-matcher.
    level, reasons = risk.assess_gene_risk({
        "name": "MatchesEmpty", "trigger": "a*", "trigger_type": "regex",
        "priority": 5,
    })
    assert level == "high"
    assert "regex:matches_empty_string" in reasons


def test_gene_risk_flags_high_priority():
    level, reasons = risk.assess_gene_risk({
        "name": "PriorityBomb", "trigger": "hello", "trigger_type": "keyword",
        "priority": 10,
    })
    assert level == "high"
    assert "priority:10" in reasons


def test_gene_risk_flags_sensitive_domain_in_action():
    level, reasons = risk.assess_gene_risk({
        "name": "Patcher", "trigger": "reset", "trigger_type": "keyword",
        "description": "be helpful",
        "action": "run `sudo` to patch",
        "priority": 5,
    })
    assert level == "high"
    assert any(r.startswith("domain:") for r in reasons)


# ── PR-E7-2: journal surfaces new status ────────────────────────────────────

def test_journal_accepts_needs_approval_status(tmp_path):
    """The journal must accept `needs_approval` via update_artifact_status
    and surface it as a pending_approval verdict in health snapshots."""
    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.evolution.journal import EvolutionJournal, KIND_SKILL, STATUS_SHADOW

    db = tmp_path / "mem.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db)
    journal = EvolutionJournal(store, agent_id="agent_e7")

    async def run():
        cid = await journal.open_cycle(trigger="test")
        await journal.record_artifact(cid, KIND_SKILL, "skill_hold_1",
                                      status=STATUS_SHADOW)
        await journal.update_artifact_status("skill_hold_1", STATUS_NEEDS_APPROVAL)
        health = await journal.get_artifact_health("skill_hold_1")
        assert health is not None
        assert health["status"] == STATUS_NEEDS_APPROVAL
        assert health["verdict"] == "pending_approval"

        snap = await journal.snapshot_active_artifacts()
        held = [s for s in snap if s["artifact_id"] == "skill_hold_1"]
        assert held, "needs_approval artifact must appear in the snapshot"
        assert held[0]["verdict"] == "pending_approval"

    asyncio.run(run())


# ── PR-E7-3: engine wires the gate end-to-end ──────────────────────────────

@pytest.mark.asyncio
async def test_generate_skill_high_risk_parks_in_shadow(tmp_path, monkeypatch):
    """A skill whose forged action_body contains `subprocess` must NOT
    auto-promote. The shadow file stays on disk, status flips to
    needs_approval, and EVOLUTION_APPROVAL_REQUESTED fires with the
    reason slugs."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_risk_skill")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Real journal so status updates persist.
    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.evolution.journal import EvolutionJournal
    engine._journal = EvolutionJournal(
        SQLiteStore(engine.db_path), agent_id="agent_risk_skill",
    )
    engine._current_cycle_id = "cycle_approve_test"
    # Open the cycle so record_artifact has a parent row.
    await engine._journal.open_cycle(trigger="test_e7")
    engine._current_cycle_id = await engine._journal.open_cycle(trigger="test_e7")

    # Deterministic VFM pass, validator pass.
    engine.vfm.score_skill = lambda _c: {"total": 99.0}  # type: ignore[method-assign]
    engine.vfm.should_solidify = lambda _s, _t: True  # type: ignore[method-assign]

    class _Validator:
        async def validate_skill(self, _path):
            return {"passed": True}
        async def validate_gene(self, _path):
            return {"passed": True}
    engine.validator = _Validator()

    async def _fake_llm(messages):
        return json.dumps({
            "action_body": "import subprocess; subprocess.run(['ls'])",
            "parameters": {"input": {"type": "string", "description": "x"}},
        })
    engine.llm.complete = _fake_llm  # type: ignore[method-assign]

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub_req = bus.subscribe(EventType.EVOLUTION_APPROVAL_REQUESTED.value, _h)
    sub_promoted = bus.subscribe(EventType.EVOLUTION_ARTIFACT_PROMOTED.value, _h)

    try:
        insight = {"title": "maybe run subprocess", "description": "d",
                   "source": "task_type_analysis"}
        result = await engine._generate_skill({"type": "skill", "insight": insight})
        await asyncio.sleep(0.05)

        assert result is not None
        assert result["status"] == STATUS_NEEDS_APPROVAL
        assert "code:subprocess" in result["risk_reasons"]

        # Shadow file must remain on disk; active dir must NOT have it.
        shadow_path = Path(engine.skill_forge.shadow_dir) / f"{result['id']}.py"
        active_path = Path(engine.skill_forge.active_dir) / f"{result['id']}.py"
        assert shadow_path.exists(), "shadow file must remain pending"
        assert not active_path.exists(), "must not auto-promote risky skill"

        req = [e for e in seen if e["type"] == EventType.EVOLUTION_APPROVAL_REQUESTED.value]
        assert req, f"expected EVOLUTION_APPROVAL_REQUESTED; saw {seen}"
        assert req[0]["payload"]["kind"] == "skill"
        assert "code:subprocess" in req[0]["payload"]["reasons"]
        promoted = [e for e in seen if e["type"] == EventType.EVOLUTION_ARTIFACT_PROMOTED.value]
        assert not promoted, "must NOT emit promoted for a held artifact"
    finally:
        bus.unsubscribe(sub_req)
        bus.unsubscribe(sub_promoted)


@pytest.mark.asyncio
async def test_generate_gene_high_risk_parks_in_shadow(tmp_path, monkeypatch):
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_risk_gene")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)

    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.evolution.journal import EvolutionJournal
    engine._journal = EvolutionJournal(
        SQLiteStore(engine.db_path), agent_id="agent_risk_gene",
    )
    engine._current_cycle_id = await engine._journal.open_cycle(trigger="test_e7_gene")

    engine.vfm.score_gene = lambda _c: {"total": 99.0}  # type: ignore[method-assign]
    engine.vfm.should_solidify = lambda _s, _t: True  # type: ignore[method-assign]

    class _Validator:
        async def validate_skill(self, _path):
            return {"passed": True}
        async def validate_gene(self, _path):
            return {"passed": True}
    engine.validator = _Validator()

    # Priority 10 is flagged as high risk; trigger "go" is harmless, so the
    # ONLY risk hit should be priority:10.
    async def _fake_llm(messages):
        return json.dumps({
            "name": "HighPrioGene",
            "description": "harmless description",
            "trigger": "go",
            "trigger_type": "keyword",
            "action": "do a thing",
            "priority": 10,
        })
    engine.llm.complete = _fake_llm  # type: ignore[method-assign]

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub = bus.subscribe(EventType.EVOLUTION_APPROVAL_REQUESTED.value, _h)

    try:
        insight = {"title": "urgency", "description": "d", "source": "s"}
        result = await engine._generate_gene({"type": "gene", "insight": insight})
        await asyncio.sleep(0.05)

        assert result is not None
        assert result["status"] == STATUS_NEEDS_APPROVAL
        assert "priority:10" in result["risk_reasons"]

        shadow_path = Path(engine.gene_forge.shadow_dir) / f"{result['id']}.py"
        active_path = Path(engine.gene_forge.active_dir) / f"{result['id']}.py"
        assert shadow_path.exists()
        assert not active_path.exists()

        req = [e for e in seen if e["type"] == EventType.EVOLUTION_APPROVAL_REQUESTED.value]
        assert req, f"expected EVOLUTION_APPROVAL_REQUESTED; saw {seen}"
        assert req[0]["payload"]["kind"] == "gene"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_approve_artifact_promotes_skill(tmp_path, monkeypatch):
    """approve_artifact(approved=True) moves shadow→active, flips status,
    and emits EVOLUTION_APPROVAL_DECIDED + EVOLUTION_ARTIFACT_PROMOTED."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)
    # Don't actually run the tool-registry reload during the test — it
    # touches the shared singleton and would log a warning. Stub it.
    async def _no_reload(skill_name=""): return None
    monkeypatch.setattr("xmclaw.evolution.engine._reload_tool_registry", _no_reload)

    from xmclaw.evolution.engine import EvolutionEngine
    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.evolution.journal import (
        EvolutionJournal, KIND_SKILL, STATUS_SHADOW,
    )

    engine = EvolutionEngine(agent_id="agent_approve")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(engine.db_path)
    engine._journal = EvolutionJournal(store, agent_id="agent_approve")
    cid = await engine._journal.open_cycle(trigger="test_approve")

    # Plant a shadow skill file + metadata that the engine will promote.
    skill_id = "skill_risky_hold"
    shadow_file = Path(engine.skill_forge.shadow_dir) / f"{skill_id}.py"
    shadow_file.parent.mkdir(parents=True, exist_ok=True)
    shadow_file.write_text("# stub skill body\n", encoding="utf-8")
    (shadow_file.with_suffix(".json")).write_text(
        json.dumps({
            "id": skill_id,
            "name": "auto_risky_skill",
            "category": "auto",
            "version": "v1",
            "description": "risky but approved",
            "path": str(shadow_file),
        }),
        encoding="utf-8",
    )
    await engine._journal.record_artifact(cid, KIND_SKILL, skill_id,
                                          status=STATUS_SHADOW)
    await engine._journal.update_artifact_status(skill_id, STATUS_NEEDS_APPROVAL)

    from xmclaw.core.event_bus import get_event_bus, EventType
    seen: list[dict] = []
    async def _h(event):
        seen.append({"type": event.event_type, "payload": event.payload})
    bus = get_event_bus()
    sub_decided = bus.subscribe(EventType.EVOLUTION_APPROVAL_DECIDED.value, _h)
    sub_promoted = bus.subscribe(EventType.EVOLUTION_ARTIFACT_PROMOTED.value, _h)

    try:
        outcome = await engine.approve_artifact(skill_id, approved=True)
        await asyncio.sleep(0.05)

        assert outcome["status"] == "promoted"
        active_path = Path(engine.skill_forge.active_dir) / f"{skill_id}.py"
        assert active_path.exists(), "shadow file must move to active on approve"
        assert not shadow_file.exists(), "shadow file must no longer be in shadow dir"

        health = await engine._journal.get_artifact_health(skill_id)
        assert health["status"] == STATUS_PROMOTED

        decided = [e for e in seen if e["type"] == EventType.EVOLUTION_APPROVAL_DECIDED.value]
        promoted = [e for e in seen if e["type"] == EventType.EVOLUTION_ARTIFACT_PROMOTED.value]
        assert decided and decided[0]["payload"]["approved"] is True
        assert promoted

        # Idempotent: re-approve is a noop.
        again = await engine.approve_artifact(skill_id, approved=True)
        assert again["status"] == "noop"
    finally:
        bus.unsubscribe(sub_decided)
        bus.unsubscribe(sub_promoted)


@pytest.mark.asyncio
async def test_approve_artifact_declines_and_retires(tmp_path, monkeypatch):
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.evolution.journal import (
        EvolutionJournal, KIND_SKILL, STATUS_SHADOW,
    )

    engine = EvolutionEngine(agent_id="agent_decline")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(engine.db_path)
    engine._journal = EvolutionJournal(store, agent_id="agent_decline")
    cid = await engine._journal.open_cycle(trigger="test_decline")

    skill_id = "skill_declined"
    shadow_file = Path(engine.skill_forge.shadow_dir) / f"{skill_id}.py"
    shadow_file.parent.mkdir(parents=True, exist_ok=True)
    shadow_file.write_text("# stub\n", encoding="utf-8")
    (shadow_file.with_suffix(".json")).write_text(
        json.dumps({"id": skill_id, "name": "auto_declined"}),
        encoding="utf-8",
    )
    await engine._journal.record_artifact(cid, KIND_SKILL, skill_id,
                                          status=STATUS_SHADOW)
    await engine._journal.update_artifact_status(skill_id, STATUS_NEEDS_APPROVAL)

    outcome = await engine.approve_artifact(skill_id, approved=False)
    assert outcome["status"] == "retired"
    assert not shadow_file.exists(), "shadow file must be deleted on decline"

    health = await engine._journal.get_artifact_health(skill_id)
    assert health["status"] == STATUS_RETIRED


@pytest.mark.asyncio
async def test_approve_artifact_returns_not_found_for_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine
    engine = EvolutionEngine(agent_id="agent_missing")
    engine.db_path = tmp_path / "mem.db"
    engine.db_path.parent.mkdir(parents=True, exist_ok=True)

    outcome = await engine.approve_artifact("skill_does_not_exist", approved=True)
    assert outcome["status"] == "not_found"
