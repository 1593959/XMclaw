"""Epic #4 exit-criterion integration test — "evolution is visible".

The per-module tests already lock each link of the chain individually:

* :mod:`tests.unit.test_v2_evolution_orchestrator` — ``promote``/``rollback``
  emit ``SKILL_PROMOTED`` / ``SKILL_ROLLED_BACK`` on the bus; ``auto_apply``
  consumes ``SKILL_CANDIDATE_PROPOSED`` and mutates ``SkillRegistry`` HEAD.
* :mod:`tests.integration.test_v2_daemon_app` ::
  ``test_skill_promoted_broadcasts_across_sessions`` — daemon's WS
  forwarder sees a ``SKILL_PROMOTED`` published with ``session_id="_system"``
  and broadcasts it to every connected REPL (``_GLOBAL_EVENT_TYPES``).
* :mod:`tests.unit.test_v2_chat_formatter` — REPL ``format_event``
  renders the three evolution types as ``[evolved]`` (green) /
  ``[rolled back]`` (yellow) / ``[candidate]`` (dim).

What none of those cover is the **full stack wired on one bus**: a
single publish of ``SKILL_CANDIDATE_PROPOSED`` must travel
Orchestrator → Registry (HEAD mutation, anti-req #12 enforced) → bus
(``SKILL_PROMOTED``) → daemon WS forwarder → every REPL's
``format_event`` as a user-visible green flash. If any adapter drifts
(orchestrator forgets to emit, forwarder's global-event set loses an
entry, formatter's branch goes missing), a unit test still passes but
a real user would not see their agent evolve. This test is the one
assertion that prevents that class of silent regression.

Exit criterion from ``docs/DEV_ROADMAP.md`` Epic #4 §4 退出标准:
*集成测试 ``tests/integration/test_evolution_visible.py`` 全绿.*
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xmclaw.cli.chat import format_event
from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.app import create_app
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.orchestrator import EvolutionOrchestrator
from xmclaw.skills.registry import SkillRegistry


class _NoopSkill(Skill):
    """Minimal skill — the test only cares about HEAD version movement,
    not invocation behaviour."""

    def __init__(self, skill_id: str, version: int) -> None:
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"v": self.version}, side_effects=[])


def _registry_with_two_versions(skill_id: str = "demo.sum") -> SkillRegistry:
    """Registry where HEAD=v1 and v2 is registered-but-inactive.

    An evolution proposal for v2 is the canonical "I found a better
    prompt during the bandit phase, please promote it" shape.
    """
    reg = SkillRegistry()
    reg.register(_NoopSkill(skill_id, 1), SkillManifest(id=skill_id, version=1))
    reg.register(_NoopSkill(skill_id, 2), SkillManifest(id=skill_id, version=2))
    return reg


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry() -> SkillRegistry:
    return _registry_with_two_versions()


@pytest.fixture
def client(bus: InProcessEventBus) -> TestClient:
    return TestClient(create_app(bus=bus))


def test_proposal_reaches_every_repl_as_green_evolved_flash(
    client: TestClient,
    bus: InProcessEventBus,
    registry: SkillRegistry,
) -> None:
    """End-to-end: a single ``SKILL_CANDIDATE_PROPOSED`` publish results
    in every connected REPL seeing a green ``[evolved]`` flash.

    The orchestrator runs ``auto_apply=True`` so the daemon itself is a
    passthrough — it owns WS forwarding, not evolution policy. This
    mirrors the production wiring intent: orchestrator and daemon share
    one bus, each component minds its own slice.
    """
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)

    with client:
        # Shared portal across the two WS connects — required so the
        # ``_pub`` coroutine runs on the same event loop the daemon's
        # WS handlers subscribed their ``forward`` coroutines to.
        client.portal.call(orch.start)

        with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
             client.websocket_connect("/agent/v2/sess-B") as ws_b:
            # Each REPL receives its own session_create frame first.
            ws_a.receive_json()
            ws_b.receive_json()

            # HEAD is v1 before the proposal.
            assert registry.active_version("demo.sum") == 1

            async def _publish_proposal() -> None:
                await bus.publish(make_event(
                    session_id="evolution:evo-1", agent_id="evo-1",
                    type=EventType.SKILL_CANDIDATE_PROPOSED,
                    payload={
                        "winner_candidate_id": "demo.sum",
                        "winner_version": 2,
                        "evidence": ["plays=12", "mean=0.78", "gap=0.13"],
                        "reason": "all gates cleared",
                    },
                ))
                await bus.drain()

            client.portal.call(_publish_proposal)

            # ── stack assertions ────────────────────────────────────
            # 1. Registry mutated — anti-req #12 evidence cleared.
            assert registry.active_version("demo.sum") == 2, (
                "orchestrator.auto_apply=True should have consumed the "
                "proposal and moved HEAD to v2"
            )

            # 2. Both REPLs received a frame. First frame off each socket
            # may be the CANDIDATE proposal (also in _GLOBAL_EVENT_TYPES);
            # scan up to two frames per socket for the PROMOTED one.
            promoted_a = _receive_first_of_type(
                ws_a, EventType.SKILL_PROMOTED.value,
            )
            promoted_b = _receive_first_of_type(
                ws_b, EventType.SKILL_PROMOTED.value,
            )
            for evt in (promoted_a, promoted_b):
                assert evt["type"] == EventType.SKILL_PROMOTED.value
                assert evt["payload"]["skill_id"] == "demo.sum"
                assert evt["payload"]["from_version"] == 1
                assert evt["payload"]["to_version"] == 2
                assert evt["payload"]["evidence"] == [
                    "plays=12", "mean=0.78", "gap=0.13",
                ]
                # session_id is inherited from the source proposal event,
                # NOT rewritten to the receiving REPL's session.
                assert evt["session_id"] == "evolution:evo-1"

            # 3. REPL formatter renders each frame as a green flash.
            line_a = format_event(promoted_a)
            line_b = format_event(promoted_b)
            for line in (line_a, line_b):
                assert line is not None
                assert "\x1b[32m" in line.text      # green
                assert "\x1b[0m" in line.text       # reset
                assert "evolved" in line.text.lower()
                assert "demo.sum" in line.text
                assert "v1" in line.text and "v2" in line.text

        client.portal.call(orch.stop)


def test_rollback_reaches_every_repl_as_yellow_flash(
    client: TestClient,
    bus: InProcessEventBus,
    registry: SkillRegistry,
) -> None:
    """Same pipeline, rollback path: ``orch.rollback`` → bus
    ``SKILL_ROLLED_BACK`` → all REPLs format as yellow ``[rolled back]``.

    The rollback is the "the promoted skill regressed in production —
    revert HEAD" safety lever. It must be as visible as the promotion.
    """
    orch = EvolutionOrchestrator(registry, bus)

    with client:
        # Promote v2 first so a rollback to v1 is actually a HEAD move.
        async def _promote_then_rollback() -> None:
            await orch.promote("demo.sum", 2, evidence=["baseline"])
            # drain so the SKILL_PROMOTED frame lands before we open WS.
            await bus.drain()
        client.portal.call(_promote_then_rollback)
        assert registry.active_version("demo.sum") == 2

        with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
             client.websocket_connect("/agent/v2/sess-B") as ws_b:
            ws_a.receive_json()
            ws_b.receive_json()

            async def _do_rollback() -> None:
                await orch.rollback(
                    "demo.sum", 1, reason="regression in domain X",
                )
                await bus.drain()
            client.portal.call(_do_rollback)

            # HEAD back to v1.
            assert registry.active_version("demo.sum") == 1

            rb_a = _receive_first_of_type(
                ws_a, EventType.SKILL_ROLLED_BACK.value,
            )
            rb_b = _receive_first_of_type(
                ws_b, EventType.SKILL_ROLLED_BACK.value,
            )
            for evt in (rb_a, rb_b):
                assert evt["payload"]["skill_id"] == "demo.sum"
                assert evt["payload"]["from_version"] == 2
                assert evt["payload"]["to_version"] == 1
                assert evt["payload"]["reason"] == "regression in domain X"

            line_a = format_event(rb_a)
            line_b = format_event(rb_b)
            for line in (line_a, line_b):
                assert line is not None
                assert "\x1b[33m" in line.text      # yellow
                assert "rolled back" in line.text.lower()
                assert "regression in domain X" in line.text


def test_empty_evidence_refused_and_invisible_to_repls(
    client: TestClient,
    bus: InProcessEventBus,
    registry: SkillRegistry,
) -> None:
    """Anti-req #12 at the stack level: a proposal with empty evidence
    must not move HEAD AND must not flash in any REPL. If the registry
    refusal ever leaked a ``SKILL_PROMOTED``, users would see a fake
    evolution on screen — the worst possible failure mode for the
    "visible evolution" claim.

    TestClient's sync ``receive_json`` has no timeout hook, so we prove
    "no PROMOTED emitted" the only way that's deterministic: publish a
    bad proposal, then a good one, then walk the WS frame stream in
    order up to the good proposal's ``SKILL_PROMOTED`` sentinel and
    assert exactly one PROMOTED arrived (the good one).
    """
    orch = EvolutionOrchestrator(registry, bus, auto_apply=True)

    with client:
        client.portal.call(orch.start)

        with client.websocket_connect("/agent/v2/sess-A") as ws_a:
            ws_a.receive_json()  # session_create

            async def _publish_bad_then_good() -> None:
                # Anti-req #12 trip — must not produce a PROMOTED.
                await bus.publish(make_event(
                    session_id="evolution:evo-1", agent_id="evo-1",
                    type=EventType.SKILL_CANDIDATE_PROPOSED,
                    payload={
                        "winner_candidate_id": "demo.sum",
                        "winner_version": 2,
                        "evidence": [],
                    },
                ))
                # Sentinel — will cleanly produce exactly one PROMOTED.
                await bus.publish(make_event(
                    session_id="evolution:evo-1", agent_id="evo-1",
                    type=EventType.SKILL_CANDIDATE_PROPOSED,
                    payload={
                        "winner_candidate_id": "demo.sum",
                        "winner_version": 2,
                        "evidence": ["plays=15", "mean=0.80"],
                    },
                ))
                await bus.drain()
            client.portal.call(_publish_bad_then_good)

            # Only the sentinel should have moved HEAD.
            assert registry.active_version("demo.sum") == 2

            # After bus.drain, the forwarder queue is processed in
            # order. Expected frames: CANDIDATE(bad), CANDIDATE(good),
            # PROMOTED(good). Read exactly three and classify —
            # asserting exactly one PROMOTED + that it carries the
            # good evidence proves the bad proposal was silently
            # dropped with zero side-effects reaching the REPL.
            frames = [ws_a.receive_json() for _ in range(3)]
            promoted = [
                f for f in frames
                if f["type"] == EventType.SKILL_PROMOTED.value
            ]
            assert len(promoted) == 1, (
                "expected exactly one SKILL_PROMOTED (the sentinel); "
                f"got {len(promoted)}. frames: "
                f"{[f['type'] for f in frames]}"
            )
            assert promoted[0]["payload"]["evidence"] == [
                "plays=15", "mean=0.80",
            ], "the surviving PROMOTED must be the good sentinel, not a phantom"

        client.portal.call(orch.stop)


# ── helpers ─────────────────────────────────────────────────────────────


def _receive_first_of_type(ws, target_type: str, max_frames: int = 4) -> dict:
    """Pull WS frames until one matches ``target_type``.

    The WS delivers everything in the global set (candidate + promoted
    + rolled_back), so the test can't assume ordering between e.g. the
    CANDIDATE proposal and the PROMOTED that follows. Bounded scan to
    keep a broken daemon from hanging the suite.
    """
    for _ in range(max_frames):
        frame = ws.receive_json()
        if frame.get("type") == target_type:
            return frame
    raise AssertionError(
        f"no frame of type {target_type!r} arrived in {max_frames} reads"
    )


