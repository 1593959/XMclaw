"""EvolutionOrchestrator — unit tests.

Locks the bridge from ``SkillRegistry.promote`` / ``.rollback`` to the
bus:

* Explicit ``promote`` / ``rollback`` emits a matching
  ``SKILL_PROMOTED`` / ``SKILL_ROLLED_BACK`` event carrying the full
  evidence / reason.
* Registry refusal (empty evidence, unknown version) propagates and
  emits NO event — subscribers never see a phantom promotion.
* ``auto_apply=False`` (default) NEVER mutates HEAD on a proposal,
  even if the proposal is valid.
* ``auto_apply=True`` subscribes after ``start()`` and mutates HEAD
  when a well-formed ``SKILL_CANDIDATE_PROPOSED`` event arrives; a
  malformed proposal is logged + skipped without crashing the task.
* ``start`` / ``stop`` are idempotent — mirror the ``EvolutionAgent``
  contract so a daemon bounce doesn't double-subscribe.
"""
from __future__ import annotations

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType, make_event
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.orchestrator import EvolutionOrchestrator
from xmclaw.skills.registry import SkillRegistry, UnknownSkillError


class _NoopSkill(Skill):
    def __init__(self, skill_id: str, version: int) -> None:
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"v": self.version}, side_effects=[])


def _skill(id_: str, v: int) -> _NoopSkill:
    return _NoopSkill(id_, v)


def _manifest(id_: str, v: int) -> SkillManifest:
    return SkillManifest(id=id_, version=v, created_by="human")


def _registry_with(*versions: tuple[str, int]) -> SkillRegistry:
    reg = SkillRegistry()
    for sid, v in versions:
        reg.register(_skill(sid, v), _manifest(sid, v))
    return reg


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry() -> SkillRegistry:
    return _registry_with(("s", 1), ("s", 2))


async def _collect_events(bus: InProcessEventBus) -> list[BehavioralEvent]:
    """Subscribe-all capture helper. Call after ``bus.drain()``."""
    captured: list[BehavioralEvent] = []

    async def _cap(e: BehavioralEvent) -> None:
        captured.append(e)

    bus.subscribe(lambda _e: True, _cap)
    return captured


# ── explicit promote / rollback ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_emits_skill_promoted(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus)

    record = await orch.promote(
        "s", 2, evidence=["plays=12", "mean=0.78"],
    )
    await bus.drain()

    assert record.kind == "promote"
    assert record.to_version == 2
    assert registry.active_version("s") == 2

    promoted = [e for e in captured if e.type == EventType.SKILL_PROMOTED]
    assert len(promoted) == 1
    payload = promoted[0].payload
    assert payload["skill_id"] == "s"
    assert payload["from_version"] == 1
    assert payload["to_version"] == 2
    assert payload["evidence"] == ["plays=12", "mean=0.78"]
    assert promoted[0].agent_id == "orchestrator"


@pytest.mark.asyncio
async def test_rollback_emits_skill_rolled_back(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus)

    await orch.promote("s", 2, evidence=["initial"])
    await bus.drain()
    record = await orch.rollback("s", 1, reason="regression in domain X")
    await bus.drain()

    assert record.kind == "rollback"
    assert registry.active_version("s") == 1

    rolled = [e for e in captured if e.type == EventType.SKILL_ROLLED_BACK]
    assert len(rolled) == 1
    payload = rolled[0].payload
    assert payload["skill_id"] == "s"
    assert payload["from_version"] == 2
    assert payload["to_version"] == 1
    assert payload["reason"] == "regression in domain X"


@pytest.mark.asyncio
async def test_promote_custom_agent_id_overrides_default(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus, agent_id="orch-main")

    await orch.promote(
        "s", 2, evidence=["e"],
        session_id="s-123", agent_id="scheduler-fiber",
    )
    await bus.drain()

    promoted = [e for e in captured if e.type == EventType.SKILL_PROMOTED]
    assert promoted[0].agent_id == "scheduler-fiber"
    assert promoted[0].session_id == "s-123"


# ── registry refusal path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_without_evidence_raises_and_emits_nothing(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus)

    with pytest.raises(ValueError):
        await orch.promote("s", 2, evidence=[])
    await bus.drain()

    assert [e for e in captured if e.type == EventType.SKILL_PROMOTED] == []
    # HEAD untouched.
    assert registry.active_version("s") == 1


