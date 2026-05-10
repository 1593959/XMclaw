"""Unit tests for Jarvis Phase 6.6: ProcessWatcher.

Covers dataclass shapes, watch lifecycle (watch / unwatch / list),
single-tick alert generation under mocked psutil, lazy-import behaviour
when psutil is absent, and start/stop lifecycle of the poll loop.

psutil is stubbed via ``sys.modules`` so the test suite does not depend
on the real package being installed (the production module imports it
lazily inside ``start()`` / ``_poll_once()``).
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

from xmclaw.cognition.process_watcher import (
    ProcessAlert,
    ProcessWatcher,
    ProcessWatchSpec,
)


# ------------------------------------------------------------------ helpers


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


def _make_fake_psutil(
    *,
    cpu_percent: float = 1.0,
    rss_bytes: int = 100 * 1024 * 1024,
    status: str = "running",
    raise_no_such: bool = False,
    raise_access_denied: bool = False,
) -> types.ModuleType:
    """Build a stand-in psutil module suitable for sys.modules patching."""
    module = types.ModuleType("psutil")

    module.NoSuchProcess = _FakeNoSuchProcess  # type: ignore[attr-defined]
    module.AccessDenied = _FakeAccessDenied  # type: ignore[attr-defined]
    module.STATUS_ZOMBIE = "zombie"  # type: ignore[attr-defined]

    def _process_factory(pid: int) -> MagicMock:
        if raise_no_such:
            raise _FakeNoSuchProcess(pid)
        if raise_access_denied:
            raise _FakeAccessDenied(pid)
        proc = MagicMock(name=f"FakeProcess(pid={pid})")
        proc.cpu_percent.return_value = cpu_percent
        mem = MagicMock()
        mem.rss = rss_bytes
        proc.memory_info.return_value = mem
        proc.status.return_value = status
        return proc

    module.Process = _process_factory  # type: ignore[attr-defined]
    return module


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    mod = _make_fake_psutil()
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return mod


def _patch_psutil(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> types.ModuleType:
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return mod


# ------------------------------------------------------------ dataclasses


def test_process_watch_spec_defaults() -> None:
    spec = ProcessWatchSpec(pid=1234, description="trainer")
    assert spec.pid == 1234
    assert spec.description == "trainer"
    assert spec.cpu_threshold == 90.0
    assert spec.memory_threshold_mb == 2048.0
    assert spec.alert_on_zombie is True
    assert spec.alert_on_exit is True


def test_process_watch_spec_is_frozen() -> None:
    spec = ProcessWatchSpec(pid=1, description="x")
    with pytest.raises((AttributeError, TypeError)):
        spec.pid = 2  # type: ignore[misc]


def test_process_alert_shape() -> None:
    alert = ProcessAlert(
        watch_id="w1",
        pid=42,
        description="hot model",
        kind="cpu_high",
        timestamp=123.0,
        payload={"cpu_percent": 95.0},
    )
    assert alert.kind == "cpu_high"
    assert alert.payload == {"cpu_percent": 95.0}


def test_process_alert_default_payload() -> None:
    alert = ProcessAlert(
        watch_id="w1",
        pid=1,
        description="d",
        kind="exited",
        timestamp=0.0,
    )
    assert alert.payload == {}


# ----------------------------------------------------- watch / unwatch


@pytest.mark.asyncio
async def test_watch_returns_unique_ids() -> None:
    w = ProcessWatcher()
    spec = ProcessWatchSpec(pid=1, description="a")
    id1 = await w.watch(spec)
    id2 = await w.watch(spec)
    assert id1 != id2
    assert isinstance(id1, str) and len(id1) > 0


@pytest.mark.asyncio
async def test_unwatch_unknown_returns_false() -> None:
    w = ProcessWatcher()
    assert await w.unwatch("does-not-exist") is False


@pytest.mark.asyncio
async def test_unwatch_known_returns_true() -> None:
    w = ProcessWatcher()
    wid = await w.watch(ProcessWatchSpec(pid=1, description="a"))
    assert await w.unwatch(wid) is True
    assert await w.unwatch(wid) is False  # second time gone


@pytest.mark.asyncio
async def test_list_watches_pairs() -> None:
    w = ProcessWatcher()
    s1 = ProcessWatchSpec(pid=1, description="a")
    s2 = ProcessWatchSpec(pid=2, description="b")
    id1 = await w.watch(s1)
    id2 = await w.watch(s2)
    pairs = await w.list_watches()
    assert dict(pairs) == {id1: s1, id2: s2}
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)


# --------------------------------------------------- _poll_once basics


@pytest.mark.asyncio
async def test_poll_once_with_no_watches(fake_psutil: types.ModuleType) -> None:
    w = ProcessWatcher()
    assert await w._poll_once() == []


@pytest.mark.asyncio
async def test_poll_once_cpu_high(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(cpu_percent=95.0))
    w = ProcessWatcher()
    await w.watch(ProcessWatchSpec(pid=1, description="trainer"))

    alerts = await w._poll_once()
    kinds = [a.kind for a in alerts]
    assert "cpu_high" in kinds
    cpu_alert = next(a for a in alerts if a.kind == "cpu_high")
    assert cpu_alert.pid == 1
    assert cpu_alert.description == "trainer"
    assert cpu_alert.payload["cpu_percent"] == pytest.approx(95.0)


@pytest.mark.asyncio
async def test_poll_once_memory_high(monkeypatch: pytest.MonkeyPatch) -> None:
    # 4 GiB > default 2048 MB threshold.
    _patch_psutil(
        monkeypatch,
        _make_fake_psutil(rss_bytes=4 * 1024 * 1024 * 1024),
    )
    w = ProcessWatcher()
    await w.watch(ProcessWatchSpec(pid=99, description="bloat"))

    alerts = await w._poll_once()
    mem_alerts = [a for a in alerts if a.kind == "memory_high"]
    assert len(mem_alerts) == 1
    assert mem_alerts[0].payload["memory_mb"] == pytest.approx(4096.0)
    assert mem_alerts[0].payload["threshold_mb"] == pytest.approx(2048.0)


@pytest.mark.asyncio
async def test_poll_once_zombie(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(status="zombie"))
    w = ProcessWatcher()
    await w.watch(ProcessWatchSpec(pid=7, description="zomb"))

    alerts = await w._poll_once()
    assert any(a.kind == "zombie" for a in alerts)


@pytest.mark.asyncio
async def test_poll_once_zombie_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(status="zombie"))
    w = ProcessWatcher()
    await w.watch(
        ProcessWatchSpec(pid=7, description="zomb", alert_on_zombie=False)
    )
    alerts = await w._poll_once()
    assert not any(a.kind == "zombie" for a in alerts)


@pytest.mark.asyncio
async def test_poll_once_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(raise_no_such=True))
    w = ProcessWatcher()
    await w.watch(ProcessWatchSpec(pid=999, description="dead"))

    alerts = await w._poll_once()
    assert len(alerts) == 1
    assert alerts[0].kind == "exited"
    assert alerts[0].pid == 999


@pytest.mark.asyncio
async def test_poll_once_exit_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(raise_no_such=True))
    w = ProcessWatcher()
    await w.watch(
        ProcessWatchSpec(pid=999, description="dead", alert_on_exit=False)
    )
    alerts = await w._poll_once()
    assert alerts == []


@pytest.mark.asyncio
async def test_poll_once_access_denied_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(raise_access_denied=True))
    w = ProcessWatcher()
    await w.watch(ProcessWatchSpec(pid=5, description="root"))

    # Should not raise, should not emit alerts for denied process.
    alerts = await w._poll_once()
    assert alerts == []


# --------------------------------------------------------- bus push


class _RecordingBus:
    def __init__(self) -> None:
        self.pushed: list[ProcessAlert] = []

    async def push(self, percept: ProcessAlert) -> None:
        self.pushed.append(percept)


@pytest.mark.asyncio
async def test_bus_push_called_per_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(cpu_percent=99.0))
    bus = _RecordingBus()
    w = ProcessWatcher(bus=bus)
    await w.watch(ProcessWatchSpec(pid=1, description="hot"))

    alerts = await w._poll_once()
    assert bus.pushed == alerts
    assert len(bus.pushed) >= 1


@pytest.mark.asyncio
async def test_bus_push_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil(monkeypatch, _make_fake_psutil(cpu_percent=99.0))

    class _BrokenBus:
        async def push(self, percept: ProcessAlert) -> None:
            raise RuntimeError("bus down")

    w = ProcessWatcher(bus=_BrokenBus())
    await w.watch(ProcessWatchSpec(pid=1, description="hot"))
    # Should not raise — bus failures are logged + swallowed.
    alerts = await w._poll_once()
    assert any(a.kind == "cpu_high" for a in alerts)


# ------------------------------------------------------- lazy import


def test_module_imports_without_psutil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure construction works even if psutil is absent.
    monkeypatch.setitem(sys.modules, "psutil", None)
    # Re-import to ensure no module-level psutil reference.
    import importlib

    import xmclaw.cognition.process_watcher as pw

    importlib.reload(pw)
    watcher = pw.ProcessWatcher()
    assert watcher is not None


@pytest.mark.asyncio
async def test_start_raises_helpful_error_without_psutil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the lazy import to fail.
    real_import = __import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "psutil":
            raise ImportError("No module named 'psutil'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.__import__", _fake_import)

    w = ProcessWatcher()
    with pytest.raises(ImportError, match="pip install psutil"):
        await w.start()


# ---------------------------------------------------- start / stop


@pytest.mark.asyncio
async def test_start_stop_lifecycle(
    fake_psutil: types.ModuleType,
) -> None:
    w = ProcessWatcher(poll_interval_s=0.01)
    await w.start()
    assert w._running is True
    assert w._task is not None
    # Let the loop tick at least once.
    await asyncio.sleep(0.03)
    await w.stop()
    assert w._running is False
    assert w._task is None


@pytest.mark.asyncio
async def test_start_is_idempotent(fake_psutil: types.ModuleType) -> None:
    w = ProcessWatcher(poll_interval_s=0.05)
    await w.start()
    first_task = w._task
    await w.start()  # second call must not spawn a new task
    assert w._task is first_task
    await w.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe() -> None:
    w = ProcessWatcher()
    await w.stop()  # should not raise
    assert w._task is None
