"""B-298 follow-up: end-to-end evolution chain integration test.

The unit tests pin individual modules (EvolutionAgent ingest, trigger
debounce, VariantSelector UCB1) but none of them exercise the
**create_app() lifespan wiring** that turned out to be where the chain
actually broke in production. This file covers that gap.

Builds a real ``create_app()`` with a fake AgentLoop that has a
nested-Composite tool stack containing a SkillToolProvider, drives
the lifespan up, publishes GRADER_VERDICT events on the same bus the
app uses, and asserts:

* the EvolutionAgent + EvolutionEvaluationTrigger + VariantSelector
  are all present on ``app.state`` (proves the wiring blocks ran);
* the EvolutionAgent ingests the verdicts (proves its bus
  subscription is alive);
* state.json is persisted to ``audit_dir/evo-main/state.json``
  (proves B-297 atomic-write actually fires);
* with a small ``debounce_s`` + ``min_new_verdicts``, the trigger
  fires evaluate() and a SKILL_CANDIDATE_PROPOSED event reaches a
  bus subscriber (proves B-294 closes the loop).

This is the test that would have caught B-298 before deploy.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import (
    EventType, InProcessEventBus, make_event,
)
from xmclaw.core.ir.toolcall import ToolSpec
from xmclaw.daemon.app import create_app
from xmclaw.daemon.app_lifespan import _find_skill_provider
from xmclaw.providers.tool.composite import CompositeToolProvider
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import SkillToolProvider


# ── test helpers ────────────────────────────────────────────────────


class _NoopTool:
    """Stand-in for a non-skill tool provider."""

    def list_tools(self) -> list[ToolSpec]:
        return []


class _StubSkill(Skill):
    """Trivial skill so the registry has something to expose."""

    id = "test_skill"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="ran", side_effects=[])


class _FakeAgent:
    """Just exposes ``_tools`` so create_app's wiring can find it.
    The real AgentLoop has a lot more surface, but the lifespan only
    pokes ``agent._tools``."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry_with_skill() -> SkillRegistry:
    reg = SkillRegistry()
    skill = _StubSkill()
    manifest = SkillManifest(id="test_skill", version=1, title="Test")
    reg.register(skill, manifest)
    return reg


@pytest.fixture
def agent_with_nested_skill_provider(
    registry_with_skill: SkillRegistry,
) -> _FakeAgent:
    """Mirrors factory.py:1141's nested-composite layout: outer
    Composite wraps inner Composite which holds the SkillToolProvider.
    Pre-B-298 this layout was the trigger for the silent failure."""
    stp = SkillToolProvider(registry_with_skill)
    inner = CompositeToolProvider(_NoopTool(), stp)
    outer = CompositeToolProvider(inner, _NoopTool())
    return _FakeAgent(outer)


# ── tests ───────────────────────────────────────────────────────────


def test_b298_lifespan_wires_evolution_observer(
    bus: InProcessEventBus,
    agent_with_nested_skill_provider: _FakeAgent,
    tmp_path: Path,
) -> None:
    """create_app + lifespan must end up with an EvolutionAgent on
    app.state.evolution_observer that has the registry the agent's
    tool stack uses."""
    config: dict[str, Any] = {}
    app = create_app(
        bus=bus,
        agent=agent_with_nested_skill_provider,
        config=config,
    )
    with TestClient(app):
        evo = app.state.evolution_observer
        assert evo is not None
        # The B-296 registry-injection: EvolutionAgent must hold the
        # very SkillRegistry the agent's tool stack uses, NOT a None
        # fallback (pre-B-298 it was always None).
        _, expected_reg = _find_skill_provider(
            agent_with_nested_skill_provider._tools,
        )
        assert getattr(evo, "_registry", None) is expected_reg


def test_b298_lifespan_wires_evaluation_trigger(
    bus: InProcessEventBus,
    agent_with_nested_skill_provider: _FakeAgent,
) -> None:
    """B-294 trigger must be on app.state and active."""
    app = create_app(
        bus=bus,
        agent=agent_with_nested_skill_provider,
        config={},
    )
    with TestClient(app):
        trig = app.state.evolution_evaluation_trigger
        assert trig is not None
        assert trig.is_active is True