@pytest.mark.asyncio
async def test_promote_to_unknown_version_raises_and_emits_nothing(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus)

    with pytest.raises(UnknownSkillError):
        await orch.promote("s", 99, evidence=["e"])
    await bus.drain()

    assert [e for e in captured if e.type == EventType.SKILL_PROMOTED] == []


# ── lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_noop_when_auto_apply_false(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    orch = EvolutionOrchestrator(registry, bus, auto_apply=False)
    await orch.start()
    assert orch.is_running() is False


@pytest.mark.asyncio
async def test_start_subscribes_when_auto_apply_true(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()
    assert orch.is_running() is True
    first = orch._subscription
    # Idempotent: a second start doesn't create a new subscription.
    await orch.start()
    assert orch._subscription is first


@pytest.mark.asyncio
async def test_stop_is_idempotent(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()
    await orch.stop()
    await orch.stop()
    assert orch.is_running() is False


# ── auto-apply path ─────────────────────────────────────────────────────


async def _publish_proposal(
    bus: InProcessEventBus,
    *,
    skill_id: str,
    version: int,
    evidence: list[str],
) -> None:
    await bus.publish(make_event(
        session_id="evolution:evo-1", agent_id="evo-1",
        type=EventType.SKILL_CANDIDATE_PROPOSED,
        payload={
            "winner_candidate_id": skill_id,
            "winner_version": version,
            "evidence": evidence,
            "reason": "all gates cleared",
        },
    ))


@pytest.mark.asyncio
async def test_observe_only_ignores_proposals(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus, auto_apply=False)
    await orch.start()

    await _publish_proposal(bus, skill_id="s", version=2, evidence=["e"])
    await bus.drain()

    # HEAD untouched, no SKILL_PROMOTED.
    assert registry.active_version("s") == 1
    assert [e for e in captured if e.type == EventType.SKILL_PROMOTED] == []


@pytest.mark.asyncio
async def test_auto_apply_promotes_on_proposal(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()

    await _publish_proposal(
        bus, skill_id="s", version=2,
        evidence=["plays=12", "mean=0.78", "gap_over_head=0.13"],
    )
    await bus.drain()

    assert registry.active_version("s") == 2
    promoted = [e for e in captured if e.type == EventType.SKILL_PROMOTED]
    assert len(promoted) == 1
    assert promoted[0].payload["evidence"] == [
        "plays=12", "mean=0.78", "gap_over_head=0.13",
    ]
    # Inherits session_id + agent_id from the source proposal event.
    assert promoted[0].session_id == "evolution:evo-1"
    assert promoted[0].agent_id == "evo-1"


@pytest.mark.asyncio
async def test_auto_apply_skips_malformed_proposal(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()

    # Missing winner_version.
    await bus.publish(make_event(
        session_id="s-x", agent_id="evo-1",
        type=EventType.SKILL_CANDIDATE_PROPOSED,
        payload={
            "winner_candidate_id": "s",
            "evidence": ["plays=12"],
        },
    ))
    # Empty evidence.
    await _publish_proposal(bus, skill_id="s", version=2, evidence=[])
    await bus.drain()

    assert registry.active_version("s") == 1
    assert [e for e in captured if e.type == EventType.SKILL_PROMOTED] == []


@pytest.mark.asyncio
async def test_auto_apply_survives_unknown_skill(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    """Registry refuses unknown versions; the subscription must not crash.

    The bus isolates handler exceptions already, but we assert the
    orchestrator stays subscribed and still processes the next valid
    proposal — proves the except branch is the one that catches it,
    not the bus's generic handler-failure isolator.
    """
    captured = await _collect_events(bus)
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()

    # Unknown version 99 — registry will raise UnknownSkillError.
    await _publish_proposal(
        bus, skill_id="s", version=99, evidence=["mean=0.9"],
    )
    await bus.drain()

    assert registry.active_version("s") == 1
    assert [e for e in captured if e.type == EventType.SKILL_PROMOTED] == []

    # Valid proposal after the bad one still gets applied — proves
    # the subscription survived.
    await _publish_proposal(
        bus, skill_id="s", version=2, evidence=["mean=0.8"],
    )
    await bus.drain()
    assert registry.active_version("s") == 2


@pytest.mark.asyncio
async def test_stop_unsubscribes_auto_apply(
    bus: InProcessEventBus, registry: SkillRegistry,
) -> None:
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)
    await orch.start()
    await orch.stop()

    await _publish_proposal(bus, skill_id="s", version=2, evidence=["e"])
    await bus.drain()

    # HEAD untouched because we unsubscribed.
    assert registry.active_version("s") == 1
