"""v2 daemon app — HTTP + WebSocket integration smoke.

Uses FastAPI's ``TestClient`` so this runs without starting uvicorn.
Verifies:
  * /health responds with ok + version
  * WS accepts a user frame, publishes it on the bus, and forwards
    any resulting session-scoped events back to the client
  * Sessions are isolated: events for session A don't leak to session B
  * Disconnect triggers a session_lifecycle(destroy) event
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.app import create_app


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def client(bus: InProcessEventBus) -> TestClient:
    return TestClient(create_app(bus=bus))


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["bus"] == "InProcessEventBus"


def test_lifespan_wires_experiment_loop_factories(bus: InProcessEventBus) -> None:
    """Phase 6.8b: SelfExperimentLoop factories are injected in lifespan."""
    class _FakeAgent:
        pass

    app = create_app(
        bus=bus,
        agent=_FakeAgent(),
        config={"cognition": {"enabled": True}},
    )
    with TestClient(app) as client:
        exp_loop = client.app.state.experiment_loop
        assert exp_loop is not None
        assert exp_loop._baseline_factory is not None
        assert exp_loop._treatment_factory is not None
        assert exp_loop._load_suite is not None
        # baseline factory returns the wired agent
        assert exp_loop._baseline_factory() is client.app.state.agent


def test_ws_connect_emits_session_create(client: TestClient) -> None:
    with client.websocket_connect("/agent/v2/sess-alpha") as ws:
        frame = ws.receive_json()
        assert frame["type"] == EventType.SESSION_LIFECYCLE.value
        assert frame["session_id"] == "sess-alpha"
        assert frame["payload"]["phase"] == "create"


def test_ws_user_message_echoed_via_bus(client: TestClient) -> None:
    with client.websocket_connect("/agent/v2/sess-beta") as ws:
        # Skip the session_create event.
        ws.receive_json()

        ws.send_text(json.dumps({"type": "user", "content": "hi daemon"}))
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "hi daemon"
        assert event["payload"]["channel"] == "ws"


def test_ws_malformed_frame_dropped_but_connection_survives(
    client: TestClient,
) -> None:
    with client.websocket_connect("/agent/v2/sess-mal") as ws:
        ws.receive_json()  # session create

        ws.send_text("{not json")     # bad frame — dropped silently
        ws.send_text(json.dumps({"type": "user", "content": "ok"}))
        # Connection must still work for the follow-up valid frame.
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "ok"


def test_sessions_are_isolated(client: TestClient) -> None:
    """A user message in session A must not appear on session B's socket."""
    with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
         client.websocket_connect("/agent/v2/sess-B") as ws_b:
        # Drain each session's own create event.
        ws_a.receive_json()
        ws_b.receive_json()

        ws_a.send_text(json.dumps({"type": "user", "content": "for A"}))
        evt = ws_a.receive_json()
        assert evt["session_id"] == "sess-A"
        assert evt["payload"]["content"] == "for A"

        # B has no event waiting. Use a short iter_text-style probe:
        # receive_json with timeout not directly supported in TestClient,
        # so we send a follow-up to B and verify A's message did not
        # leak into B's event order.
        ws_b.send_text(json.dumps({"type": "user", "content": "for B"}))
        evt_b = ws_b.receive_json()
        assert evt_b["session_id"] == "sess-B"
        assert evt_b["payload"]["content"] == "for B"


def test_session_destroy_on_disconnect(
    client: TestClient, bus: InProcessEventBus,
) -> None:
    """Disconnect publishes a session_lifecycle(destroy) event on the bus."""
    received: list = []

    async def collect(ev) -> None:  # noqa: ANN001
        received.append(ev)

    bus.subscribe(
        lambda e: (
            e.type == EventType.SESSION_LIFECYCLE
            and e.session_id == "sess-bye"
            and e.payload.get("phase") == "destroy"
        ),
        collect,
    )

    with client.websocket_connect("/agent/v2/sess-bye") as ws:
        ws.receive_json()  # create
    # Connection closed — daemon should now have published destroy.
    # TestClient runs handlers on the same thread, so by here the
    # event has been emitted.
    assert len(received) == 1


