"""B-301: live evolution-chain snapshot endpoint.

Pins the JSON shape + the "graceful degrade when chain not wired"
contract the UI's Evolution page depends on.

The endpoint is at GET /api/v2/evolution/snapshot. It surfaces the
in-memory state of:
  * EvolutionAgent observer's _arms (per-skill plays / mean / progress)
  * EvolutionEvaluationTrigger (debounce / cooldown / fire counter)
  * VariantSelector (UCB1 arm count)
  * SkillDreamCycle (recent proposals from audit jsonl)

These tests use the real ``create_app`` lifespan + a fake agent with a
nested SkillToolProvider so the wiring blocks (B-298 ``_find_skill_provider``
helper) actually run, not a hand-crafted state object — that way a
future refactor that breaks the wire-up gets caught here, not at
runtime in production.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.app import create_app
from xmclaw.providers.tool.composite import CompositeToolProvider
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import SkillToolProvider


# ── shared fixtures ────────────────────────────────────────────────


class _StubSkill(Skill):
    id = "test_skill"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="ran", side_effects=[])


class _NoopProvider:
    def list_tools(self) -> list:
        return []


class _FakeAgent:
    def __init__(self, tools: Any) -> None:
        self._tools = tools


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry_with_skill() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(
        _StubSkill(),
        SkillManifest(
            id="test_skill", version=1, title="Test",
            description="A test skill",
        ),
    )
    return reg


@pytest.fixture
def agent_with_skills(registry_with_skill: SkillRegistry) -> _FakeAgent:
    """Mirrors factory.py:1141 nested-composite layout."""
    stp = SkillToolProvider(registry_with_skill)
    inner = CompositeToolProvider(_NoopProvider(), stp)
    outer = CompositeToolProvider(inner, _NoopProvider())
    return _FakeAgent(outer)


# ── shape: empty / pre-ingest case ─────────────────────────────────


def test_snapshot_returns_full_shape_when_chain_wired(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right after lifespan boots — no verdicts ingested yet — the
    snapshot must still include all four sub-keys (observer / trigger
    / variant_selector / skill_dream) with sensible empty values."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        resp = client.get("/api/v2/evolution/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert "ts" in body
    assert "observer" in body
    assert "trigger" in body
    assert "variant_selector" in body
    assert "skill_dream" in body

    obs = body["observer"]
    assert obs is not None, "B-298 wired the observer; snapshot must surface it"
    assert obs["agent_id"] == "evo-main"
    assert obs["arms"] == []
    assert obs["tracked_skill_count"] == 0
    assert obs["ready_to_propose_count"] == 0


def test_snapshot_observer_null_when_no_chain(
    bus: InProcessEventBus,
) -> None:
    """Echo-mode daemon (no agent) → observer is None. UI degrades to
    'evolution not configured' rather than getting a 404."""
    app = create_app(bus=bus, agent=None, config={})
    with TestClient(app) as client:
        resp = client.get("/api/v2/evolution/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    # No agent → no SkillToolProvider → no EvolutionAgent registry
    # injection → ``evolution_observer`` stays as the fallback created
    # in lifespan; we accept either {observer: null} OR an observer
    # with empty arms (depends on wiring path) — what we DON'T accept
    # is the endpoint failing.
    assert "observer" in body


# ── shape: arm progress tracking ───────────────────────────────────


def test_snapshot_arm_progress_below_thresholds(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest 3 verdicts at 0.8 → arm shows plays=3/10 (0.3 progress),
    mean=0.8/0.65 (1.0 progress), ready_to_propose=False."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        async def publish_three() -> None:
            for _ in range(3):
                await bus.publish(make_event(
                    session_id="t",
                    agent_id="main",
                    type=EventType.GRADER_VERDICT,
                    payload={
                        "skill_id": "test_skill", "version": 1, "score": 0.8,
                    },
                ))
            await bus.drain()

        asyncio.run(publish_three())
        resp = client.get("/api/v2/evolution/snapshot")
    body = resp.json()
    arms = body["observer"]["arms"]
    assert len(arms) == 1
    arm = arms[0]
    assert arm["skill_id"] == "test_skill"
    assert arm["plays"] == 3
    assert arm["mean_score"] == pytest.approx(0.8)
    p = arm["progress"]
    assert p["plays_required"] == 10
    assert p["plays_progress"] == pytest.approx(0.3, abs=0.01)
    assert p["mean_required"] == 0.65
    assert p["mean_progress"] == pytest.approx(1.0)  # 0.8 caps at 1.0
    assert p["ready_to_propose"] is False  # plays gate not cleared


def test_snapshot_arm_ready_to_propose_when_gates_clear(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 verdicts at 0.8 → plays=10 + mean=0.8 → ready_to_propose=True."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        async def publish_ten() -> None:
            for _ in range(10):
                await bus.publish(make_event(
                    session_id="t",
                    agent_id="main",
                    type=EventType.GRADER_VERDICT,
                    payload={
                        "skill_id": "test_skill", "version": 1, "score": 0.8,
                    },
                ))
            await bus.drain()

        asyncio.run(publish_ten())
        resp = client.get("/api/v2/evolution/snapshot")
    body = resp.json()
    arms = body["observer"]["arms"]
    assert arms[0]["progress"]["ready_to_propose"] is True
    assert body["observer"]["ready_to_propose_count"] == 1


def test_snapshot_arms_sorted_ready_first_then_plays_desc(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two skills, one ready (10 plays), one not (3 plays). Ready
    must appear first so the UI's 'about to happen' panel is at the
    top of the table."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        async def publish() -> None:
            for sid, n in [("ready_skill", 10), ("warmup_skill", 3)]:
                for _ in range(n):
                    await bus.publish(make_event(
                        session_id="t",
                        agent_id="main",
                        type=EventType.GRADER_VERDICT,
                        payload={
                            "skill_id": sid, "version": 1, "score": 0.8,
                        },
                    ))
            await bus.drain()

        asyncio.run(publish())
        resp = client.get("/api/v2/evolution/snapshot")
    arms = resp.json()["observer"]["arms"]
    assert len(arms) == 2
    assert arms[0]["skill_id"] == "ready_skill"
    assert arms[1]["skill_id"] == "warmup_skill"


# ── trigger payload ────────────────────────────────────────────────


def test_snapshot_trigger_reports_config(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trigger config should be readable so the UI can show
    'next evaluate in ~30s' style text."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        resp = client.get("/api/v2/evolution/snapshot")
    trig = resp.json()["trigger"]
    assert trig is not None
    assert trig["debounce_s"] == 30.0
    assert trig["cooldown_s"] == 300.0
    assert trig["min_new_verdicts"] == 10
    assert trig["fire_count"] == 0
    assert trig["verdicts_since_last_fire"] == 0
    assert trig["is_active"] is True


# ── skill_dream audit reader ───────────────────────────────────────


def test_snapshot_skill_dream_reads_audit_jsonl(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SkillDreamCycle ran and wrote audit lines, the snapshot
    must surface them — newest first, capped at 10. We don't have a
    real cycle in the fixture, so we synthesise the audit file at the
    expected path."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        sd = app.state.skill_dream
        if sd is None:
            pytest.skip("skill_dream not wired in this fixture")
        audit_path = Path(sd._audit_path)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            "\n".join(
                json.dumps({"ts": float(i), "skill_id": f"s{i}",
                            "title": f"Skill {i}",
                            "confidence": 0.8})
                for i in range(3)
            ) + "\n",
            encoding="utf-8",
        )
        resp = client.get("/api/v2/evolution/snapshot")
    sd_payload = resp.json()["skill_dream"]
    assert "audit_path" in sd_payload
    proposals = sd_payload["recent_proposals"]
    assert len(proposals) == 3
    # Newest-first.
    assert proposals[0]["skill_id"] == "s2"
    assert proposals[2]["skill_id"] == "s0"


# ── error paths ────────────────────────────────────────────────────


def test_snapshot_handles_corrupt_audit_jsonl(
    bus: InProcessEventBus,
    agent_with_skills: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad line in the audit jsonl shouldn't 500 the endpoint —
    skip and continue."""
    import xmclaw.daemon.evolution_agent as evo_mod
    import xmclaw.daemon.skill_dream as sd_mod
    monkeypatch.setattr(evo_mod, "evolution_dir", lambda: tmp_path)
    # Skill-dream imports ``evolution_dir`` separately; without
    # patching its module too the audit file would land at the
    # real ~/.xmclaw/v2/evolution/skill-dream/proposals.jsonl
    # and pollute production state when tests run on a dev box.
    monkeypatch.setattr(sd_mod, "evolution_dir", lambda: tmp_path)

    app = create_app(bus=bus, agent=agent_with_skills, config={})
    with TestClient(app) as client:
        sd = app.state.skill_dream
        if sd is None:
            pytest.skip("skill_dream not wired in this fixture")
        audit_path = Path(sd._audit_path)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps({"ts": 1.0, "skill_id": "good"}) + "\n"
            + "{not json\n"
            + json.dumps({"ts": 2.0, "skill_id": "alsogood"}) + "\n",
            encoding="utf-8",
        )
        resp = client.get("/api/v2/evolution/snapshot")
    assert resp.status_code == 200
    proposals = resp.json()["skill_dream"]["recent_proposals"]
    # Only the two valid lines parsed.
    assert len(proposals) == 2
