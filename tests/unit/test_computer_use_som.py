"""Tests for SOM (Set-of-Mark) screenshot overlay in computer_use.

Covers:
- capture(mode="som") returns elements list with index/name/control_type/bbox
- click(element=N) resolves 1-indexed SOM elements
- Non-Windows graceful fallback to plain screenshot + empty elements
- Screenshot file existence and size
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from xmclaw.core.ir import ToolCall, ToolResult


@pytest.fixture
def make_call():
    _id = 0
    def _fn(name: str, args: dict | None = None) -> ToolCall:
        nonlocal _id
        _id += 1
        return ToolCall(id=f"tc-{_id:03d}", name=name, args=args or {}, provenance="synthetic")
    return _fn


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch):
    mod = types.ModuleType("pyautogui")
    mod.FAILSAFE = False
    mod.PAUSE = 0.0
    mod.calls: list[tuple[str, tuple, dict]] = []

    def _record(name):
        def _fn(*args, **kwargs):
            mod.calls.append((name, args, kwargs))
            if name == "position":
                return (123, 456)
            if name == "size":
                return (1920, 1080)
            return None
        return _fn

    for fname in ("moveTo", "click", "dragTo", "scroll", "write", "press", "hotkey"):
        setattr(mod, fname, _record(fname))
    mod.position = _record("position")
    mod.size = _record("size")
    monkeypatch.setitem(sys.modules, "pyautogui", mod)
    return mod


@pytest.fixture
def fake_mss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    mss_mod = types.ModuleType("mss")
    tools_mod = types.ModuleType("mss.tools")

    class _StubGrab:
        rgb = b"\x00" * (10 * 10 * 3)
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

    def _to_png(rgb, size, output=None):
        from PIL import Image
        import io
        img = Image.new("RGB", size, color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        if output:
            Path(output).write_bytes(data)
        return data

    tools_mod.to_png = _to_png
    mss_mod.tools = tools_mod
    monkeypatch.setitem(sys.modules, "mss", mss_mod)
    monkeypatch.setitem(sys.modules, "mss.tools", tools_mod)
    return mss_mod


@pytest.fixture
def tools(tmp_path: Path, fake_pyautogui, fake_mss):
    from xmclaw.providers.tool.computer_use import ComputerUseTools
    return ComputerUseTools(
        screenshot_dir=tmp_path / "shots",
        base64_size_cap=2 * 1024 * 1024,
    )


@pytest.mark.asyncio
async def test_capture_som_returns_elements(tools, make_call, monkeypatch, tmp_path: Path):
    async def fake_ui_inspect(*a, **k):
        elements = [
            {"name": "Btn", "control_type": "ButtonControl", "bbox": [10, 20, 30, 40], "depth": 1},
            {"name": "Edit", "control_type": "EditControl", "bbox": [50, 60, 70, 80], "depth": 2},
        ]
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": 2, "elements": elements}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som"})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    assert data["mode"] == "som"
    assert data["element_count"] == 2
    assert len(data["elements"]) == 2
    assert Path(data["path"]).exists()


@pytest.mark.asyncio
async def test_som_elements_have_required_fields(tools, make_call, monkeypatch):
    async def fake_ui_inspect(*a, **k):
        elements = [
            {"name": "Btn", "control_type": "ButtonControl", "bbox": [10, 20, 30, 40], "depth": 1},
        ]
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": 1, "elements": elements}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som"})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    el = data["elements"][0]
    assert "index" in el
    assert el["index"] == 1
    assert "name" in el
    assert "control_type" in el
    assert "bbox" in el
    assert len(el["bbox"]) == 4


@pytest.mark.asyncio
async def test_click_element_by_index(tools, make_call, fake_pyautogui):
    tools._last_som_elements = [
        {"index": 1, "name": "Button", "control_type": "ButtonControl", "bbox": [100, 200, 50, 30]},
        {"index": 2, "name": "Input", "control_type": "EditControl", "bbox": [200, 300, 80, 25]},
    ]
    call = make_call("computer_use", {"action": "click", "element": 1})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    # Center of first element bbox: (100 + 25, 200 + 15) = (125, 215)
    assert data["x"] == 125
    assert data["y"] == 215


@pytest.mark.asyncio
async def test_click_element_not_found(tools, make_call):
    tools._last_som_elements = [
        {"index": 1, "name": "Button", "control_type": "ButtonControl", "bbox": [100, 200, 50, 30]},
    ]
    call = make_call("computer_use", {"action": "click", "element": 999})
    result = await tools.invoke(call)
    assert not result.ok
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_click_element_no_som_yet(tools, make_call):
    tools._last_som_elements = []
    call = make_call("computer_use", {"action": "click", "element": 1})
    result = await tools.invoke(call)
    assert not result.ok
    assert "capture(mode='som')" in result.error


@pytest.mark.asyncio
async def test_som_non_windows_fallback(tools, make_call, monkeypatch, tmp_path: Path, fake_mss):
    async def fake_ui_inspect(*a, **k):
        return ToolResult(
            call_id="fake", ok=False, content=None,
            error="ui_inspect needs uiautomation (Windows only)",
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som"})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    assert data["mode"] == "som"
    assert data["element_count"] == 0
    assert data["elements"] == []
    assert Path(data["path"]).exists()


@pytest.mark.asyncio
async def test_som_screenshot_size(tools, make_call, monkeypatch, tmp_path: Path, fake_mss):
    async def fake_ui_inspect(*a, **k):
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": 0, "elements": []}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som"})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    path = data["path"]
    assert Path(path).exists()
    # The fake mss produces a 10x10 PNG header; size should be present
    assert "size" in data


@pytest.mark.asyncio
async def test_som_max_elements_cap(tools, make_call, monkeypatch):
    async def fake_ui_inspect(*a, **k):
        # _ui_inspect(self, call, t0, args) -> when monkeypatched on instance,
        # self is NOT passed, so args is the 3rd positional arg (index 2)
        inspect_args = a[2] if len(a) >= 3 else {}
        max_elements = inspect_args.get("max_elements", 100)
        elements = [
            {"name": f"El{i}", "control_type": "ButtonControl", "bbox": [i, i, 10, 10], "depth": 1}
            for i in range(max_elements + 10)
        ]
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": len(elements[:max_elements]), "elements": elements[:max_elements]}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som", "max_elements": 50})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    assert data["element_count"] <= 50


@pytest.mark.asyncio
async def test_som_max_elements_hard_cap(tools, make_call, monkeypatch):
    async def fake_ui_inspect(*a, **k):
        inspect_args = a[2] if len(a) >= 3 else {}
        max_elements = inspect_args.get("max_elements", 100)
        elements = [
            {"name": f"El{i}", "control_type": "ButtonControl", "bbox": [i, i, 10, 10], "depth": 1}
            for i in range(max_elements)
        ]
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": len(elements), "elements": elements}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    # Request 500, should be clamped to 200
    call = make_call("computer_use", {"action": "capture", "mode": "som", "max_elements": 500})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    assert data["element_count"] <= 200


@pytest.mark.asyncio
async def test_som_overlay_file_created(tools, make_call, monkeypatch, tmp_path: Path, fake_mss):
    async def fake_ui_inspect(*a, **k):
        elements = [
            {"name": "Btn", "control_type": "ButtonControl", "bbox": [10, 20, 30, 40], "depth": 1},
        ]
        return ToolResult(
            call_id="fake", ok=True,
            content=json.dumps({"window_title": "Test", "count": 1, "elements": elements}),
        )
    monkeypatch.setattr(tools, "_ui_inspect", fake_ui_inspect)

    call = make_call("computer_use", {"action": "capture", "mode": "som"})
    result = await tools.invoke(call)
    assert result.ok
    data = json.loads(result.content)
    overlay_path = data["path"]
    assert Path(overlay_path).exists()
    assert overlay_path.endswith(".som.png")
    # Verify original screenshot also exists
    assert Path(data["original_path"]).exists()