def test_unknown_frame_type_is_ignored_for_phase_40(
    client: TestClient,
) -> None:
    """Phase 4.0 only handles 'user' frames. Other frame types are
    silently ignored (no crash, no spurious event). Phase 4.1 wires
    more frame types to scheduler calls."""
    with client.websocket_connect("/agent/v2/sess-ignore") as ws:
        ws.receive_json()  # create
        # NOTE: ``cancel`` is now a WIRED frame type (it emits a
        # session_lifecycle event), so it's no longer a valid stand-in for
        # "unknown". Use a genuinely unrecognised type to test the ignore path.
        ws.send_text(json.dumps({"type": "definitely-not-a-real-frame", "id": "x"}))
        # Follow up with a real user frame — should be the next event.
        ws.send_text(json.dumps({"type": "user", "content": "ok"}))
        event = ws.receive_json()
        assert event["type"] == EventType.USER_MESSAGE.value
        assert event["payload"]["content"] == "ok"


def test_skill_promoted_broadcasts_across_sessions(
    client: TestClient, bus: InProcessEventBus,
) -> None:
    """Epic #4 REPL flash: evolution events must reach every REPL.

    The orchestrator emits ``SKILL_PROMOTED`` with ``session_id="_system"``
    (or the evolution fiber's own id). A naive per-session WS filter would
    swallow those events — nobody's REPL would flash. This test verifies
    both sockets see the promotion regardless of which session triggered it.

    ``with client:`` makes TestClient hold a single shared portal across
    the two websocket_connect blocks — needed so that ``bus.publish``
    called via ``client.portal`` runs on the same loop where the two
    WS handlers subscribed their ``forward`` coroutines.
    """
    with client:
        with client.websocket_connect("/agent/v2/sess-A") as ws_a, \
             client.websocket_connect("/agent/v2/sess-B") as ws_b:
            ws_a.receive_json()  # each session's own create
            ws_b.receive_json()

            # Publish a promotion event on a totally unrelated session id
            # — mimics the orchestrator emitting on "_system".
            async def _pub() -> None:
                await bus.publish(make_event(
                    session_id="_system", agent_id="orchestrator",
                    type=EventType.SKILL_PROMOTED,
                    payload={
                        "skill_id": "email_digest",
                        "from_version": 3,
                        "to_version": 4,
                        "evidence": ["plays=12"],
                    },
                ))
                await bus.drain()
            client.portal.call(_pub)

            evt_a = ws_a.receive_json()
            evt_b = ws_b.receive_json()
            assert evt_a["type"] == EventType.SKILL_PROMOTED.value
            assert evt_b["type"] == EventType.SKILL_PROMOTED.value
            assert evt_a["payload"]["skill_id"] == "email_digest"
            assert evt_b["payload"]["skill_id"] == "email_digest"
            # Event keeps its original session_id — clients format it,
            # they don't rely on it matching their own session.
            assert evt_a["session_id"] == "_system"
            assert evt_b["session_id"] == "_system"


# ── Epic #4 Phase C: create_app orchestrator lifespan wiring ─────────

def _build_orchestrator(
    bus: InProcessEventBus, tmp_path, *, auto_apply: bool,
):
    """Real EvolutionOrchestrator over an empty registry rooted at tmp_path.

    Real object (not a mock) because we want ``is_running`` / ``start`` /
    ``stop`` to exercise the actual subscription path — that's the whole
    thing we're guarding. Empty registry is fine: these tests never call
    ``promote``/``rollback``, they only verify the lifespan hooks fired.
    """
    from xmclaw.skills.orchestrator import EvolutionOrchestrator
    from xmclaw.skills.registry import SkillRegistry

    registry = SkillRegistry(history_dir=tmp_path / "skills")
    return EvolutionOrchestrator(registry, bus, auto_apply=auto_apply)


def test_create_app_without_orchestrator_still_boots(
    bus: InProcessEventBus,
) -> None:
    """``orchestrator=None`` must be a valid shape (first-install path).

    Most users won't have evolution enabled on day one, so ``xmclaw
    serve`` passes ``None`` through. The lifespan must not crash and
    ``app.state.orchestrator`` must read back as ``None`` so downstream
    surfaces can feature-gate on it.
    """
    app = create_app(bus=bus, orchestrator=None)
    with TestClient(app) as client:
        assert app.state.orchestrator is None
        assert client.get("/health").status_code == 200


def test_auto_apply_orchestrator_starts_on_lifespan_enter(
    bus: InProcessEventBus, tmp_path,
) -> None:
    """auto_apply=True → ``is_running()`` must be True inside the lifespan.

    This is the whole point of wiring — a configured orchestrator
    subscribes on daemon boot so SKILL_CANDIDATE_PROPOSED events
    actually reach ``_on_proposal``. If this test fails, the daemon
    exposes the feature flag but silently ignores proposals.
    """
    orch = _build_orchestrator(bus, tmp_path, auto_apply=True)
    assert orch.is_running() is False  # not running before app boots

    app = create_app(bus=bus, orchestrator=orch)
    with TestClient(app) as client:
        assert orch.is_running() is True
        assert app.state.orchestrator is orch
        # Health still works — lifespan didn't block the rest of setup.
        assert client.get("/health").status_code == 200

    # After the ``with`` exits, shutdown runs and stop() cancels the sub.
    assert orch.is_running() is False


