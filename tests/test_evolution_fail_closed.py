"""Phase E2 regression tests: fail-closed synthesis / validation.

Pins the three bugs fixed in Phase E2:

* **M22** — forged skills with SyntaxError used to land directly in the active
  loader path. A broken file on disk meant the next registry reload tried to
  import it and either crashed the daemon or disabled valid skills. Now:
  forge writes to `shared/skills/shadow/` only; engine promotes on pass,
  deletes on fail.
* **M23** — validator returned `passed: False` but nothing ENFORCED what that
  meant. The engine now has explicit promote/retire gates keyed on `passed`.
* **fail-closed journal** — every cycle opens a journal row, every artifact
  gets a lineage row, and the verdict is derived from the outcome, not
  reported optimistically.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xmclaw.evolution.engine import (
    _promote_shadow_artifact,
    _retire_shadow_artifact,
)
from xmclaw.evolution.gene_forge import GeneForge
from xmclaw.evolution.skill_forge import SkillForge


# ── M22 / structural: forge targets shadow, NOT active ─────────────────────

def test_skill_forge_points_at_shadow_subdir(tmp_path, monkeypatch):
    """Forge must write to `shared/skills/shadow/`, never the active dir."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    forge = SkillForge()
    assert forge.shadow_dir == tmp_path / "shared" / "skills" / "shadow"
    assert forge.active_dir == tmp_path / "shared" / "skills"
    assert forge.shadow_dir.exists()
    # output_dir is the shadow dir now — the backwards-compat alias must
    # NOT point at the active dir, otherwise we regress bug M22.
    assert forge.output_dir == forge.shadow_dir


def test_gene_forge_points_at_shadow_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    forge = GeneForge()
    assert forge.shadow_dir == tmp_path / "shared" / "genes" / "shadow"
    assert forge.active_dir == tmp_path / "shared" / "genes"
    assert forge.shadow_dir.exists()


# ── promote / retire helpers enforce the fail-closed contract ──────────────

