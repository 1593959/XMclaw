"""Sprint 3 #3 — sleep-time agent + idle scheduler.

Covers:

- ``IdleDetector`` — each platform's detector returns sensible values
  with the OS calls mocked out, plus the always-idle fallback.
- ``parse_sleep_config`` — defaults, opt-in, per-field bad-value
  fallback (never raises).
- ``SleepWorker.tick_once`` — threshold-crossing fires the level's
  bus event exactly once per crossing; multiple registered tasks at
  the same level run in registration order; long-level tasks never
  fire before short-level at the same idle threshold.
- ``SleepWorker.start`` / ``stop`` lifecycle — idempotent; stop
  cancels in-flight task with rollback (SLEEP_INTERRUPTED publishes
  the partial-progress checkpoint).
- ``SleepWorkspace`` — buffered writes apply on success; discard on
  cancel; read-only workspaces silently swallow buffer_set so the
  same task code can run with either workspace.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import patch

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.daemon._idle_detector import (
    IdleDetector,
    _AlwaysIdleDetector,
    _LinuxIdleDetector,
    build_idle_detector,
)
from xmclaw.daemon.sleep_worker import (
    DEFAULT_IDLE_LONG_S,
    DEFAULT_IDLE_SHORT_S,
    DEFAULT_POLL_INTERVAL_S,
    SleepWorker,
    SleepWorkerConfig,
    SleepWorkspace,
    make_dream_cycle_task,
    make_memory_sweep_task,
    parse_sleep_config,
)


# ── helpers ───────────────────────────────────────────────────────────


class _FakeDetector(IdleDetector):
    """Idle detector with a programmable sequence of return values."""

    def __init__(self, *values: float) -> None:
        self._values = list(values) or [0.0]
        self._call_count = 0

    def idle_seconds(self) -> float:
        if self._call_count >= len(self._values):
            v = self._values[-1]
        else:
            v = self._values[self._call_count]
        self._call_count += 1
        return v

    @property
    def call_count(self) -> int:
        return self._call_count


async def _collect_events(bus: InProcessEventBus) -> list[BehavioralEvent]:
    """Subscribe a sink and return the list it accumulates."""
    captured: list[BehavioralEvent] = []

    async def _sink(event: BehavioralEvent) -> None:
        captured.append(event)

    bus.subscribe(lambda _e: True, _sink)
    return captured


async def _drain(bus: InProcessEventBus) -> None:
    """Wait one event-loop tick so subscriber tasks finish."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ── IdleDetector platform tests ───────────────────────────────────────


class TestIdleDetector:
    def test_always_idle_returns_sentinel(self) -> None:
        d = _AlwaysIdleDetector()
        assert d.idle_seconds() == _AlwaysIdleDetector.SENTINEL
        # Sentinel is comfortably past 30min so long_threshold trips.
        assert d.idle_seconds() > DEFAULT_IDLE_LONG_S
        assert d.reason  # non-empty default reason

    def test_always_idle_carries_reason(self) -> None:
        d = _AlwaysIdleDetector(reason="custom: pyobjc missing")
        assert "pyobjc" in d.reason
        assert d.name == "_AlwaysIdleDetector"

    def test_build_returns_a_detector(self) -> None:
        d = build_idle_detector()
        assert isinstance(d, IdleDetector)
        # Whatever we got, idle_seconds is callable + returns a float
        # (or negative sentinel — never None / never raises).
        v = d.idle_seconds()
        assert isinstance(v, float)

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="GetLastInputInfo only exists on Windows",
    )
    def test_windows_detector_constructs_natively(self) -> None:
        from xmclaw.daemon._idle_detector import _WindowsIdleDetector
        d = _WindowsIdleDetector()
        v = d.idle_seconds()
        # Native Windows always has a sane non-negative value.
        assert v >= 0.0

    def test_linux_detector_xprintidle_path(
        self, tmp_path,  # noqa: ARG002
    ) -> None:
        # Construct a detector with xprintidle pretend-resolved.
        with patch(
            "xmclaw.daemon._idle_detector.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/xprintidle" if name == "xprintidle" else None
            ),
        ):
            if sys.platform == "win32":
                # Constructor refuses on win32 by design.
                with pytest.raises(RuntimeError):
                    _LinuxIdleDetector()
                return
            d = _LinuxIdleDetector()
        # Mock the subprocess so we don't actually try to exec xprintidle.
        from unittest.mock import MagicMock
        run_ret = MagicMock(returncode=0, stdout="12345\n", stderr="")
        with patch(
            "xmclaw.daemon._idle_detector.subprocess.run",
            return_value=run_ret,
        ):
            v = d.idle_seconds()
        assert v == pytest.approx(12.345)

    def test_linux_detector_loginctl_idle_yes(self) -> None:
        if sys.platform == "win32":
            pytest.skip("loginctl path is non-Windows only")
        with patch(
            "xmclaw.daemon._idle_detector.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/loginctl" if name == "loginctl" else None
            ),
        ), patch.dict("os.environ", {"XDG_SESSION_ID": "c1"}):
            d = _LinuxIdleDetector(long_threshold_hint=999.0)
        from unittest.mock import MagicMock
        run_ret = MagicMock(returncode=0, stdout="IdleHint=yes", stderr="")
        with patch(
            "xmclaw.daemon._idle_detector.subprocess.run",
            return_value=run_ret,
        ):
            v = d.idle_seconds()
        assert v == 999.0  # the threshold hint we passed

    def test_linux_detector_no_source_raises(self) -> None:
        if sys.platform == "win32":
            pytest.skip("Linux-only path")
        with patch(
            "xmclaw.daemon._idle_detector.shutil.which", return_value=None,
        ), patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError):
                _LinuxIdleDetector()