def test_observe_only_orchestrator_start_is_noop(
    bus: InProcessEventBus, tmp_path,
) -> None:
    """auto_apply=False: start() returns early, no subscription.

    First-install default. The orchestrator is still on app.state so
    ``/agent/v2/*`` routes can call ``.promote()`` / ``.rollback()``
    explicitly, but the proposal-consumer fiber stays dark until the
    user flips ``evolution.auto_apply=true`` in config.
    """
    orch = _build_orchestrator(bus, tmp_path, auto_apply=False)
    app = create_app(bus=bus, orchestrator=orch)
    with TestClient(app):
        assert orch.is_running() is False
        assert app.state.orchestrator is orch
    assert orch.is_running() is False


def test_orchestrator_startup_failure_does_not_block_daemon(
    bus: InProcessEventBus,
) -> None:
    """A broken orchestrator.start() must NOT take the daemon down.

    Evolution is best-effort observability, not a critical path. If the
    orchestrator's ``start`` raises (disk full writing the audit log,
    corrupt registry file, whatever), the daemon must still serve HTTP
    + WS. app.py catches the exception inside the lifespan.
    """
    class _BrokenOrch:
        started = False
        stopped = False

        async def start(self) -> None:
            _BrokenOrch.started = True
            raise RuntimeError("synthetic start failure")

        async def stop(self) -> None:
            _BrokenOrch.stopped = True

    orch = _BrokenOrch()
    app = create_app(bus=bus, orchestrator=orch)
    with TestClient(app) as client:
        assert _BrokenOrch.started is True
        assert client.get("/health").status_code == 200
        # WS path still works — start() failing didn't tear down the loop.
        with client.websocket_connect("/agent/v2/sess-orch-fail") as ws:
            evt = ws.receive_json()
            assert evt["type"] == EventType.SESSION_LIFECYCLE.value
    # stop() is still called on exit even though start() raised —
    # symmetric cleanup, even if redundant for a broken object.
    assert _BrokenOrch.stopped is True


# ── Epic #24 Phase 1-3 lifespan observers ──────────────────────────


def test_lifespan_starts_evolution_observer(bus: InProcessEventBus) -> None:
    """Phase 1: EvolutionAgent observer must be in app.state after boot.

    Without this the GRADER_VERDICT events emitted by AgentLoop have
    nowhere to land and the entire evolution feedback loop is silent.
    """
    app = create_app(bus=bus)
    with TestClient(app):
        evo = getattr(app.state, "evolution_observer", None)
        assert evo is not None, "EvolutionAgent observer not started"
        assert evo.is_running()
        assert evo.agent_id == "evo-main"


def test_lifespan_starts_journal_writer(bus: InProcessEventBus) -> None:
    """Phase 2.1: JournalWriter must be in app.state and subscribed."""
    app = create_app(bus=bus)
    with TestClient(app):
        jw = getattr(app.state, "journal_writer", None)
        assert jw is not None, "JournalWriter not started"
        assert jw.is_running()


def test_lifespan_starts_profile_extractor(bus: InProcessEventBus) -> None:
    """Phase 2.2: ProfileExtractor must be in app.state and subscribed."""
    app = create_app(bus=bus)
    with TestClient(app):
        pe = getattr(app.state, "profile_extractor", None)
        assert pe is not None, "ProfileExtractor not started"
        assert pe.is_running()


def test_lifespan_starts_skill_dream(bus: InProcessEventBus) -> None:
    """Phase 3.2: SkillDreamCycle must be in app.state and running."""
    app = create_app(bus=bus)
    with TestClient(app):
        sd = getattr(app.state, "skill_dream", None)
        assert sd is not None, "SkillDreamCycle not started"
        assert sd.is_running()
        assert sd.agent_id == "skill-dream"


def test_lifespan_skill_dream_disabled_via_config(bus: InProcessEventBus) -> None:
    """``evolution.skill_dream.enabled=false`` keeps SkillDreamCycle out."""
    cfg = {"evolution": {"skill_dream": {"enabled": False}}}
    app = create_app(bus=bus, config=cfg)
    with TestClient(app):
        sd = getattr(app.state, "skill_dream", None)
        # Disabled = state attr exists but is None (not constructed).
        assert sd is None