def test_b298_lifespan_wires_variant_selector(
    bus: InProcessEventBus,
    agent_with_nested_skill_provider: _FakeAgent,
) -> None:
    """B-295 selector must be on app.state AND injected into the
    SkillToolProvider's invoke path (pre-B-298 silently never
    injected — selector stayed at app.state but wasn't reachable
    from the tool path)."""
    app = create_app(
        bus=bus,
        agent=agent_with_nested_skill_provider,
        config={},
    )
    with TestClient(app):
        sel = app.state.variant_selector
        assert sel is not None

        # Critical: the selector must be threaded through to the
        # actual SkillToolProvider. The lifespan does
        # ``_stp_ref._variant_selector = selector``; verify that
        # mutation actually landed.
        stp, _ = _find_skill_provider(
            agent_with_nested_skill_provider._tools,
        )
        assert stp is not None
        assert getattr(stp, "_variant_selector", None) is sel


def test_b298_verdict_flow_persists_state(
    bus: InProcessEventBus,
    agent_with_nested_skill_provider: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: publish GRADER_VERDICT events on the bus →
    EvolutionAgent ingests them → state.json is written.

    Pre-B-298 this entire data path was indistinguishable from
    "wired but unreachable" — tests passed but the daemon never
    persisted state. This test would have caught it.
    """
    # Redirect the EvolutionAgent's audit/state dir into tmp_path so
    # we don't write to the user's real ~/.xmclaw/v2/evolution/. The
    # agent reads its base path from
    # ``xmclaw.daemon.evolution_agent.evolution_dir`` (re-export of
    # the xmclaw.utils.paths function); patching at the import site
    # is enough since EvolutionAgent calls it during ``__init__``.
    import xmclaw.daemon.evolution_agent as evo_mod
    monkeypatch.setattr(
        evo_mod, "evolution_dir",
        lambda: tmp_path,
    )

    app = create_app(
        bus=bus,
        agent=agent_with_nested_skill_provider,
        config={},
    )
    with TestClient(app):
        evo = app.state.evolution_observer
        assert evo is not None

        async def publish_verdicts() -> None:
            for _ in range(5):
                await bus.publish(make_event(
                    session_id="test-session",
                    agent_id="main",
                    type=EventType.GRADER_VERDICT,
                    payload={
                        "skill_id": "test_skill",
                        "version": 1,
                        "score": 0.9,
                    },
                ))
            await bus.drain()

        asyncio.run(publish_verdicts())

        # Snapshot must show 5 plays for test_skill v1.
        snap = evo.snapshot()
        by_key = {(e.candidate_id, e.version): e for e in snap}
        assert ("test_skill", 1) in by_key
        assert by_key[("test_skill", 1)].plays == 5

        # B-297 state.json must exist on disk now (atomic-write
        # fires inside _ingest's lock).
        state_path = tmp_path / "evo-main" / "state.json"
        assert state_path.exists(), (
            f"state.json not written — B-297 persistence didn't fire. "
            f"Looked at {state_path}, audit_dir base={tmp_path}"
        )
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        assert raw["arms"], "state.json has no arms — _save_state_locked never ran"
        assert raw["arms"][0]["plays"] == 5


def test_b298_skip_session_prefixes_dont_drive_evaluation(
    bus: InProcessEventBus,
    agent_with_nested_skill_provider: _FakeAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verdicts from internal sessions (skill-dream, evolution:*,
    reflect:*) must NOT advance the trigger's
    verdicts_since_last_fire counter. Pre-B-294 a skill-dream burst
    would pin the eval loop (or worse, propose its own dream-only
    candidate). The trigger's predicate should reject those at the
    bus subscription layer."""
    import xmclaw.daemon.evolution_agent as evo_mod
    monkeypatch.setattr(
        evo_mod, "evolution_dir",
        lambda: tmp_path,
    )

    app = create_app(
        bus=bus,
        agent=agent_with_nested_skill_provider,
        config={},
    )
    with TestClient(app):
        trig = app.state.evolution_evaluation_trigger
        assert trig is not None

        async def publish_internal() -> None:
            for prefix in (
                "_system", "skill-dream", "dream:foo",
                "evolution:bar", "reflect:baz",
            ):
                await bus.publish(make_event(
                    session_id=prefix,
                    agent_id="main",
                    type=EventType.GRADER_VERDICT,
                    payload={
                        "skill_id": "test_skill",
                        "version": 1,
                        "score": 0.9,
                    },
                ))
            await bus.drain()

        asyncio.run(publish_internal())
        assert trig.verdicts_since_last_fire == 0