# ── parse_sleep_config ────────────────────────────────────────────────


class TestParseSleepConfig:
    def test_none_returns_defaults(self) -> None:
        c = parse_sleep_config(None)
        assert c.idle_aware is True
        assert c.idle_short_s == DEFAULT_IDLE_SHORT_S
        assert c.idle_long_s == DEFAULT_IDLE_LONG_S
        assert c.poll_interval_s == DEFAULT_POLL_INTERVAL_S

    def test_non_dict_returns_defaults(self) -> None:
        # YAML fat-finger: a list landed where a dict should be.
        assert parse_sleep_config([1, 2, 3]) == SleepWorkerConfig()  # type: ignore[arg-type]
        assert parse_sleep_config("oops") == SleepWorkerConfig()  # type: ignore[arg-type]

    def test_opt_out_disables(self) -> None:
        c = parse_sleep_config({"idle_aware": False})
        assert c.idle_aware is False

    def test_custom_thresholds(self) -> None:
        c = parse_sleep_config(
            {"idle_short_s": 60, "idle_long_s": 600, "poll_interval_s": 5},
        )
        assert c.idle_short_s == 60.0
        assert c.idle_long_s == 600.0
        assert c.poll_interval_s == 5.0

    def test_bad_short_falls_back_to_default(self) -> None:
        c = parse_sleep_config({"idle_short_s": -10})
        assert c.idle_short_s == DEFAULT_IDLE_SHORT_S

    def test_long_below_short_swaps(self) -> None:
        c = parse_sleep_config({"idle_short_s": 600, "idle_long_s": 60})
        # After swap: long > short.
        assert c.idle_short_s <= c.idle_long_s


# ── SleepWorkspace ────────────────────────────────────────────────────