def test_lifespan_subscribes_user_profile_updated_handler(
    bus: InProcessEventBus,
) -> None:
    """Phase 2.4 wiring: lifespan must register a subscription that
    listens for USER_PROFILE_UPDATED → ``bump_prompt_freeze_generation``.

    Without this hook, the persona assembler keeps serving cached
    system prompts and new auto-extracted preferences never reach the
    agent until daemon restart. We don't need to dispatch the event
    here — just assert at least one subscription exists for the type
    after lifespan boots."""
    app = create_app(bus=bus)
    with TestClient(app):
        # InProcessEventBus exposes ``_handlers`` as a mapping of
        # subscriptions; we count the ones whose filter accepts a
        # USER_PROFILE_UPDATED event.
        from xmclaw.core.bus.events import EventType, make_event
        sample = make_event(
            session_id="probe", agent_id="probe",
            type=EventType.USER_PROFILE_UPDATED,
            payload={},
        )
        # ``_subs`` is the live subscription list inside InProcessEventBus.
        subs = getattr(bus, "_subs", None)
        assert subs is not None, "InProcessEventBus shape changed"
        matched = [s for s in subs if s.predicate(sample)]
        assert len(matched) >= 1, (
            "no subscription registered for USER_PROFILE_UPDATED — "
            "Phase 2.4 prompt cache invalidation hook missing"
        )


# ── B-348 (Sprint 1): single-tab-wins per session ─────────────────


def _drain_until_supersede(ws, max_frames: int = 20):
    """Read up to ``max_frames`` frames from ``ws`` and return the
    first one whose ``type == "superseded"``. Tolerates any
    intermediate session_replay / session_lifecycle frames the
    bus forwards before the supersede arrives. Returns None if
    nothing matched within the budget.
    """
    for _ in range(max_frames):
        try:
            f = ws.receive_json()
        except Exception:  # noqa: BLE001 — disconnect mid-drain
            return None
        if isinstance(f, dict) and f.get("type") == "superseded":
            return f
    return None


def test_b348_second_ws_to_same_session_supersedes_first(
    client: TestClient,
) -> None:
    """Pre-B-348 opening tab 2 on the same session_id left BOTH WS
    subscribed to the bus. The bus fanout doubled (every event
    delivered twice), and ``cancel`` from either tab cancelled the
    shared run_turn. Now: tab 2 supersedes tab 1.
    """
    sid = "sess-b348-supersede"
    with client.websocket_connect(f"/agent/v2/{sid}") as ws_first:
        ws_first.receive_json()  # session_create

        with client.websocket_connect(f"/agent/v2/{sid}") as ws_second:
            # Tab 1 must receive a supersede frame. Other frames may
            # arrive first (replay markers, the second-tab session
            # create that the still-alive sub forwards) so we scan.
            sup = _drain_until_supersede(ws_first)
            assert sup is not None, (
                "first tab did not receive any supersede frame"
            )
            assert sup["payload"]["session_id"] == sid
            assert sup["payload"]["reason"] == "another_tab_connected"
            # Sanity: second tab is alive and got its own create event
            # somewhere in the first few frames.
            saw_create_for_second = False
            for _ in range(20):
                try:
                    f = ws_second.receive_json()
                except Exception:  # noqa: BLE001
                    break
                if (
                    f.get("type") == "session_lifecycle"
                    and f.get("payload", {}).get("phase") == "create"
                    and f.get("session_id") == sid
                ):
                    saw_create_for_second = True
                    break
            assert saw_create_for_second, (
                "second tab never received its session_create event"
            )


def test_b348_third_ws_supersedes_second_after_first_disconnects(
    client: TestClient,
) -> None:
    """When tab 1 disconnects AFTER being superseded by tab 2, the
    finally-block must NOT pop tab 2's registration from the
    active_ws_for_session map. We can't peek at the dict from the
    test directly, so we exit tab 1's context (triggering its
    finally), then open tab 3 and verify tab 2 still receives a
    supersede frame — proving tab 2 was still the registered owner.
    """
    sid = "sess-b348-three-way"
    # Open tab 1, immediately supersede with tab 2, then exit tab 1.
    with client.websocket_connect(f"/agent/v2/{sid}") as ws_a:
        ws_a.receive_json()
        with client.websocket_connect(f"/agent/v2/{sid}") as ws_b:
            assert _drain_until_supersede(ws_a) is not None
            # ws_a's `with` will exit when this inner block continues —
            # but we want ws_b to outlive ws_a's disconnect. Open ws_c
            # while still inside ws_b's context.
            with client.websocket_connect(f"/agent/v2/{sid}") as ws_c:
                _ = ws_c  # silence unused
                sup_b = _drain_until_supersede(ws_b)
                assert sup_b is not None, (
                    "B-348 dict-cleanup race: tab 2 was already popped "
                    "from active_ws_for_session before tab 3 connected — "
                    "likely ws_a's finally clobbered ws_b's registration"
                )


