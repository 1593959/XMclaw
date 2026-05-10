"""CognitiveDaemon ↔ ReflectionCycle integration — R1 (2026-05-10).

Pins that:
  * The daemon calls reflection_cycle.run_due exactly once per tick.
  * Cycle results show up in the tick summary as ``n_reflections``.
  * A failing cycle does NOT crash the tick (the daemon stays alive).
  * No reflection_cycle wired = ``n_reflections=0`` and the daemon
    keeps its legacy behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.cognition.cognitive_daemon import (
    CognitiveDaemon,
    CognitiveDaemonConfig,
)


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _NopBus:
    async def push(self, percept: Any) -> None: ...


@dataclass
class _NopAttention:
    async def tick(self) -> list:
        return []


@dataclass
class _RecordingCycle:
    """Stub ReflectionCycle that counts run_due calls + returns
    a controllable list of CycleResults."""
    return_results: list = field(default_factory=list)
    calls: int = 0
    last_tick: int = -1

    async def run_due(self, tick: int) -> list:
        self.calls += 1
        self.last_tick = tick
        return list(self.return_results)


@dataclass
class _BoomCycle:
    async def run_due(self, tick: int) -> list:
        raise RuntimeError("cycle imploded")


def _make_daemon(*, cycle: Any | None) -> CognitiveDaemon:
    return CognitiveDaemon(
        config=CognitiveDaemonConfig(
            enabled=True,
            heartbeat_hz=0.0,  # tick_once-only mode
        ),
        bus=_NopBus(),
        attention=_NopAttention(),
        reflection_cycle=cycle,
    )


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_calls_reflection_cycle_once_per_tick() -> None:
    cycle = _RecordingCycle(return_results=[])
    daemon = _make_daemon(cycle=cycle)
    await daemon.tick_once()
    await daemon.tick_once()
    await daemon.tick_once()
    assert cycle.calls == 3
    assert cycle.last_tick == 3


@pytest.mark.asyncio
async def test_tick_summary_carries_n_reflections_count() -> None:
    """3 cycles ran → tick summary reports n_reflections=3."""
    @dataclass
    class _Result:
        scope: str
    cycle = _RecordingCycle(return_results=[
        _Result(scope="recent"),
        _Result(scope="consolidate"),
        _Result(scope="groom"),
    ])
    daemon = _make_daemon(cycle=cycle)
    summary = await daemon.tick_once()
    assert summary["n_reflections"] == 3
    # Other counters still present (didn't accidentally break the
    # legacy schema).
    for k in (
        "tick", "n_percepts", "n_actionable",
        "n_goals_spawned", "n_plans_executed", "ran_experiment",
        "errors",
    ):
        assert k in summary


@pytest.mark.asyncio
async def test_failing_cycle_does_not_crash_tick() -> None:
    daemon = _make_daemon(cycle=_BoomCycle())
    summary = await daemon.tick_once()
    # Tick succeeds.
    assert summary["tick"] == 1
    # Error string captured for observability.
    assert any("reflection_cycle" in e for e in summary["errors"])
    # n_reflections defaults to 0 when cycle blew up.
    assert summary["n_reflections"] == 0


@pytest.mark.asyncio
async def test_no_cycle_wired_means_silent_n_reflections_zero() -> None:
    """Backward compat: daemon without a cycle still ticks fine."""
    daemon = _make_daemon(cycle=None)
    summary = await daemon.tick_once()
    assert summary["n_reflections"] == 0
    assert summary["errors"] == []