class TestSleepWorkspace:
    def test_read_only_swallows_buffer_set(self) -> None:
        ws = SleepWorkspace(writable=False)
        ws.buffer_set("x", 1)
        assert ws.buffer_view() == {}
        assert ws.writable is False

    def test_writable_buffers_until_apply(self) -> None:
        ws = SleepWorkspace(writable=True)
        ws.buffer_set("x", 1)
        ws.buffer_set("y", 2)
        assert ws.buffer_view() == {"x": 1, "y": 2}
        assert ws.applied is False

    def test_apply_runs_callbacks_in_order(self) -> None:
        ws = SleepWorkspace(writable=True)
        seen: list[dict[str, Any]] = []
        ws.register_apply(lambda b: seen.append({"first": dict(b)}))
        ws.register_apply(lambda b: seen.append({"second": dict(b)}))
        ws.buffer_set("k", "v")
        ws.apply()
        assert ws.applied is True
        assert seen == [{"first": {"k": "v"}}, {"second": {"k": "v"}}]

    def test_apply_is_idempotent(self) -> None:
        ws = SleepWorkspace(writable=True)
        calls = 0

        def cb(_b: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1

        ws.register_apply(cb)
        ws.buffer_set("x", 1)
        ws.apply()
        ws.apply()
        assert calls == 1

    def test_rollback_discards_buffer(self) -> None:
        ws = SleepWorkspace(writable=True)
        ws.buffer_set("x", 1)
        ws.rollback()
        assert ws.rolled_back is True
        # buffer_set after rollback is a no-op so the snapshot stays empty.
        ws.buffer_set("y", 2)
        assert ws.buffer_view() == {}

    def test_apply_after_rollback_is_no_op(self) -> None:
        ws = SleepWorkspace(writable=True)
        ws.buffer_set("x", 1)
        ws.rollback()
        called = False

        def cb(_b: dict[str, Any]) -> None:
            nonlocal called
            called = True

        ws.register_apply(cb)
        ws.apply()
        assert called is False

    def test_apply_callback_failure_does_not_block_others(self) -> None:
        ws = SleepWorkspace(writable=True)
        seen: list[str] = []
        ws.register_apply(lambda _b: (_ for _ in ()).throw(OSError("disk")))
        ws.register_apply(lambda _b: seen.append("ran"))
        ws.buffer_set("x", 1)
        ws.apply()
        # First callback raised, second still ran.
        assert seen == ["ran"]
        assert ws.applied is True

    def test_checkpoint_accumulates(self) -> None:
        ws = SleepWorkspace(writable=True)
        ws.checkpoint(progress=0.3)
        ws.checkpoint(progress=0.7, note="halfway")
        cp = ws.get_checkpoint()
        assert cp == {"progress": 0.7, "note": "halfway"}


# ── SleepWorker basics ────────────────────────────────────────────────


class TestSleepWorker:
    def test_constructor_validates_thresholds(self) -> None:
        bus = InProcessEventBus()
        with pytest.raises(ValueError):
            SleepWorker(_AlwaysIdleDetector(), bus, idle_short_s=-1)
        with pytest.raises(ValueError):
            SleepWorker(
                _AlwaysIdleDetector(), bus,
                idle_short_s=600, idle_long_s=300,
            )
        with pytest.raises(ValueError):
            SleepWorker(_AlwaysIdleDetector(), bus, poll_interval_s=0)

    def test_register_task_validates(self) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(_AlwaysIdleDetector(), bus)

        async def _ok(_ws: SleepWorkspace) -> dict[str, Any]:
            return {}

        with pytest.raises(ValueError):
            worker.register_task("", "short", _ok)
        with pytest.raises(ValueError):
            worker.register_task("x", "weekly", _ok)  # type: ignore[arg-type]
        worker.register_task("x", "short", _ok)
        # Same name across levels is rejected.
        with pytest.raises(ValueError):
            worker.register_task("x", "long", _ok)

    @pytest.mark.asyncio
    async def test_below_threshold_fires_nothing(self) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(
            _FakeDetector(10.0),  # well below 5min short
            bus, idle_short_s=300, idle_long_s=1800,
        )
        ran: list[str] = []

        async def t1(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("t1")
            return {}

        worker.register_task("t1", "short", t1)
        result = await worker.tick_once()
        assert result["fired"] == []
        assert ran == []

    @pytest.mark.asyncio
    async def test_short_threshold_crossing_fires_short_only(self) -> None:
        bus = InProcessEventBus()
        captured = await _collect_events(bus)
        worker = SleepWorker(
            _FakeDetector(400.0),  # > 5min, < 30min
            bus, idle_short_s=300, idle_long_s=1800,
        )
        ran: list[str] = []

        async def shortish(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("short")
            return {"ok": True}

        async def longish(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("long")
            return {"ok": True}

        worker.register_task("shortish", "short", shortish)
        worker.register_task("longish", "long", longish)

        result = await worker.tick_once()
        await _drain(bus)

        assert ran == ["short"]
        assert ("short", "shortish") in result["fired"]
        # Long never fired.
        types = [e.type for e in captured]
        assert EventType.SLEEP_IDLE_DETECTED in types
        assert EventType.SLEEP_TASK_STARTED in types
        assert EventType.SLEEP_TASK_FINISHED in types

    @pytest.mark.asyncio
    async def test_long_threshold_crossing_fires_short_then_long(
        self,
    ) -> None:
        bus = InProcessEventBus()
        # 2000s — way past long_threshold (1800s).
        worker = SleepWorker(
            _FakeDetector(2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        ran: list[str] = []

        async def shortish(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("short")
            return {}

        async def longish(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("long")
            return {}

        worker.register_task("shortish", "short", shortish)
        worker.register_task("longish", "long", longish)
        await worker.tick_once()
        # Short MUST run before long at the same crossing.
        assert ran == ["short", "long"]

    @pytest.mark.asyncio
    async def test_threshold_crosses_only_once_per_idle(self) -> None:
        bus = InProcessEventBus()
        # First tick: well past long. Second tick: still past long.
        # Third: dropped below short. Fourth: past long again.
        worker = SleepWorker(
            _FakeDetector(2000.0, 2000.0, 10.0, 2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        fire_counts: dict[str, int] = {"short": 0, "long": 0}

        async def shortish(_ws: SleepWorkspace) -> dict[str, Any]:
            fire_counts["short"] += 1
            return {}

        async def longish(_ws: SleepWorkspace) -> dict[str, Any]:
            fire_counts["long"] += 1
            return {}

        worker.register_task("shortish", "short", shortish)
        worker.register_task("longish", "long", longish)
        # tick 1: cross both, fire both.
        await worker.tick_once()
        # tick 2: still idle, but already fired since last dip → skip.
        await worker.tick_once()
        # tick 3: dipped below short → re-arm.
        await worker.tick_once()
        # tick 4: cross again → fire both.
        await worker.tick_once()
        assert fire_counts["short"] == 2
        assert fire_counts["long"] == 2

    @pytest.mark.asyncio
    async def test_multiple_tasks_at_same_level_run_in_registration_order(
        self,
    ) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(
            _FakeDetector(2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        order: list[str] = []

        async def t_a(_ws: SleepWorkspace) -> dict[str, Any]:
            order.append("a")
            return {}

        async def t_b(_ws: SleepWorkspace) -> dict[str, Any]:
            order.append("b")
            return {}

        async def t_c(_ws: SleepWorkspace) -> dict[str, Any]:
            order.append("c")
            return {}

        worker.register_task("a", "short", t_a)
        worker.register_task("b", "short", t_b)
        worker.register_task("c", "short", t_c)
        await worker.tick_once()
        assert order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_unmeasurable_negative_idle_treated_as_always_idle(
        self,
    ) -> None:
        bus = InProcessEventBus()
        # -1 is the unmeasurable sentinel — should fire just like the
        # fallback would.
        worker = SleepWorker(
            _FakeDetector(-1.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        ran: list[str] = []

        async def t1(_ws: SleepWorkspace) -> dict[str, Any]:
            ran.append("t1")
            return {}

        worker.register_task("t1", "short", t1)
        await worker.tick_once()
        assert ran == ["t1"]

    @pytest.mark.asyncio
    async def test_writable_workspace_apply_runs_on_success(self) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(
            _FakeDetector(2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        applied_with: list[dict[str, Any]] = []

        async def task(ws: SleepWorkspace) -> dict[str, Any]:
            ws.register_apply(lambda b: applied_with.append(dict(b)))
            ws.buffer_set("memo", "hello")
            return {"ok": True}

        worker.register_task("task", "short", task, writable=True)
        await worker.tick_once()
        assert applied_with == [{"memo": "hello"}]

    @pytest.mark.asyncio
    async def test_failing_task_rolls_back_workspace(self) -> None:
        bus = InProcessEventBus()
        captured = await _collect_events(bus)
        worker = SleepWorker(
            _FakeDetector(2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        applied_with: list[dict[str, Any]] = []

        async def task(ws: SleepWorkspace) -> dict[str, Any]:
            ws.register_apply(lambda b: applied_with.append(dict(b)))
            ws.buffer_set("memo", "should_not_apply")
            raise OSError("boom")

        worker.register_task("task", "short", task, writable=True)
        await worker.tick_once()
        await _drain(bus)
        # apply did NOT run.
        assert applied_with == []
        # SLEEP_TASK_FINISHED ok=False fired.
        finished = [
            e for e in captured if e.type == EventType.SLEEP_TASK_FINISHED
        ]
        assert finished
        assert finished[0].payload["ok"] is False
        assert "error" in finished[0].payload["result"]


# ── interrupt / cancel ────────────────────────────────────────────────


class TestInterrupt:
    @pytest.mark.asyncio
    async def test_stop_cancels_in_flight_with_rollback(self) -> None:
        bus = InProcessEventBus()
        captured = await _collect_events(bus)
        worker = SleepWorker(
            _FakeDetector(2000.0),
            bus, idle_short_s=300, idle_long_s=1800,
            poll_interval_s=0.05,
        )
        applied: list[dict[str, Any]] = []
        started = asyncio.Event()

        async def slow(ws: SleepWorkspace) -> dict[str, Any]:
            ws.register_apply(lambda b: applied.append(dict(b)))
            ws.buffer_set("phase", "in_progress")
            ws.checkpoint(phase="in_progress", percent=33)
            started.set()
            # Wait long enough for the test to call stop().
            await asyncio.sleep(2.0)
            ws.buffer_set("phase", "done")
            return {"ok": True}

        worker.register_task("slow", "short", slow, writable=True)
        await worker.start()
        # Wait for the task to be running.
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await worker.stop()
        await _drain(bus)

        # apply NEVER ran.
        assert applied == []
        # SLEEP_INTERRUPTED was published with the checkpoint.
        interrupts = [
            e for e in captured if e.type == EventType.SLEEP_INTERRUPTED
        ]
        assert interrupts
        assert interrupts[0].payload["task_name"] == "slow"
        assert interrupts[0].payload["partial_progress"]["percent"] == 33

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(
            _AlwaysIdleDetector(),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        await worker.start()
        await worker.stop()
        await worker.stop()  # second stop is a no-op

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        bus = InProcessEventBus()
        worker = SleepWorker(
            _AlwaysIdleDetector(),
            bus, idle_short_s=300, idle_long_s=1800,
            poll_interval_s=0.05,
        )
        await worker.start()
        await worker.start()  # second start is a no-op
        assert worker.is_running()
        await worker.stop()


# ── migration helpers ────────────────────────────────────────────────


class TestMigrationHelpers:
    @pytest.mark.asyncio
    async def test_make_dream_cycle_task_returns_proposal_count(self) -> None:
        class _FakeDream:
            def __init__(self) -> None:
                self.calls = 0

            async def run_once(self) -> int:
                self.calls += 1
                return 7

        fake = _FakeDream()
        fn = make_dream_cycle_task(fake)
        ws = SleepWorkspace(writable=False)
        result = await fn(ws)
        assert result == {"proposals": 7}
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_make_memory_sweep_task_translates_layers(self) -> None:
        class _FakeSweep:
            async def sweep_once(self) -> dict[str, int]:
                return {"short": 3, "working": 1, "long": 0}

        fn = make_memory_sweep_task(_FakeSweep())
        ws = SleepWorkspace(writable=False)
        result = await fn(ws)
        assert result == {
            "evicted_short": 3,
            "evicted_working": 1,
            "evicted_long": 0,
        }


# ── full end-to-end via SleepWorker ──────────────────────────────────


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_dream_cycle_registered_at_long_level(self) -> None:
        class _FakeDream:
            def __init__(self) -> None:
                self.runs = 0

            async def run_once(self) -> int:
                self.runs += 1
                return 0

        fake = _FakeDream()
        bus = InProcessEventBus()
        worker = SleepWorker(
            _FakeDetector(2000.0),  # > long
            bus, idle_short_s=300, idle_long_s=1800,
        )
        worker.register_task(
            "dream", "long", make_dream_cycle_task(fake),
        )
        await worker.tick_once()
        assert fake.runs == 1

    @pytest.mark.asyncio
    async def test_dream_cycle_does_not_fire_below_long_threshold(
        self,
    ) -> None:
        class _FakeDream:
            def __init__(self) -> None:
                self.runs = 0

            async def run_once(self) -> int:
                self.runs += 1
                return 0

        fake = _FakeDream()
        bus = InProcessEventBus()
        # 600s — past short, NOT past long.
        worker = SleepWorker(
            _FakeDetector(600.0),
            bus, idle_short_s=300, idle_long_s=1800,
        )
        worker.register_task(
            "dream", "long", make_dream_cycle_task(fake),
        )
        await worker.tick_once()
        assert fake.runs == 0  # below long threshold, no fire
