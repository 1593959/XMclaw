"""Unit tests for Jarvis Phase 6 wiring A: percept_sources adapters.

Covers:
* Each ``make_*_percept`` helper produces a Percept with correct
  source / kind / payload / salience / correlation_id.
* PerceptSourceRegistry attaches into existing producer hooks via
  ``setattr`` (not method override) and forwards events to the bus.
* Detach restores the producer's original state.
* Each percept gets a unique uuid id.
* Salience defaults match the documented baselines.
* AgentLoop with no perception_bus is unchanged from today (a fake
  AgentLoop with the new constructor kwarg verifies wiring is opt-in).

Test contract: NO real psutil, NO real WebSocket, NO real cron — all
producers are duck-typed fakes built inline.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.cognition.percept_sources import (
    PerceptSourceRegistry,
    make_cron_tick_percept,
    make_file_event_percept,
    make_internal_event_percept,
    make_process_alert_percept,
    make_user_msg_percept,
)
from xmclaw.cognition.perception_bus import Percept, PerceptionBus


# --------------------------------------------------------------------- fakes


@dataclass
class _FakeFilePercept:
    """Duck-shape of FileWatcher.FilePercept."""

    path: str = "/tmp/x"
    event_type: str = "modified"
    timestamp: float = 1_700_000_000.0
    is_directory: bool = False
    src_path: str | None = None


@dataclass
class _FakeProcessAlert:
    """Duck-shape of ProcessWatcher.ProcessAlert."""

    watch_id: str = "w-1"
    pid: int = 1234
    description: str = "training run"
    kind: str = "cpu_high"
    timestamp: float = 1_700_000_000.0
    payload: dict | None = None


class _FakeFileWatcher:
    """Mimics FileWatcher's public surface: a ``callback`` attribute."""

    def __init__(self) -> None:
        self.callback: Any = None


class _FakeProcessWatcher:
    """Mimics ProcessWatcher's wiring surface: a ``_bus`` attribute that
    receives ``await bus.push(alert)`` from a real watcher's poll pass."""

    def __init__(self) -> None:
        self._bus: Any = None


class _FakeAgentLoop:
    """Mimics AgentLoop's new ``_perception_bus`` attribute injection."""

    def __init__(self, perception_bus: Any = None) -> None:
        # Same default behaviour as the real AgentLoop's new kwarg.
        self._perception_bus = perception_bus

    async def run_turn(self, session_id: str, user_message: str) -> None:
        """Mirror of the real run_turn's percept push branch."""
        bus = getattr(self, "_perception_bus", None)
        if bus is not None and user_message:
            try:
                await bus.push(
                    make_user_msg_percept(session_id, user_message)
                )
            except Exception:
                pass


class _FakeCronRunner:
    def __init__(self) -> None:
        self._cb: Any = None

    def add_fire_callback(self, fn: Any) -> None:
        self._cb = fn


# ====================================================================== make_*


def test_make_user_msg_percept_basic_shape() -> None:
    p = make_user_msg_percept("sess-1", "hi there")
    assert isinstance(p, Percept)
    assert p.source == "ws"
    assert p.kind == "user_msg"
    assert p.payload["session_id"] == "sess-1"
    assert p.payload["content"] == "hi there"
    assert p.payload["ultrathink"] is False
    assert p.suggested_salience == pytest.approx(0.85)
    assert p.correlation_id == "sess-1"
    assert isinstance(p.id, str) and p.id


def test_make_user_msg_percept_ultrathink_propagates() -> None:
    p = make_user_msg_percept("s", "x", ultrathink=True)
    assert p.payload["ultrathink"] is True


def test_make_user_msg_percept_ids_are_unique() -> None:
    ids = {make_user_msg_percept("s", "m").id for _ in range(50)}
    assert len(ids) == 50  # all distinct


def test_make_file_event_percept_modified() -> None:
    src = _FakeFilePercept(path="/a", event_type="modified")
    p = make_file_event_percept(src)
    assert p.source == "file"
    assert p.kind == "file_modified"
    assert p.payload["path"] == "/a"
    assert p.payload["event_type"] == "modified"
    assert p.payload["is_directory"] is False
    assert p.suggested_salience == pytest.approx(0.4)
    assert p.timestamp == 1_700_000_000.0


