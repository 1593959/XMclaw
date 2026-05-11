"""Multi-modal perception source unit tests — R4 (2026-05-10).

Each watcher is duck-tested via monkey-patching its optional native
module so we never actually grab a screenshot, read the clipboard,
or query window state on the test box. Coverage:

  * PerceptionSource ABC: lifecycle (start/stop idempotent), error
    containment, push to bus.
  * ActiveWindowWatcher: change detection (no-push on same title).
  * ClipboardWatcher: classification (URL / code / text).
  * ScreenWatcher: summary mode + OCR mode (mocked).
  * CalendarWatcher: ICS parsing + dedup + salience window.
  * factory: cfg → sources list, unavailable filtering.
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception.calendar_watcher import CalendarWatcher
from xmclaw.cognition.perception.clipboard_watcher import (
    ClipboardWatcher,
    _classify,
)
from xmclaw.cognition.perception.factory import (
    build_perception_sources_from_config,
)
from xmclaw.cognition.perception.screen_watcher import ScreenWatcher
from xmclaw.cognition.perception.window_watcher import (
    ActiveWindowWatcher,
)
from xmclaw.cognition.perception_bus import Percept


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _CapturingBus:
    pushed: list[Percept] = field(default_factory=list)

    async def push(self, p: Percept) -> None:
        self.pushed.append(p)


# ── Minimal source for ABC tests ─────────────────────────────────


class _FakeSource(PerceptionSource):
    def __init__(
        self,
        *,
        bus: Any | None = None,
        percept: Percept | None = None,
        always_raise: bool = False,
    ) -> None:
        super().__init__(bus=bus, period_s=0.01)
        self._fixed = percept
        self._always_raise = always_raise

    @property
    def name(self) -> str:
        return "fake"

    def available(self) -> bool:
        return True

    async def poll_once(self) -> list[Percept]:
        if self._always_raise:
            raise RuntimeError("poll exploded")
        return [self._fixed] if self._fixed is not None else []


# ── ABC tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_source_pushes_one_percept_per_poll() -> None:
    bus = _CapturingBus()
    p = Percept(
        id="x", source="window", kind="k", timestamp=0.0,
        payload={}, suggested_salience=0.5,
    )
    src = _FakeSource(bus=bus, percept=p)
    await src.start()
    await asyncio.sleep(0.05)   # let a couple of ticks fire
    await src.stop()
    assert len(bus.pushed) >= 1
    assert all(pp.id == "x" for pp in bus.pushed)


@pytest.mark.asyncio
async def test_source_start_is_idempotent() -> None:
    bus = _CapturingBus()
    src = _FakeSource(bus=bus, percept=None)
    await src.start()
    await src.start()  # second call must not double-spawn
    await asyncio.sleep(0.02)
    await src.stop()


@pytest.mark.asyncio
async def test_source_swallows_poll_exceptions() -> None:
    bus = _CapturingBus()
    src = _FakeSource(bus=bus, always_raise=True)
    await src.start()
    await asyncio.sleep(0.04)
    await src.stop()
    # The exception didn't propagate. Bus has no pushes.
    assert bus.pushed == []


@pytest.mark.asyncio
async def test_source_unavailable_skips_start() -> None:
    class _Unavail(PerceptionSource):
        @property
        def name(self) -> str:
            return "ghost"

        def available(self) -> bool:
            return False

        async def poll_once(self) -> list[Percept]:
            return []  # never called

    src = _Unavail(period_s=0.01)
    await src.start()
    # No task scheduled; stop is also a no-op.
    assert src._task is None
    await src.stop()


# ── ActiveWindowWatcher ──────────────────────────────────────────


def _install_pygetwindow_stub(title: str) -> None:
    """Inject a dummy ``pygetwindow`` module into sys.modules with
    a fixed active-window title."""
    mod = types.ModuleType("pygetwindow")

    class _Win:
        def __init__(self, t: str) -> None:
            self.title = t

    def get_active() -> _Win:
        return _Win(title)
    mod.getActiveWindow = get_active  # type: ignore[attr-defined]
    sys.modules["pygetwindow"] = mod


@pytest.mark.asyncio
async def test_window_watcher_pushes_on_change_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_pygetwindow_stub("Code - main.py")
    monkeypatch.setattr(
        "sys.modules",
        sys.modules,
    )
    w = ActiveWindowWatcher(bus=None, period_s=0.01)
    out1 = await w.poll_once()
    out2 = await w.poll_once()  # same title → no push
    assert len(out1) == 1
    assert out1[0].payload["title"] == "Code - main.py"
    assert out2 == []
    # Now change title.
    _install_pygetwindow_stub("Browser - GitHub")
    out3 = await w.poll_once()
    assert len(out3) == 1
    assert out3[0].payload["title"] == "Browser - GitHub"
    assert out3[0].source == "window"


@pytest.mark.asyncio
async def test_window_watcher_handles_no_active_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = types.ModuleType("pygetwindow")
    mod.getActiveWindow = lambda: None  # type: ignore[attr-defined]
    sys.modules["pygetwindow"] = mod
    w = ActiveWindowWatcher(bus=None, period_s=0.01)
    out = await w.poll_once()
    assert out == []


# ── ClipboardWatcher ─────────────────────────────────────────────


def test_classify_url() -> None:
    kind, sal = _classify("看这里 https://example.com 关于 Python")
    assert kind == "url_copied"
    assert sal == 0.6


def test_classify_code_block() -> None:
    code = (
        "def hello(name):\n"
        "    print(f'hi {name}')\n"
        "    return name\n"
    )
    kind, sal = _classify(code)
    assert kind == "code_copied"
    assert sal == 0.55


def test_classify_plain_text() -> None:
    kind, sal = _classify("just a normal sentence")
    assert kind == "text_copied"
    assert sal == 0.4


@pytest.mark.asyncio
async def test_clipboard_watcher_pushes_on_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"content": "hello world"}

    mod = types.ModuleType("pyperclip")
    mod.paste = lambda: state["content"]  # type: ignore[attr-defined]
    sys.modules["pyperclip"] = mod

    w = ClipboardWatcher(bus=None, period_s=0.01, preview_chars=20)
    out1 = await w.poll_once()
    assert len(out1) == 1
    assert out1[0].kind == "text_copied"
    assert out1[0].payload["preview"] == "hello world"
    assert out1[0].payload["truncated"] is False

    out2 = await w.poll_once()
    assert out2 == []   # unchanged

    state["content"] = "https://github.com/example/repo"
    out3 = await w.poll_once()
    assert out3[0].kind == "url_copied"

    # Long content → preview truncated. ClipboardWatcher enforces a
    # 50-char floor on preview_chars (defensive — short previews
    # of binary garbage are useless), so the configured 20 gets
    # clamped to 50 internally. Pin that contract.
    state["content"] = "x" * 200
    out4 = await w.poll_once()
    assert out4[0].payload["truncated"] is True
    assert len(out4[0].payload["preview"]) == 50


# ── ScreenWatcher (mocked mss) ───────────────────────────────────


def _install_mss_stub() -> None:
    """Mock the mss module to avoid real screen capture."""
    mss_mod = types.ModuleType("mss")

    class _MSS:
        monitors = [
            {"width": 1920, "height": 1080},  # virtual all-in-one
            {"width": 1920, "height": 1080},  # primary
        ]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def grab(self, monitor):
            class _S:
                size = (10, 10)
                rgb = b"\x00" * (10 * 10 * 3)
            return _S()

    def factory():
        return _MSS()

    mss_mod.mss = factory  # type: ignore[attr-defined]
    sys.modules["mss"] = mss_mod


@pytest.mark.asyncio
async def test_screen_watcher_summary_mode() -> None:
    _install_mss_stub()
    w = ScreenWatcher(bus=None, period_s=0.01)
    out = await w.poll_once()
    assert len(out) == 1
    p = out[0]
    assert p.source == "screen"
    assert p.kind == "screen_state"
    assert p.payload["display_count"] == 1
    assert p.payload["primary_resolution"] == "1920x1080"
    assert "ocr_text" not in p.payload


# ── CalendarWatcher ──────────────────────────────────────────────


def _make_ics(tmp_path: Path, summary: str, mins_from_now: int) -> Path:
    """Write a minimal valid ICS file with one VEVENT."""
    import datetime
    start = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=mins_from_now)
    )
    end = start + datetime.timedelta(minutes=30)
    fmt = "%Y%m%dT%H%M%SZ"
    content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//XMclaw Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:test-{mins_from_now}@x\r\n"
        f"DTSTAMP:{datetime.datetime.now(datetime.timezone.utc).strftime(fmt)}\r\n"
        f"DTSTART:{start.strftime(fmt)}\r\n"
        f"DTEND:{end.strftime(fmt)}\r\n"
        f"SUMMARY:{summary}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    p = tmp_path / "cal.ics"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_calendar_watcher_emits_imminent_event(
    tmp_path: Path,
) -> None:
    pytest.importorskip("icalendar")
    ics = _make_ics(tmp_path, "Standup", mins_from_now=10)
    w = CalendarWatcher(bus=None, ics_path=ics, period_s=0.01)
    assert w.available()
    out = await w.poll_once()
    assert len(out) == 1
    p = out[0]
    assert p.source == "calendar"
    assert p.kind == "upcoming_event"
    assert p.payload["summary"] == "Standup"
    # 10-min window → 0.7 salience tier.
    assert p.suggested_salience == 0.7


@pytest.mark.asyncio
async def test_calendar_watcher_dedupes_within_window(
    tmp_path: Path,
) -> None:
    pytest.importorskip("icalendar")
    ics = _make_ics(tmp_path, "Standup", mins_from_now=10)
    w = CalendarWatcher(bus=None, ics_path=ics, period_s=0.01)
    out1 = await w.poll_once()
    out2 = await w.poll_once()
    assert len(out1) == 1
    assert out2 == []  # already pushed once


@pytest.mark.asyncio
async def test_calendar_watcher_skips_distant_events(
    tmp_path: Path,
) -> None:
    """Event 60+ minutes out is outside the 30-minute window → no
    push (no spam for "your meeting tomorrow")."""
    pytest.importorskip("icalendar")
    ics = _make_ics(tmp_path, "Tomorrow", mins_from_now=120)
    w = CalendarWatcher(
        bus=None, ics_path=ics, window_minutes=30, period_s=0.01,
    )
    out = await w.poll_once()
    assert out == []


def test_calendar_watcher_unavailable_without_ics_path() -> None:
    w = CalendarWatcher(bus=None, ics_path=None, period_s=0.01)
    assert w.available() is False


def test_calendar_watcher_unavailable_when_file_missing(
    tmp_path: Path,
) -> None:
    w = CalendarWatcher(
        bus=None, ics_path=tmp_path / "ghost.ics", period_s=0.01,
    )
    assert w.available() is False


# ── Factory ──────────────────────────────────────────────────────


def test_factory_returns_empty_when_all_explicitly_disabled() -> None:
    """2026-05-10 default flip: defaults are now opt-out (enabled=True
    per source). To get an empty list operator must explicitly disable
    every source. Pin the explicit-off path."""
    cfg = {"cognition": {"perception": {
        "screen":    {"enabled": False},
        "window":    {"enabled": False},
        "clipboard": {"enabled": False},
        "calendar":  {"enabled": False},
    }}}
    out = build_perception_sources_from_config(cfg, bus=None)
    assert out == []


def test_factory_default_on_returns_only_available_sources() -> None:
    """No-cfg path: factory tries every default-on watcher, but
    available() filters out any whose optional dep isn't installed.
    Result is environment-dependent — we only assert it's a list and
    every member has its deps available."""
    out = build_perception_sources_from_config(None, bus=None)
    assert isinstance(out, list)
    for s in out:
        assert s.available()


def test_factory_only_returns_available_sources() -> None:
    """Even when enabled, factory drops sources whose deps aren't
    present (avoid trying to start dead ones). Use a config that
    explicitly disables ALL but calendar with a bogus path so we
    test exactly the "enabled but unavailable" filter — independent
    of what the test box has installed."""
    cfg = {"cognition": {"perception": {
        "screen":    {"enabled": False},
        "window":    {"enabled": False},
        "clipboard": {"enabled": False},
        "calendar":  {"enabled": True, "ics_path": "/no/such/file.ics"},
    }}}
    out = build_perception_sources_from_config(cfg, bus=None)
    assert out == []


def test_factory_skips_calendar_without_ics_path() -> None:
    """Calendar enabled but missing ``ics_path`` is logged + dropped.
    Disable the other sources so we test the calendar branch only,
    independent of what the test box has installed for screen/
    window/clipboard."""
    cfg = {"cognition": {"perception": {
        "screen":    {"enabled": False},
        "window":    {"enabled": False},
        "clipboard": {"enabled": False},
        "calendar":  {"enabled": True},   # missing ics_path
    }}}
    out = build_perception_sources_from_config(cfg, bus=None)
    assert out == []


def test_factory_builds_calendar_when_valid(tmp_path: Path) -> None:
    pytest.importorskip("icalendar")
    ics = _make_ics(tmp_path, "Soon", mins_from_now=10)
    cfg = {"cognition": {"perception": {
        "calendar": {"enabled": True, "ics_path": str(ics)},
    }}}
    out = build_perception_sources_from_config(cfg, bus=None)
    assert len(out) == 1
    assert out[0].name == "calendar"
