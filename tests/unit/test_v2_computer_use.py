"""Unit tests for ComputerUseTools.

Mocks ``pyautogui`` + ``mss`` so the tests run on CI / headless boxes
without an X server and without actually moving the user's cursor.
End-to-end live verification is left to manual ``xmclaw chat`` runs
on a real desktop.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmclaw.core.ir import ToolCall


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch):
    """Inject a mock pyautogui module into sys.modules.

    Records every call so tests can assert on it.
    """
    mod = types.ModuleType("pyautogui")
    mod.FAILSAFE = False
    mod.PAUSE = 0.0
    mod.calls: list[tuple[str, tuple, dict]] = []

    def _record(name):
        def _fn(*args, **kwargs):
            mod.calls.append((name, args, kwargs))
            # Return shape mirrors the real lib where the tools read back
            if name == "position":
                return (123, 456)
            if name == "size":
                return (1920, 1080)
            return None
        return _fn

    for fname in (
        "moveTo", "click", "dragTo", "scroll", "write", "press", "hotkey",
    ):
        setattr(mod, fname, _record(fname))
    mod.position = _record("position")
    mod.size = _record("size")
    monkeypatch.setitem(sys.modules, "pyautogui", mod)
    return mod


@pytest.fixture
def fake_mss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Inject a mock mss module. ``sct.grab`` returns a stub with
    rgb + size; ``mss.tools.to_png`` writes a tiny dummy PNG so
    the tool can read it back."""
    mss_mod = types.ModuleType("mss")
    tools_mod = types.ModuleType("mss.tools")

    class _StubGrab:
        rgb = b"\x00" * (10 * 10 * 3)  # 10x10 dummy
        size = (10, 10)

    class _StubSct:
        monitors = [
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def grab(self, mon): return _StubGrab()

    mss_mod.mss = lambda: _StubSct()
    PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"x" * 100  # ~108 bytes, well under cap

    def _to_png(rgb, size, output=None):
        if output:
            Path(output).write_bytes(PNG_HEADER)
        return PNG_HEADER

    tools_mod.to_png = _to_png
    mss_mod.tools = tools_mod
    monkeypatch.setitem(sys.modules, "mss", mss_mod)
    monkeypatch.setitem(sys.modules, "mss.tools", tools_mod)
    return mss_mod


@pytest.fixture
def fake_pygetwindow(monkeypatch: pytest.MonkeyPatch):
    """Mock pygetwindow with a stable 3-window scene."""
    gw_mod = types.ModuleType("pygetwindow")

    class _Win:
        def __init__(self, title, left, top, width, height,
                     minimized=False, active=False):
            self.title = title
            self.left = left
            self.top = top
            self.width = width
            self.height = height
            self.isMinimized = minimized
            self.isActive = active
            self.activated = False
            self.restored = False

        def activate(self):
            self.activated = True

        def restore(self):
            self.restored = True
            self.isMinimized = False

    windows = [
        _Win("Visual Studio Code - main.py", 100, 100, 1200, 800, active=True),
        _Win("Chrome — XMclaw", 200, 150, 1400, 900),
        _Win("Calculator", 50, 50, 300, 400, minimized=True),
        _Win("", 0, 0, 0, 0),  # empty title → filtered out
    ]
    gw_mod.getAllWindows = lambda: list(windows)
    gw_mod._windows = windows  # exposed for assertions
    monkeypatch.setitem(sys.modules, "pygetwindow", gw_mod)
    return gw_mod


@pytest.fixture
def tools(tmp_path: Path):
    from xmclaw.providers.tool.computer_use import ComputerUseTools
    return ComputerUseTools(
        screenshot_dir=tmp_path / "shots",
        base64_size_cap=2 * 1024 * 1024,  # generous for tests
    )


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    # Provenance is a Literal[...] string in xmclaw.core.ir.toolcall;
    # ``"synthetic"`` is the right tag for tool calls minted by tests /
    # internal code paths that didn't come off a real LLM wire format.
    return ToolCall(
        id=f"t-{name}",
        name=name,
        args=args or {},
        provenance="synthetic",
    )


def _json(result):
    return json.loads(result.content) if result.ok else None


# ── list_tools ────────────────────────────────────────────────────


def test_lists_all_eleven_tools(tools):
    names = {s.name for s in tools.list_tools()}
    assert names == {
        "screen_capture", "screen_size", "cursor_position",
        "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
        "keyboard_type", "keyboard_press",
        "window_list", "window_focus",
    }


# ── Vision ────────────────────────────────────────────────────────


async def test_screen_capture_writes_png_and_returns_b64(
    tools, fake_mss,
):
    r = await tools.invoke(_call("screen_capture", {"monitor": 1}))
    assert r.ok, r.error
    payload = _json(r)
    assert "path" in payload
    assert Path(payload["path"]).is_file()
    assert payload["size"] == [10, 10]
    assert "base64_png" in payload  # under cap


async def test_screen_capture_invalid_monitor(tools, fake_mss):
    r = await tools.invoke(_call("screen_capture", {"monitor": 99}))
    assert not r.ok
    assert "out of range" in r.error.lower()


async def test_screen_size_returns_dims(tools, fake_pyautogui):
    r = await tools.invoke(_call("screen_size"))
    assert r.ok
    assert _json(r) == {"width": 1920, "height": 1080}


async def test_cursor_position_returns_xy(tools, fake_pyautogui):
    r = await tools.invoke(_call("cursor_position"))
    assert r.ok
    assert _json(r) == {"x": 123, "y": 456}


# ── Mouse ─────────────────────────────────────────────────────────


async def test_mouse_move_invokes_pyautogui_with_clamped_duration(
    tools, fake_pyautogui,
):
    r = await tools.invoke(
        _call("mouse_move", {"x": 500, "y": 600, "duration": 99}),
    )
    assert r.ok
    # Duration clamped to _MAX_DURATION_S=30
    assert fake_pyautogui.calls[0] == ("moveTo", (500, 600), {"duration": 30.0})


async def test_mouse_move_missing_xy_fails(tools, fake_pyautogui):
    r = await tools.invoke(_call("mouse_move", {"x": 1}))  # y missing
    assert not r.ok
    assert "x, y" in r.error


async def test_mouse_click_at_coords(tools, fake_pyautogui):
    r = await tools.invoke(
        _call("mouse_click", {"x": 100, "y": 200, "button": "left", "count": 2}),
    )
    assert r.ok, r.error
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "click"
    assert kw == {"x": 100, "y": 200, "button": "left", "clicks": 2}


async def test_mouse_click_at_current_position(tools, fake_pyautogui):
    r = await tools.invoke(_call("mouse_click", {"button": "right"}))
    assert r.ok
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "click"
    # No x/y — uses current position
    assert kw == {"button": "right", "clicks": 1}
    # Response includes the current pos read-back
    payload = _json(r)
    assert payload["x"] == 123 and payload["y"] == 456


async def test_mouse_click_invalid_button(tools, fake_pyautogui):
    r = await tools.invoke(_call("mouse_click", {"button": "scroll"}))
    assert not r.ok
    assert "button must be" in r.error


async def test_mouse_drag(tools, fake_pyautogui):
    r = await tools.invoke(_call("mouse_drag", {
        "start_x": 10, "start_y": 20, "end_x": 110, "end_y": 220,
        "duration": 0.4,
    }))
    assert r.ok
    # First call: moveTo start; second: dragTo end
    assert fake_pyautogui.calls[0][0] == "moveTo"
    assert fake_pyautogui.calls[1][0] == "dragTo"


async def test_mouse_scroll(tools, fake_pyautogui):
    r = await tools.invoke(_call("mouse_scroll", {
        "clicks": -3, "x": 500, "y": 400,
    }))
    assert r.ok
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "scroll"
    assert args == (-3,)
    assert kw == {"x": 500, "y": 400}


# ── Keyboard ──────────────────────────────────────────────────────


async def test_keyboard_type_writes(tools, fake_pyautogui):
    r = await tools.invoke(
        _call("keyboard_type", {"text": "hello world", "interval": 0.02}),
    )
    assert r.ok
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "write"
    assert args == ("hello world",)
    assert kw == {"interval": 0.02}


async def test_keyboard_type_oversize_rejected(tools, fake_pyautogui):
    r = await tools.invoke(_call(
        "keyboard_type", {"text": "x" * 5000},  # > 4000 cap
    ))
    assert not r.ok
    assert "split into multiple" in r.error


async def test_keyboard_press_single_key(tools, fake_pyautogui):
    r = await tools.invoke(_call("keyboard_press", {"keys": "enter"}))
    assert r.ok
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "press"
    assert args == ("enter",)


async def test_keyboard_press_chord(tools, fake_pyautogui):
    r = await tools.invoke(_call("keyboard_press", {"keys": "ctrl+shift+t"}))
    assert r.ok
    name, args, kw = fake_pyautogui.calls[0]
    assert name == "hotkey"
    assert args == ("ctrl", "shift", "t")
    assert _json(r)["kind"] == "chord"


# ── Windows ───────────────────────────────────────────────────────


async def test_window_list_filters_by_substring(
    tools, fake_pygetwindow,
):
    r = await tools.invoke(_call("window_list", {"title_contains": "chrome"}))
    assert r.ok
    payload = _json(r)
    # case-insensitive, only "Chrome — XMclaw" matches
    assert payload["count"] == 1
    assert "Chrome" in payload["windows"][0]["title"]


async def test_window_list_skips_empty_titles(tools, fake_pygetwindow):
    r = await tools.invoke(_call("window_list", {}))
    assert r.ok
    payload = _json(r)
    # 4 windows in fixture but one has empty title → 3 returned
    assert payload["count"] == 3
    titles = [w["title"] for w in payload["windows"]]
    assert "" not in titles


async def test_window_focus_activates_match(tools, fake_pygetwindow):
    r = await tools.invoke(_call("window_focus", {"title_contains": "Code"}))
    assert r.ok
    payload = _json(r)
    assert "Visual Studio Code" in payload["title"]
    # The matched window had .activate() called
    matched = next(w for w in fake_pygetwindow._windows if "Code" in w.title)
    assert matched.activated is True


async def test_window_focus_prefers_non_minimized(
    tools, fake_pygetwindow,
):
    """When both a minimized and non-minimized window match, prefer
    the non-minimized one (matches user mental model: 'activate the
    visible Chrome')."""
    # Patch fixture: rename Calculator to also contain "lc" so two match
    fake_pygetwindow._windows[2].title = "Old Chrome (minimized)"
    r = await tools.invoke(_call("window_focus", {"title_contains": "chrome"}))
    assert r.ok
    # The non-minimized "Chrome — XMclaw" should win
    matched = next(
        w for w in fake_pygetwindow._windows if w.title == "Chrome — XMclaw"
    )
    assert matched.activated is True


async def test_window_focus_no_match(tools, fake_pygetwindow):
    r = await tools.invoke(
        _call("window_focus", {"title_contains": "does-not-exist"}),
    )
    assert not r.ok
    assert "no visible window" in r.error.lower()


# ── Missing-dep paths ─────────────────────────────────────────────


async def test_mouse_move_without_pyautogui(tools, monkeypatch):
    """No pyautogui in sys.modules + import will raise → tool returns
    a clear install hint, not crash."""
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    r = await tools.invoke(_call("mouse_move", {"x": 1, "y": 2}))
    assert not r.ok
    assert "pyautogui" in r.error.lower()


async def test_screen_capture_without_mss(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)
    r = await tools.invoke(_call("screen_capture"))
    assert not r.ok
    assert "mss" in r.error


async def test_window_list_without_pygetwindow(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "pygetwindow", None)
    r = await tools.invoke(_call("window_list"))
    assert not r.ok
    assert "pygetwindow" in r.error


# ── Unknown tool ──────────────────────────────────────────────────


async def test_unknown_tool_name(tools):
    r = await tools.invoke(_call("teleport"))
    assert not r.ok
    assert "unknown tool" in r.error


# ── Factory integration: opt-in gate ──────────────────────────────


def test_factory_does_not_wire_when_disabled(monkeypatch):
    """tools.computer_use.enabled=false (or absent) → ComputerUseTools
    must NOT appear in the agent's tool stack. Guards against silent
    enable that would hand the LLM a mouse it shouldn't have."""
    from xmclaw.daemon.factory import build_tools_from_config
    cfg = {
        "tools": {"allowed_dirs": [], "computer_use": {"enabled": False}},
        "llm": {"provider": "anthropic", "api_key": "test"},
    }
    provider = build_tools_from_config(cfg)
    names = {s.name for s in provider.list_tools()}
    assert "screen_capture" not in names
    assert "mouse_click" not in names


def test_factory_wires_when_enabled():
    from xmclaw.daemon.factory import build_tools_from_config
    cfg = {
        "tools": {
            "allowed_dirs": [],
            "computer_use": {"enabled": True},
        },
        "llm": {"provider": "anthropic", "api_key": "test"},
    }
    provider = build_tools_from_config(cfg)
    names = {s.name for s in provider.list_tools()}
    assert "screen_capture" in names
    assert "mouse_click" in names
    assert "keyboard_type" in names
    assert "window_list" in names
