"""Unit tests for P0.1 (ref system), P0.2 (dialog supervisor),
and P1.4 (annotated screenshots).

Strategy:
  - We don't need a real Playwright. The Page interface we touch
    here is small enough to stub. ``test_v2_browser_tools.py``
    already established the heavyweight fake; here we use lighter
    targeted mocks where they're sharper.
  - The PIL overlay function is tested with real Pillow if present;
    skipped otherwise (Pillow is an optional dep).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.browser import BrowserTools


def _call(name: str, args: dict, sid: str = "s1") -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic", session_id=sid,
    )


# ─── P0.1: ref system ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_click_ref_no_snapshot_returns_clear_error():
    bt = BrowserTools()
    r = await bt._click_ref(_call("browser_click_ref", {"ref": 1}), 0.0)
    assert r.ok is False
    assert "snapshot" in r.error.lower()


@pytest.mark.asyncio
async def test_click_ref_unknown_ref_returns_range():
    bt = BrowserTools()
    bt._session_refs["s1"] = {
        1: {"selector": "#login", "kind": "button"},
        2: {"selector": "#username", "kind": "input"},
    }
    r = await bt._click_ref(_call("browser_click_ref", {"ref": 99}), 0.0)
    assert r.ok is False
    assert "99" in r.error
    assert "1..2" in r.error


@pytest.mark.asyncio
async def test_click_ref_dispatches_to_click_with_resolved_selector(monkeypatch):
    bt = BrowserTools()
    bt._session_refs["s1"] = {
        5: {"selector": "#login-btn", "kind": "button"},
    }

    seen: dict[str, str] = {}

    async def _fake_click(forged_call, _t0):
        seen["selector"] = forged_call.args["selector"]
        seen["name"] = forged_call.name
        from xmclaw.core.ir import ToolResult
        return ToolResult(call_id=forged_call.id, ok=True, content="clicked")

    monkeypatch.setattr(bt, "_click", _fake_click)

    r = await bt._click_ref(_call("browser_click_ref", {"ref": 5}), 0.0)
    assert r.ok is True
    assert seen == {"selector": "#login-btn", "name": "browser_click"}


@pytest.mark.asyncio
async def test_type_ref_dispatches_to_fill_with_text(monkeypatch):
    bt = BrowserTools()
    bt._session_refs["s1"] = {
        3: {"selector": "input[name='q']", "kind": "input"},
    }

    captured: dict[str, str] = {}

    async def _fake_fill(forged_call, _t0):
        captured["selector"] = forged_call.args["selector"]
        captured["value"] = forged_call.args["value"]
        from xmclaw.core.ir import ToolResult
        return ToolResult(call_id=forged_call.id, ok=True, content="filled")

    monkeypatch.setattr(bt, "_fill", _fake_fill)

    r = await bt._type_ref(
        _call("browser_type_ref", {"ref": 3, "text": "hello"}), 0.0,
    )
    assert r.ok is True
    assert captured == {"selector": "input[name='q']", "value": "hello"}


@pytest.mark.asyncio
async def test_type_ref_with_submit_presses_enter(monkeypatch):
    bt = BrowserTools()
    bt._session_refs["s1"] = {
        1: {"selector": "input[name='q']", "kind": "input"},
    }

    async def _fake_fill(forged_call, _t0):
        from xmclaw.core.ir import ToolResult
        return ToolResult(call_id=forged_call.id, ok=True, content="filled")

    fake_page = MagicMock()
    fake_locator = MagicMock()
    fake_locator.press = AsyncMock()
    fake_page.locator = MagicMock(return_value=fake_locator)

    monkeypatch.setattr(bt, "_fill", _fake_fill)
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_resolve_locator", lambda p, s: fake_locator)
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())

    r = await bt._type_ref(
        _call("browser_type_ref", {"ref": 1, "text": "claude", "submit": True}),
        0.0,
    )
    assert r.ok is True
    fake_locator.press.assert_awaited_once_with("Enter")


# ─── P0.2: dialog supervisor ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dialog_no_pending_returns_error():
    bt = BrowserTools()
    r = await bt._dialog(
        _call("browser_dialog", {"action": "accept"}), 0.0,
    )
    assert r.ok is False
    assert "no pending dialog" in r.error.lower()


@pytest.mark.asyncio
async def test_dialog_bad_action():
    bt = BrowserTools()
    r = await bt._dialog(
        _call("browser_dialog", {"action": "shrug"}), 0.0,
    )
    assert r.ok is False
    assert "action must" in r.error


@pytest.mark.asyncio
async def test_dialog_accept_resolves_oldest_pending():
    bt = BrowserTools()
    fake_handle = MagicMock()
    fake_handle.accept = AsyncMock()
    bt._session_dialogs_pending["s1"] = [
        {"id": "d1", "type": "confirm", "message": "Delete?", "ts": 1.0},
    ]
    bt._dialog_handles[("s1", "d1")] = fake_handle

    r = await bt._dialog(
        _call("browser_dialog", {"action": "accept"}), 0.0,
    )
    assert r.ok is True
    fake_handle.accept.assert_awaited_once_with()
    # Moved out of pending into recent.
    assert bt._session_dialogs_pending["s1"] == []
    assert len(bt._session_dialogs_recent["s1"]) == 1
    assert bt._session_dialogs_recent["s1"][0]["resolved_action"] == "accept"


@pytest.mark.asyncio
async def test_dialog_respond_text_for_prompt():
    bt = BrowserTools()
    fake_handle = MagicMock()
    fake_handle.accept = AsyncMock()
    bt._session_dialogs_pending["s1"] = [
        {"id": "p1", "type": "prompt", "message": "Your name?", "ts": 1.0},
    ]
    bt._dialog_handles[("s1", "p1")] = fake_handle

    r = await bt._dialog(
        _call(
            "browser_dialog",
            {"action": "respond", "id": "p1", "text": "Alice"},
        ),
        0.0,
    )
    assert r.ok is True
    fake_handle.accept.assert_awaited_once_with("Alice")


@pytest.mark.asyncio
async def test_dialog_respond_rejected_for_non_prompt():
    bt = BrowserTools()
    fake_handle = MagicMock()
    bt._session_dialogs_pending["s1"] = [
        {"id": "c1", "type": "confirm", "message": "ok?", "ts": 1.0},
    ]
    bt._dialog_handles[("s1", "c1")] = fake_handle

    r = await bt._dialog(
        _call(
            "browser_dialog",
            {"action": "respond", "text": "x"},
        ),
        0.0,
    )
    assert r.ok is False
    assert "prompt" in r.error.lower()


def test_attach_dialog_listener_does_not_throw_without_event_loop():
    """Defensive: even when called from a sync test runner with no
    running event loop, the wrapper must swallow internally."""
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.on = MagicMock()
    # No event loop in this thread; just ensure attach doesn't blow up.
    bt._attach_dialog_listener("s1", fake_page)
    fake_page.on.assert_called_once()
    args, _ = fake_page.on.call_args
    assert args[0] == "dialog"


def test_dialog_event_appends_pending_and_caches_handle():
    """Simulate the dialog event firing and verify the listener
    populates pending + handle map."""
    bt = BrowserTools()
    fake_page = MagicMock()

    captured_handler = {}

    def _on(event_name, handler):
        captured_handler["fn"] = handler

    fake_page.on = _on
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        bt._attach_dialog_listener("s1", fake_page)

        fake_dialog = MagicMock()
        fake_dialog.type = "confirm"
        fake_dialog.message = "really delete?"
        fake_dialog.default_value = ""

        captured_handler["fn"](fake_dialog)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    pending = bt._session_dialogs_pending["s1"]
    assert len(pending) == 1
    rec = pending[0]
    assert rec["type"] == "confirm"
    assert rec["message"] == "really delete?"
    # Handle cached for browser_dialog to find.
    assert ("s1", rec["id"]) in bt._dialog_handles


# ─── close_session clears refs + dialogs ───────────────────────────


@pytest.mark.asyncio
async def test_close_session_clears_refs_and_dialogs():
    bt = BrowserTools()
    bt._session_refs["s1"] = {1: {"selector": "#x"}}
    bt._session_dialogs_pending["s1"] = [{"id": "a", "type": "alert"}]
    bt._session_dialogs_recent["s1"] = [{"id": "old"}]
    bt._dialog_handles[("s1", "a")] = MagicMock()

    await bt.close_session("s1")

    assert "s1" not in bt._session_refs
    assert "s1" not in bt._session_dialogs_pending
    assert "s1" not in bt._session_dialogs_recent
    assert ("s1", "a") not in bt._dialog_handles


# ─── P1.4: annotated screenshot overlay ────────────────────────────


def test_draw_ref_overlay_pil_missing_raises_pil_unavailable(monkeypatch):
    """If Pillow can't be imported, we raise the sentinel so the
    caller falls back to a plain screenshot."""
    import sys
    import builtins
    real_import = builtins.__import__

    def _no_pil(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("Pillow not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pil)
    from xmclaw.providers.tool.browser import _draw_ref_overlay, _PILUnavailable
    with pytest.raises(_PILUnavailable):
        _draw_ref_overlay(b"fake", {1: {"selector": "#x"}}, fmt="png")


def test_draw_ref_overlay_produces_valid_image():
    """End-to-end: real PIL, draw badges, decode the result."""
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    import io
    # Create a 200x100 white image as input.
    src = Image.new("RGB", (200, 100), (255, 255, 255))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    src_bytes = buf.getvalue()

    ref_map = {
        1: {
            "selector": "#a",
            "bbox": {"x": 10, "y": 10, "w": 50, "h": 20},
            "label": "btn A",
        },
        2: {
            "selector": "#b",
            "bbox": {"x": 80, "y": 30, "w": 40, "h": 20},
            "label": "btn B",
        },
    }
    from xmclaw.providers.tool.browser import _draw_ref_overlay
    out_bytes = _draw_ref_overlay(src_bytes, ref_map, fmt="png")

    # Result must be a valid PNG of same size.
    out_img = Image.open(io.BytesIO(out_bytes))
    assert out_img.size == (200, 100)
    # Output must differ from input (overlay drew something).
    assert out_bytes != src_bytes


def test_draw_ref_overlay_skips_entries_without_bbox():
    """An entry without bbox (shouldn't normally happen but be safe)
    is skipped without raising."""
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    import io
    src = Image.new("RGB", (100, 100), (255, 255, 255))
    buf = io.BytesIO()
    src.save(buf, format="PNG")

    from xmclaw.providers.tool.browser import _draw_ref_overlay
    out = _draw_ref_overlay(
        buf.getvalue(),
        {1: {"selector": "#x", "bbox": None}},  # no bbox
        fmt="png",
    )
    # No throw; output is a valid image.
    Image.open(io.BytesIO(out))


# ─── tool roster ───────────────────────────────────────────────────


def test_new_tools_registered():
    bt = BrowserTools()
    names = {t.name for t in bt.list_tools()}
    assert "browser" in names