def test_retire_shadow_deletes_py_and_json(tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    py = shadow / "skill_abc.py"
    meta = shadow / "skill_abc.json"
    py.write_text("broken = ???")
    meta.write_text("{}")

    _retire_shadow_artifact(py)

    # BOTH files must be gone — a lingering .json on disk can still confuse
    # manual inspection of shadow state.
    assert not py.exists()
    assert not meta.exists()


def test_retire_shadow_is_idempotent(tmp_path):
    """Already-gone files must not raise. Validation can be retried; retire
    must not crash the cycle."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    ghost = shadow / "skill_missing.py"  # never created
    _retire_shadow_artifact(ghost)  # must not raise


def test_promote_moves_from_shadow_to_active(tmp_path):
    shadow = tmp_path / "shadow"
    active = tmp_path / "active"
    shadow.mkdir()
    py = shadow / "skill_xyz.py"
    meta = shadow / "skill_xyz.json"
    py.write_text("print('ok')")
    meta.write_text('{"id": "skill_xyz"}')

    new_path = _promote_shadow_artifact(py, active)

    assert new_path == active / "skill_xyz.py"
    assert new_path.exists()
    assert (active / "skill_xyz.json").exists()
    # Shadow dir must be empty post-promotion — a stale shadow copy would
    # let a failed-validation retry see a 'ghost' artifact.
    assert not py.exists()
    assert not meta.exists()


# ── engine-level fail-closed: validation failure → shadow file deleted ────

@pytest.mark.asyncio
async def test_generate_skill_retires_shadow_on_validation_failure(
    tmp_path, monkeypatch
):
    """When the validator says `passed: False`, `_generate_skill` must delete
    the shadow file so the next registry reload cannot pick it up (M22)."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine

    engine = EvolutionEngine(agent_id="test_agent")
    # Skip the actual DB write — test is about the shadow/active invariant.
    engine._get_journal = lambda: None  # type: ignore[assignment]
    engine._current_cycle_id = "cycle_test"

    # Stub VFM to accept everything so we reach the validator.
    engine.vfm.score_skill = lambda c: {"total": 99.9}  # type: ignore[method-assign]
    engine.vfm.should_solidify = lambda s, t: True      # type: ignore[method-assign]

    # Stub LLM so forge uses a hard-coded action_body and skips any network.
    async def _fake_complete(messages):
        return '{"action_body": "    return \'ok\'", "parameters": {}}'
    engine.llm.complete = _fake_complete  # type: ignore[method-assign]

    # Force validation to fail.
    async def _fail_validate(path):
        return {"passed": False, "syntax": (False, "SyntaxError: test")}
    engine.validator.validate_skill = _fail_validate  # type: ignore[method-assign]

    insight = {
        "title": "test skill",
        "description": "a skill that will fail validation",
        "source": "tool_usage_analysis",
    }
    result = await engine._generate_skill({"type": "skill", "insight": insight})

    assert result is None
    # Shadow must be empty — no .py, no .json.
    shadow_dir = tmp_path / "shared" / "skills" / "shadow"
    py_files = list(shadow_dir.glob("skill_*.py"))
    json_files = list(shadow_dir.glob("skill_*.json"))
    assert py_files == [], f"shadow still has {py_files} after retire"
    assert json_files == [], f"shadow still has {json_files} after retire"
    # Active must be empty — failed artifacts must NEVER reach the loader path.
    active_dir = tmp_path / "shared" / "skills"
    assert list(active_dir.glob("skill_*.py")) == []


@pytest.mark.asyncio
async def test_generate_skill_promotes_shadow_on_validation_pass(
    tmp_path, monkeypatch
):
    """Happy path: validation passes, shadow → active, file readable by loader."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.engine.BASE_DIR", tmp_path)

    from xmclaw.evolution.engine import EvolutionEngine

    engine = EvolutionEngine(agent_id="test_agent")
    engine._get_journal = lambda: None  # type: ignore[assignment]
    engine._current_cycle_id = "cycle_test"

    engine.vfm.score_skill = lambda c: {"total": 99.9}  # type: ignore[method-assign]
    engine.vfm.should_solidify = lambda s, t: True      # type: ignore[method-assign]

    async def _fake_complete(messages):
        return '{"action_body": "    return \'ok\'", "parameters": {}}'
    engine.llm.complete = _fake_complete  # type: ignore[method-assign]

    async def _pass_validate(path):
        return {"passed": True, "syntax": (True, "OK")}
    engine.validator.validate_skill = _pass_validate  # type: ignore[method-assign]

    # Stub DB insert so we don't need a real sqlite file.
    from xmclaw.memory import sqlite_store as sqlite_mod
    original_insert = sqlite_mod.SQLiteStore.insert_skill

    def _noop_insert(self, agent_id, skill):
        return None
    monkeypatch.setattr(sqlite_mod.SQLiteStore, "insert_skill", _noop_insert)

    # Stub registry reload — we don't have a shared registry in this test.
    async def _noop_reload(skill_name=""):
        return None
    monkeypatch.setattr("xmclaw.evolution.engine._reload_tool_registry", _noop_reload)

    insight = {
        "title": "good skill",
        "description": "a skill that passes",
        "source": "tool_usage_analysis",
    }
    result = await engine._generate_skill({"type": "skill", "insight": insight})

    assert result is not None
    assert result["status"] == "promoted"
    # File must live in the ACTIVE dir, not shadow.
    active_path = Path(result["path"])
    assert active_path.parent == tmp_path / "shared" / "skills"
    assert active_path.exists()
    # And the shadow must be empty — moved, not copied.
    shadow_dir = tmp_path / "shared" / "skills" / "shadow"
    assert list(shadow_dir.glob("skill_*.py")) == []

    # Clean up the monkeypatch so other tests aren't affected.
    monkeypatch.setattr(sqlite_mod.SQLiteStore, "insert_skill", original_insert)


# ── registry loader must skip shadow ───────────────────────────────────────

@pytest.mark.asyncio
async def test_registry_loader_skips_shadow_subdir(tmp_path, monkeypatch):
    """A broken .py placed under `shared/skills/shadow/` must NEVER be loaded
    by the tool registry, even if it matches the skill_*.py glob."""
    monkeypatch.setattr("xmclaw.tools.registry.BASE_DIR", tmp_path)
    skills_dir = tmp_path / "shared" / "skills"
    shadow_dir = skills_dir / "shadow"
    shadow_dir.mkdir(parents=True)
    # Broken skill in shadow — would crash the loader if it were picked up.
    (shadow_dir / "skill_broken.py").write_text("this is !!! not python")

    from xmclaw.tools.registry import ToolRegistry

    registry = ToolRegistry(llm_router=None)
    registry._tools = {}
    await registry._load_generated_skills()
    # No exception, no broken tool loaded.
    assert "skill_broken" not in registry._tools
    assert all("broken" not in n for n in registry._tools)