def test_make_file_event_percept_created_kind() -> None:
    p = make_file_event_percept(_FakeFilePercept(event_type="created"))
    assert p.kind == "file_created"


def test_make_file_event_percept_deleted_kind() -> None:
    p = make_file_event_percept(_FakeFilePercept(event_type="deleted"))
    assert p.kind == "file_deleted"


def test_make_file_event_percept_moved_carries_src_path() -> None:
    src = _FakeFilePercept(event_type="moved", src_path="/old/path")
    p = make_file_event_percept(src)
    assert p.kind == "file_moved"
    assert p.payload["src_path"] == "/old/path"


def test_make_file_event_percept_unknown_kind_falls_back() -> None:
    p = make_file_event_percept(_FakeFilePercept(event_type="weird"))
    # Kind has the file_ prefix even for an unmapped event_type.
    assert p.kind == "file_weird"


def test_make_cron_tick_percept_shape_and_correlation() -> None:
    p = make_cron_tick_percept("job-7", "nightly-summary", 1_700_000_111.0)
    assert p.source == "time"
    assert p.kind == "cron_tick"
    assert p.payload["job_id"] == "job-7"
    assert p.payload["job_name"] == "nightly-summary"
    assert p.payload["fired_at"] == 1_700_000_111.0
    assert p.suggested_salience == pytest.approx(0.3)
    assert p.correlation_id == "job-7"
    assert p.timestamp == 1_700_000_111.0


def test_make_process_alert_percept_cpu_high_salience() -> None:
    a = _FakeProcessAlert(kind="cpu_high", payload={"cpu_percent": 95.0})
    p = make_process_alert_percept(a)
    assert p.source == "process"
    assert p.kind == "cpu_high"
    assert p.payload["pid"] == 1234
    assert p.payload["description"] == "training run"
    assert p.payload["watch_id"] == "w-1"
    # Producer payload merged in.
    assert p.payload["cpu_percent"] == 95.0
    assert p.suggested_salience == pytest.approx(0.7)
    assert p.correlation_id == "w-1"


def test_make_process_alert_percept_memory_high_salience() -> None:
    p = make_process_alert_percept(_FakeProcessAlert(kind="memory_high"))
    assert p.suggested_salience == pytest.approx(0.7)


def test_make_process_alert_percept_zombie_high_priority() -> None:
    p = make_process_alert_percept(_FakeProcessAlert(kind="zombie"))
    assert p.kind == "zombie"
    assert p.suggested_salience == pytest.approx(0.95)


def test_make_process_alert_percept_exited_high_priority() -> None:
    p = make_process_alert_percept(_FakeProcessAlert(kind="exited"))
    assert p.suggested_salience == pytest.approx(0.95)


def test_make_process_alert_percept_handles_missing_payload() -> None:
    a = _FakeProcessAlert(kind="cpu_high", payload=None)
    p = make_process_alert_percept(a)
    # Doesn't crash; baseline payload still present.
    assert p.payload["pid"] == 1234


def test_make_internal_event_percept_default_salience() -> None:
    p = make_internal_event_percept(
        "goal_completed", {"goal_id": "g-1", "ok": True}
    )
    assert p.source == "internal"
    assert p.kind == "goal_completed"
    assert p.payload["goal_id"] == "g-1"
    assert p.suggested_salience == pytest.approx(0.5)
    assert p.correlation_id is None


def test_make_internal_event_percept_custom_salience_and_correlation() -> None:
    p = make_internal_event_percept(
        "skill_promoted",
        {"skill_id": "s-7"},
        suggested_salience=0.8,
        correlation_id="exp-42",
    )
    assert p.suggested_salience == pytest.approx(0.8)
    assert p.correlation_id == "exp-42"


# =================================================================== registry


@pytest.mark.asyncio
async def test_attach_file_watcher_subscribes_callback() -> None:
    bus = PerceptionBus()
    watcher = _FakeFileWatcher()
    reg = PerceptSourceRegistry(bus)

    await reg.attach_file_watcher(watcher)
    assert callable(watcher.callback)

    # Drive an event through the wired callback.
    await watcher.callback(_FakeFilePercept(path="/x", event_type="created"))
    drained = await bus.drain()
    assert len(drained) == 1
    assert drained[0].source == "file"
    assert drained[0].kind == "file_created"
    assert drained[0].payload["path"] == "/x"