# ── Phase 6: CognitiveDaemon end-to-end tick wiring ───────────────────


@pytest.mark.asyncio
async def test_cognitive_daemon_tick_e2e_lifespan_wiring(bus: InProcessEventBus) -> None:
    """Full tick cycle on real lifespan wiring: percept → attention →
    daemon tick → event bus publish.

    Uses a fake agent so no LLM is required; the assertion is that the
    tick completes without error and the summary contains the expected
    keys.  This guards against regressions where a collaborator
    (ReasoningEngine, Planner, ActionDispatcher) is wired with the
    wrong shape and silently fails inside tick_once's try/except.
    """
    class _FakeAgent:
        async def run_turn(self, session_id: str, prompt: str):
            class _Res:
                text = "ok"
                hops = 1
                cost_usd = 0.0
                ok = True
                tool_calls = []
            return _Res()

    app = create_app(
        bus=bus,
        agent=_FakeAgent(),
        config={"cognition": {"enabled": True}},
    )
    with TestClient(app) as client:
        daemon = client.app.state.cognitive_daemon
        assert daemon is not None, "cognitive_daemon not wired in lifespan"

        # Drive one tick deterministically (no wall-clock sleep).
        # The daemon may have already ticked in the background since
        # lifespan start() spawns _run(); use tick_count to know where
        # we are.
        before = daemon.tick_count
        summary = await daemon.tick_once()
        assert summary["tick"] == before + 1
        assert summary["errors"] == []
        assert "n_percepts" in summary
        assert "n_actionable" in summary
        assert "n_goals_spawned" in summary
        assert "n_plans_executed" in summary
        assert "ran_experiment" in summary
        assert "n_reflections" in summary
        assert "n_skill_proposals" in summary

        # Verify the tick event was published on the bus.
        tick_events = []
        async def _collect(ev) -> None:  # noqa: ANN001
            if ev.type == EventType.COGNITIVE_DAEMON_TICK:
                tick_events.append(ev)

        bus.subscribe(
            lambda e: e.type == EventType.COGNITIVE_DAEMON_TICK, _collect,
        )
        await daemon.tick_once()
        # publish spawns async tasks for subscribers; drain waits for
        # them to finish before we assert.
        await bus.drain()
        assert len(tick_events) >= 1
        assert tick_events[-1].payload["tick"] == daemon.tick_count


@pytest.mark.asyncio
async def test_cognitive_daemon_config_hot_reload(
    bus: InProcessEventBus, tmp_path: Path,
) -> None:
    """CONFIG_RELOADED with cognition changes updates daemon config live."""
    class _FakeAgent:
        async def run_turn(self, session_id: str, prompt: str):
            class _Res:
                text = "ok"
                hops = 1
                cost_usd = 0.0
                ok = True
                tool_calls = []
            return _Res()

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "cognition": {
                "enabled": True,
                "continuous_loop": {
                    "autonomy_level": 50,
                    "heartbeat_hz": 1.0,
                },
            },
        }),
        encoding="utf-8",
    )

    app = create_app(
        bus=bus,
        agent=_FakeAgent(),
        config={
            "cognition": {
                "enabled": True,
                "continuous_loop": {
                    "autonomy_level": 50,
                    "heartbeat_hz": 1.0,
                },
            },
        },
        config_path=config_path,
    )
    with TestClient(app) as client:
        daemon = client.app.state.cognitive_daemon
        assert daemon is not None
        assert daemon.config.autonomy_level == 50
        assert daemon.config.heartbeat_hz == 1.0

        # Mutate the in-memory config dict (mirrors what ConfigFileWatcher
        # does when it detects a file change) and fire CONFIG_RELOADED.
        cfg = client.app.state.config
        cfg["cognition"]["continuous_loop"]["autonomy_level"] = 75
        cfg["cognition"]["continuous_loop"]["heartbeat_hz"] = 2.5
        event = make_event(
            session_id="test",
            agent_id="test",
            type=EventType.CONFIG_RELOADED,
            payload={"top_changed": ["cognition"]},
        )
        await bus.publish(event)
        await bus.drain()

        assert daemon.config.autonomy_level == 75
        assert daemon.config.heartbeat_hz == 2.5
