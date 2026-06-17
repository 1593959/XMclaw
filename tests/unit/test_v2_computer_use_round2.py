"""Unit tests for the round-2 computer-use tools (2026-05-12):

  * find_image_on_screen / click_on_image  — template-based icon find
  * scroll_to_text                          — scroll + wait for OCR hit
  * ui_inspect / ui_click                   — Windows UIAutomation

Mocks cv2 / OCR / uiautomation so tests run on any platform without
needing a real screen, real OpenCV install, or Windows.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        id=f"t-{name}", name=name, args=args or {},
        provenance="synthetic",
    )


def _json(result):
    return json.loads(result.content) if result.ok else None


@pytest.fixture
def tools(tmp_path):
    from xmclaw.providers.tool.computer_use import ComputerUseTools
    return ComputerUseTools(screenshot_dir=tmp_path / "shots")


def test_list_includes_round2_tools(tools):
    names = {s.name for s in tools.list_tools()}
    for n in (
        "find_image_on_screen", "click_on_image", "scroll_to_text",
        "ui_inspect", "ui_click",
    ):
        assert n in names, n


# ── find_image_on_screen ───────────────────────────────────────────


async def test_find_image_requires_existing_template(tools):
    r = await tools.invoke(_call("find_image_on_screen", {
        "template_path": "C:/nonexistent/icon.png",
    }))
    assert not r.ok
    assert "template not found" in r.error


async def test_find_image_no_template_path(tools):
    r = await tools.invoke(_call("find_image_on_screen", {}))
    assert not r.ok
    assert "template_path" in r.error


async def test_find_image_calls_cv2(tools, tmp_path, monkeypatch):
    """Mock cv2 + mss to verify the matchTemplate path runs end-to-end."""
    tpl = tmp_path / "icon.png"
    tpl.write_bytes(b"x")

    # Mock cv2
    cv2 = types.ModuleType("cv2")
    cv2.IMREAD_COLOR = 1
    cv2.TM_CCOEFF_NORMED = 5

    class _Tpl:
        shape = (32, 64, 3)  # h, w, channels

    def _imread(path, flag):
        return _Tpl()

    def _matchTemplate(screen, template, method):
        return [[0.95]]  # max_val will be 0.95

    def _minMaxLoc(res):
        return (0.0, 0.95, (0, 0), (500, 300))

    cv2.imread = _imread
    cv2.matchTemplate = _matchTemplate
    cv2.minMaxLoc = _minMaxLoc
    def _resize(img, size):
        class _R:
            shape = (size[1], size[0], img.shape[2])
        return _R()
    cv2.resize = _resize
    monkeypatch.setitem(sys.modules, "cv2", cv2)

    # Mock _grab_for_ocr (avoid touching real mss)
    import xmclaw.providers.tool.computer_use as cu

    class _ScreenStub:
        shape = (1080, 1920, 3)
    monkeypatch.setattr(
        cu, "_grab_for_ocr",
        lambda region: (_ScreenStub(), (0, 0)),
    )

    # numpy needed for type
    np_mod = types.ModuleType("numpy")
    monkeypatch.setitem(sys.modules, "numpy", np_mod)

    r = await tools.invoke(_call("find_image_on_screen", {
        "template_path": str(tpl),
        "confidence": 0.8,
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["found"] is True
    assert p["confidence"] == 0.95
    # Multi-scale: 0.75 scale wins because all mocked confidences are equal (0.95)
    # Template resized from (64, 32) to (48, 24)
    assert p["bbox"] == [500, 300, 48, 24]
    assert p["x"] == 524 and p["y"] == 312
    assert p["scale_used"] == 0.75


async def test_find_image_below_confidence(tools, tmp_path, monkeypatch):
    tpl = tmp_path / "icon.png"
    tpl.write_bytes(b"x")

    cv2 = types.ModuleType("cv2")
    cv2.IMREAD_COLOR = 1
    cv2.TM_CCOEFF_NORMED = 5

    class _Tpl:
        shape = (32, 64, 3)
    cv2.imread = lambda *a, **kw: _Tpl()
    cv2.matchTemplate = lambda *a, **kw: [[0.4]]
    cv2.minMaxLoc = lambda res: (0.0, 0.4, (0, 0), (10, 10))
    def _resize(img, size):
        class _R:
            shape = (size[1], size[0], img.shape[2])
        return _R()
    cv2.resize = _resize
    monkeypatch.setitem(sys.modules, "cv2", cv2)

    # numpy is also imported by _find_image_on_screen; stub it.
    np = types.ModuleType("numpy")
    monkeypatch.setitem(sys.modules, "numpy", np)

    import xmclaw.providers.tool.computer_use as cu
    monkeypatch.setattr(
        cu, "_grab_for_ocr",
        lambda region: (types.SimpleNamespace(shape=(100, 100, 3)), (0, 0)),
    )

    r = await tools.invoke(_call("find_image_on_screen", {
        "template_path": str(tpl),
        "confidence": 0.8,
    }))
    assert not r.ok
    err = json.loads(r.error)
    assert err["found"] is False
    assert err["best_confidence"] == 0.4


# ── scroll_to_text ────────────────────────────────────────────────


async def test_scroll_to_text_found_immediately(tools, monkeypatch):
    """Text visible on first OCR poll → no scrolls performed."""
    import xmclaw.providers.tool.computer_use as cu

    def _fake_ocr(region, min_conf):
        return [{
            "text": "目标群", "bbox": [50, 100, 60, 30],
            "center": [80, 115], "confidence": 0.95, "engine": "mock",
        }]
    monkeypatch.setattr(cu, "_run_ocr_full_pipeline", _fake_ocr)

    pg = types.ModuleType("pyautogui")
    pg.FAILSAFE = False
    pg.PAUSE = 0.0
    pg.scroll_calls = []
    pg.size = lambda: (1920, 1080)
    pg.position = lambda: (0, 0)

    def _scroll(*args, **kwargs):
        pg.scroll_calls.append((args, kwargs))

    pg.scroll = _scroll
    monkeypatch.setitem(sys.modules, "pyautogui", pg)

    r = await tools.invoke(_call("scroll_to_text", {
        "text": "目标", "max_scrolls": 5,
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["found"] is True
    assert p["scrolls_tried"] == 0
    # No scrolls performed
    assert len(pg.scroll_calls) == 0


async def test_scroll_to_text_polls_then_finds(tools, monkeypatch):
    """Text appears after 2 scrolls."""
    import xmclaw.providers.tool.computer_use as cu
    call_n = {"n": 0}

    def _fake_ocr(region, min_conf):
        call_n["n"] += 1
        if call_n["n"] < 3:
            return [{"text": "noise", "bbox": [0, 0, 1, 1], "center": [0, 0],
                     "confidence": 0.9, "engine": "x"}]
        return [{"text": "目标群", "bbox": [50, 200, 60, 30],
                 "center": [80, 215], "confidence": 0.95, "engine": "x"}]
    monkeypatch.setattr(cu, "_run_ocr_full_pipeline", _fake_ocr)

    pg = types.ModuleType("pyautogui")
    pg.FAILSAFE = False
    pg.PAUSE = 0.0
    pg.scroll_calls = []
    pg.size = lambda: (1920, 1080)
    pg.position = lambda: (0, 0)
    pg.scroll = lambda *a, **kw: pg.scroll_calls.append((a, kw))
    monkeypatch.setitem(sys.modules, "pyautogui", pg)

    r = await tools.invoke(_call("scroll_to_text", {
        "text": "目标",
        "max_scrolls": 10,
        "scroll_amount": 5,
    }))
    assert r.ok
    p = _json(r)
    assert p["scrolls_tried"] == 2
    # Each scroll went DOWN (negative clicks)
    assert all(call[0][0] < 0 for call in pg.scroll_calls)


async def test_scroll_to_text_gives_up(tools, monkeypatch):
    import xmclaw.providers.tool.computer_use as cu
    monkeypatch.setattr(
        cu, "_run_ocr_full_pipeline", lambda r, c: [],
    )
    pg = types.ModuleType("pyautogui")
    pg.FAILSAFE = False
    pg.PAUSE = 0.0
    pg.size = lambda: (1920, 1080)
    pg.position = lambda: (0, 0)
    pg.scroll = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "pyautogui", pg)

    r = await tools.invoke(_call("scroll_to_text", {
        "text": "never appears",
        "max_scrolls": 3,
    }))
    assert not r.ok
    err = json.loads(r.error)
    assert err["found"] is False
    assert err["scrolls_tried"] == 3


async def test_scroll_to_text_invalid_direction(tools):
    r = await tools.invoke(_call("scroll_to_text", {
        "text": "x",
        "direction": "sideways",
    }))
    assert not r.ok
    assert "direction" in r.error


# ── ui_inspect / ui_click  ─────────────────────────────────────────


@pytest.fixture
def fake_uiautomation(monkeypatch):
    """Stand-in for the uiautomation package — provides just enough
    shape that our tools work without Windows."""
    uia = types.ModuleType("uiautomation")

    class _BBox:
        def __init__(self, left, top, right, bottom):
            self.left = left
            self.top = top
            self.right = right
            self.bottom = bottom

    class _Ctrl:
        def __init__(self, name, ctype="ButtonControl", auto_id="",
                     children=None, bbox=(0, 0, 100, 30)):
            self.Name = name
            self.ControlTypeName = ctype
            self.AutomationId = auto_id
            self._children = children or []
            self.BoundingRectangle = _BBox(*self._mk_bbox(bbox))
            self.invoked = False
            self.clicked = False
            self.double_clicked = False
            self.focused = False

        @staticmethod
        def _mk_bbox(b):
            x, y, w, h = b
            return (x, y, x + w, y + h)

        def GetChildren(self):
            return self._children

        def Exists(self, maxSearchSeconds=1):
            return True

        def SetFocus(self):
            self.focused = True

        def GetInvokePattern(self):
            class _IP:
                def __init__(s, parent): s.parent = parent
                def Invoke(s): s.parent.invoked = True
            return _IP(self)

        def Click(self):
            self.clicked = True

        def DoubleClick(self):
            self.double_clicked = True

    # Build a fake foreground "window" with 3 children
    btn1 = _Ctrl("发送", "ButtonControl", "send_btn", bbox=(100, 200, 80, 30))
    btn2 = _Ctrl("Cancel", "ButtonControl", "cancel_btn", bbox=(200, 200, 80, 30))
    edit = _Ctrl("Search input", "EditControl", "search", bbox=(50, 50, 400, 30))
    fg = _Ctrl(
        "Test Window", "WindowControl", "",
        children=[btn1, btn2, edit],
        bbox=(0, 0, 1280, 720),
    )

    class _Initializer:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    uia.UIAutomationInitializerInThread = lambda: _Initializer()
    uia.GetForegroundControl = lambda: fg
    uia.WindowControl = lambda **kw: fg  # window_title path also returns fg

    monkeypatch.setitem(sys.modules, "uiautomation", uia)
    uia._fg = fg  # exposed for assertions
    return uia


async def test_ui_inspect_dumps_tree(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_inspect", {"max_depth": 6}))
    assert r.ok, r.error
    p = _json(r)
    assert p["count"] >= 3  # 3 children + maybe root
    names = {e["name"] for e in p["elements"]}
    assert "发送" in names
    assert "Cancel" in names


async def test_ui_inspect_filters_by_control_type(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_inspect", {"control_type": "Button"}))
    assert r.ok
    p = _json(r)
    # Only the 2 Button children + maybe root window
    assert all(
        "Button" in e["control_type"] or "Window" in e["control_type"]
        for e in p["elements"]
    )


async def test_ui_inspect_filters_by_name(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_inspect", {"name_contains": "send"}))
    assert r.ok
    p = _json(r)
    # Either matches name "发送" (Chinese) or automation_id "send_btn"
    # — name_contains is on Name field, so 0 hits expected here.
    # Adjust: use name_contains="cancel"
    r2 = await tools.invoke(_call("ui_inspect", {"name_contains": "cancel"}))
    p2 = _json(r2)
    assert any("Cancel" in e["name"] for e in p2["elements"])


async def test_ui_click_invoke_pattern(tools, fake_uiautomation):
    """ui_click should prefer InvokePattern over physical click."""
    r = await tools.invoke(_call("ui_click", {
        "name_contains": "发送",
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["clicked"] is True
    assert p["name"] == "发送"
    assert p["via"] == "invoke_pattern"
    # The actual ctrl's invoked flag flipped
    btn = next(
        c for c in fake_uiautomation._fg._children
        if c.Name == "发送"
    )
    assert btn.invoked is True


async def test_ui_click_no_match(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_click", {"name_contains": "Does not exist"}))
    assert not r.ok
    assert "no UI element" in r.error


async def test_ui_click_needs_a_filter(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_click", {}))
    assert not r.ok
    assert "name_contains or automation_id" in r.error


async def test_ui_click_by_automation_id(tools, fake_uiautomation):
    r = await tools.invoke(_call("ui_click", {"automation_id": "search"}))
    assert r.ok
    p = _json(r)
    assert p["automation_id"] == "search"


async def test_ui_inspect_no_uiautomation_pkg(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "uiautomation", None)
    r = await tools.invoke(_call("ui_inspect"))
    assert not r.ok
    assert "uiautomation" in r.error