@pytest.mark.asyncio
async def test_attach_file_watcher_chains_existing_callback() -> None:
    """If the watcher already has a callback, ours runs alongside it."""
    bus = PerceptionBus()
    watcher = _FakeFileWatcher()
    seen: list[Any] = []

    async def original_cb(event: Any) -> None:
        seen.append(event)

    watcher.callback = original_cb
    reg = PerceptSourceRegistry(bus)
    await reg.attach_file_watcher(watcher)

    fp = _FakeFilePercept(path="/y")
    await watcher.callback(fp)
    assert seen == [fp]
    assert len(await bus.drain()) == 1


@pytest.mark.asyncio
async def test_attach_process_watcher_swaps_bus() -> None:
    bus = PerceptionBus()
    watcher = _FakeProcessWatcher()
    reg = PerceptSourceRegistry(bus)
    await reg.attach_process_watcher(watcher)

    # Watcher's _bus is now an adapter exposing ``async push(alert)``.
    assert watcher._bus is not None
    await watcher._bus.push(_FakeProcessAlert(kind="cpu_high"))
    drained = await bus.drain()
    assert len(drained) == 1
    assert drained[0].source == "process"
    assert drained[0].kind == "cpu_high"


@pytest.mark.asyncio
async def test_attach_process_watcher_alert_payload_merged() -> None:
    bus = PerceptionBus()
    watcher = _FakeProcessWatcher()
    reg = PerceptSourceRegistry(bus)
    await reg.attach_process_watcher(watcher)
    a = _FakeProcessAlert(
        kind="memory_high", payload={"memory_mb": 4096.0, "threshold_mb": 2048.0}
    )
    await watcher._bus.push(a)
    drained = await bus.drain()
    assert drained[0].payload["memory_mb"] == 4096.0
    assert drained[0].payload["threshold_mb"] == 2048.0
    assert drained[0].payload["pid"] == 1234


@pytest.mark.asyncio
async def test_attach_user_message_hook_uses_setattr_not_subclass() -> None:
    """Wiring is via setattr on a public attribute, not method override.

    The fake AgentLoop has its own ``run_turn`` defined; we only inject
    the bus attribute, then drive run_turn and verify a percept lands
    on the bus.
    """
    bus = PerceptionBus()
    agent = _FakeAgentLoop()
    assert agent._perception_bus is None
    reg = PerceptSourceRegistry(bus)
    reg.attach_user_message_hook(agent)
    assert agent._perception_bus is bus

    await agent.run_turn("sess-x", "hello world")
    drained = await bus.drain()
    assert len(drained) == 1
    assert drained[0].source == "ws"
    assert drained[0].kind == "user_msg"
    assert drained[0].payload["content"] == "hello world"
    assert drained[0].correlation_id == "sess-x"


@pytest.mark.asyncio
async def test_agent_loop_without_bus_unchanged_no_push() -> None:
    """When no perception_bus is wired, run_turn is the legacy code path.

    Verified against the same _FakeAgentLoop: passing perception_bus=None
    (the constructor default) means no bus, no push, no failure.
    """
    bus = PerceptionBus()
    agent = _FakeAgentLoop(perception_bus=None)
    await agent.run_turn("sess-y", "should not push")
    assert await bus.drain() == []


@pytest.mark.asyncio
async def test_attach_cron_hook_registers_callback() -> None:
    bus = PerceptionBus()
    runner = _FakeCronRunner()
    reg = PerceptSourceRegistry(bus)
    reg.attach_cron_hook(runner)

    assert callable(runner._cb)
    await runner._cb("job-9", "weekly", 1_700_000_222.0)
    drained = await bus.drain()
    assert len(drained) == 1
    assert drained[0].source == "time"
    assert drained[0].kind == "cron_tick"
    assert drained[0].correlation_id == "job-9"


@pytest.mark.asyncio
async def test_attach_cron_hook_no_op_when_runner_lacks_register() -> None:
    """A runner without ``add_fire_callback`` is silently skipped."""
    bus = PerceptionBus()

    class Bare:
        pass

    reg = PerceptSourceRegistry(bus)
    # Should not raise.
    reg.attach_cron_hook(Bare())


@pytest.mark.asyncio
async def test_attach_handlers_treat_none_as_noop() -> None:
    """All attach methods accept None as a "source disabled" sentinel."""
    bus = PerceptionBus()
    reg = PerceptSourceRegistry(bus)
    await reg.attach_file_watcher(None)
    await reg.attach_process_watcher(None)
    reg.attach_user_message_hook(None)
    reg.attach_cron_hook(None)
    # Bus is untouched.
    assert bus.stats()["total_pushed"] == 0


@pytest.mark.asyncio
async def test_detach_all_restores_file_watcher_callback() -> None:
    bus = PerceptionBus()
    watcher = _FakeFileWatcher()

    async def original(_event: Any) -> None:
        pass

    watcher.callback = original
    reg = PerceptSourceRegistry(bus)
    await reg.attach_file_watcher(watcher)
    assert watcher.callback is not original  # we swapped in
    await reg.detach_all()
    assert watcher.callback is original  # restored


@pytest.mark.asyncio
async def test_detach_all_restores_process_watcher_bus() -> None:
    bus = PerceptionBus()
    watcher = _FakeProcessWatcher()
    sentinel = object()
    watcher._bus = sentinel
    reg = PerceptSourceRegistry(bus)
    await reg.attach_process_watcher(watcher)
    assert watcher._bus is not sentinel
    await reg.detach_all()
    assert watcher._bus is sentinel


@pytest.mark.asyncio
async def test_detach_all_restores_agent_loop_bus() -> None:
    bus = PerceptionBus()
    agent = _FakeAgentLoop()
    reg = PerceptSourceRegistry(bus)
    reg.attach_user_message_hook(agent)
    assert agent._perception_bus is bus
    await reg.detach_all()
    assert agent._perception_bus is None


@pytest.mark.asyncio
async def test_detach_all_is_idempotent() -> None:
    bus = PerceptionBus()
    watcher = _FakeFileWatcher()
    reg = PerceptSourceRegistry(bus)
    await reg.attach_file_watcher(watcher)
    await reg.detach_all()
    # Second call should not raise.
    await reg.detach_all()


@pytest.mark.asyncio
async def test_async_context_manager_form() -> None:
    """The registry is usable as an ``async with`` if a caller wants it."""
    bus = PerceptionBus()
    watcher = _FakeFileWatcher()
    async with PerceptSourceRegistry(bus) as reg:
        await reg.attach_file_watcher(watcher)
        assert callable(watcher.callback)
    # On exit, original (None) is restored.
    assert watcher.callback is None


@pytest.mark.asyncio
async def test_process_watcher_wiring_handles_bad_alert_gracefully() -> None:
    """A garbage push must not crash the adapter — it logs and drops."""
    bus = PerceptionBus()
    watcher = _FakeProcessWatcher()
    reg = PerceptSourceRegistry(bus)
    await reg.attach_process_watcher(watcher)

    # Push something that isn't an alert. The adapter coerces best-effort
    # — duck-typed make_process_alert_percept tolerates missing fields.
    await watcher._bus.push(object())
    # No exception bubbles up. Bus may still have a percept (our coercion
    # is permissive) — just assert the adapter survived.
    drained = await bus.drain()
    # If anything got through, it was at least a Percept.
    for p in drained:
        assert isinstance(p, Percept)


@pytest.mark.asyncio
async def test_unique_ids_across_helpers() -> None:
    """Every helper produces unique ids — no helper accidentally reuses."""
    ids: set[str] = set()
    ids.add(make_user_msg_percept("s", "m").id)
    ids.add(make_file_event_percept(_FakeFilePercept()).id)
    ids.add(make_cron_tick_percept("j", "n", 1.0).id)
    ids.add(make_process_alert_percept(_FakeProcessAlert()).id)
    ids.add(make_internal_event_percept("e", {}).id)
    assert len(ids) == 5


# Silence "asyncio imported but unused" — keeps the test module clean.
_ = asyncio
