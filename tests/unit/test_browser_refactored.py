"""Refactored browser tool tests: unified browser() + action param.

Covers:
  1. list_tools() returns exactly 1 ToolSpec named "browser".
  2. Unified browser(action="navigate", url=...) works.
  3. Unified browser(action="click", selector=...) works.
  4. Legacy browser_open maps to browser(action="navigate") with DeprecationWarning.
  5. capture_after returns a merged screenshot on mutating actions.
  6. Backward compatibility: all 30 legacy names still dispatch correctly.

Strategy: same as sibling tests — stub _page_for so we don't need Playwright.
"""
from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool.browser import BrowserTools, _LEGACY_TOOL_MAP


def _call(name: str, args: dict, sid: str = "s1") -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic", session_id=sid,
    )


# ─── 1. list_tools() returns exactly 1 spec ────────────────────────


def test_list_tools_returns_single_browser_spec():
    bt = BrowserTools()
    tools = bt.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "browser"
    assert "action" in tools[0].parameters_schema["properties"]
    assert tools[0].parameters_schema["properties"]["action"]["enum"]
    assert "navigate" in tools[0].parameters_schema["properties"]["action"]["enum"]
    assert "click" in tools[0].parameters_schema["properties"]["action"]["enum"]


# ─── 2. Unified browser(action="navigate", url=...) ─────────────────


@pytest.mark.asyncio
async def test_browser_navigate_ok(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_page.title = AsyncMock(return_value="Example")
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_page.goto = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())

    r = await bt.invoke(
        _call("browser", {"action": "navigate", "url": "https://example.com/"}),
    )
    assert r.ok is True
    assert r.content["url"] == "https://example.com/"
    assert r.content["title"] == "Example"


# ─── 3. Unified browser(action="click", selector=...) ─────────────


@pytest.mark.asyncio
async def test_browser_click_ok(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_page.title = AsyncMock(return_value="Example")
    fake_locator = MagicMock()
    fake_locator.count = AsyncMock(return_value=1)
    fake_locator.is_visible = AsyncMock(return_value=True)
    fake_locator.click = AsyncMock()
    fake_locator.nth = MagicMock(return_value=fake_locator)
    fake_locator.first = fake_locator  # _resolve_locator returns .first
    fake_page.locator = MagicMock(return_value=fake_locator)
    fake_page.wait_for_load_state = AsyncMock()
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())

    r = await bt.invoke(
        _call("browser", {"action": "click", "selector": "#btn"}),
    )
    assert r.ok is True
    assert r.content["selector"] == "#btn"


# ─── 4. Legacy browser_open → DeprecationWarning + action remap ────


@pytest.mark.asyncio
async def test_legacy_browser_open_emits_warning_and_works(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_page.title = AsyncMock(return_value="Example")
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_page.goto = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        r = await bt.invoke(
            _call("browser_open", {"url": "https://example.com/"}),
        )
    assert r.ok is True
    assert r.content["url"] == "https://example.com/"
    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert "browser_open" in str(w[0].message)
    assert "browser(action='navigate')" in str(w[0].message)


# ─── 5. capture_after merges screenshot into mutating result ───────


@pytest.mark.asyncio
async def test_capture_after_merges_screenshot(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_page.title = AsyncMock(return_value="Example")
    fake_locator = MagicMock()
    fake_locator.count = AsyncMock(return_value=1)
    fake_locator.is_visible = AsyncMock(return_value=True)
    fake_locator.click = AsyncMock()
    fake_locator.nth = MagicMock(return_value=fake_locator)
    fake_locator.first = fake_locator  # _resolve_locator returns .first
    fake_page.locator = MagicMock(return_value=fake_locator)
    fake_page.wait_for_load_state = AsyncMock()

    # Stub screenshot to return a fake metadata image
    async def _fake_screenshot(call, t0):
        return ToolResult(
            call_id=call.id, ok=True,
            content={"mime": "image/png", "path": "/tmp/fake.png"},
            side_effects=("/tmp/fake.png",),
            metadata={"attach_image": "/tmp/fake.png"},
            latency_ms=0.0,
        )

    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())
    monkeypatch.setattr(bt, "_screenshot", _fake_screenshot)

    r = await bt.invoke(
        _call("browser", {"action": "click", "selector": "#btn"}),
    )
    assert r.ok is True
    assert "screenshot" in r.content
    assert r.content["screenshot"]["path"] == "/tmp/fake.png"
    assert r.metadata.get("attach_image") == "/tmp/fake.png"


@pytest.mark.asyncio
async def test_capture_after_false_skips_screenshot(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_locator = MagicMock()
    fake_locator.count = AsyncMock(return_value=1)
    fake_locator.is_visible = AsyncMock(return_value=True)
    fake_locator.click = AsyncMock()
    fake_locator.nth = MagicMock(return_value=fake_locator)
    fake_locator.first = fake_locator  # _resolve_locator returns .first
    fake_page.locator = MagicMock(return_value=fake_locator)
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(bt, "_bring_to_foreground", AsyncMock())

    r = await bt.invoke(
        _call("browser", {"action": "click", "selector": "#btn", "capture_after": False}),
    )
    assert r.ok is True
    assert "screenshot" not in r.content


# ─── 6. Backward compatibility: all 30 legacy names dispatch ────────


@pytest.mark.asyncio
async def test_all_legacy_names_dispatch(monkeypatch):
    """Every entry in _LEGACY_TOOL_MAP produces a valid action parameter."""
    bt = BrowserTools()
    # We don't need real Playwright for this — just verify the action remap
    # reaches _browser() without raising "unknown tool".
    # For most actions we can patch the underlying handler to return a dummy OK.
    async def _ok_handler(*args, **kwargs):
        return ToolResult(
            call_id=args[0].id if args else "", ok=True, content="ok",
            latency_ms=0.0,
        )

    for action_name in _LEGACY_TOOL_MAP:
        action, _ = _LEGACY_TOOL_MAP[action_name]
        # Patch the concrete handler for this action.
        handler_name = {
            "navigate": "_open",
            "click": "_click",
            "press": "_press",
            "fill": "_fill",
            "hover": "_hover",
            "scroll": "_scroll",
            "select_option": "_select_option",
            "upload": "_upload",
            "wait_for": "_wait_for",
            "back": "_history_nav",
            "forward": "_history_nav",
            "reload": "_history_nav",
            "tabs": "_tabs_list",
            "tab_switch": "_tab_switch",
            "tab_close": "_tab_close",
            "screenshot": "_screenshot",
            "snapshot": "_snapshot",
            "eval": "_eval",
            "close": "_close",
            "click_ref": "_click_ref",
            "type_ref": "_type_ref",
            "dialog": "_dialog",
            "dialog_arm": "_dialog_arm",
            "network_log": "_network_log",
            "download_next": "_download_next",
            "save_state": "_save_state",
            "list_states": "_list_states",
            "import_cookies": "_import_cookies",
            "get_console": "_get_console",
            "use_my_browser": "_use_my_browser",
        }[action]

        monkeypatch.setattr(bt, handler_name, _ok_handler)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            r = await bt.invoke(
                _call(action_name, {"url": "https://x.com"} if action == "navigate" else
                      {"selector": "#x"} if action in ("click", "hover", "fill") else
                      {"action": "accept"} if action_name == "browser_dialog" else
                      {"action": "accept"} if action_name == "browser_dialog_arm" else
                      {}),
            )
        assert r.ok is True, f"legacy {action_name!r} failed: {r.error}"


# ─── Edge: unknown action inside browser() ─────────────────────────


@pytest.mark.asyncio
async def test_browser_unknown_action_returns_error():
    bt = BrowserTools()
    r = await bt.invoke(
        _call("browser", {"action": "fly_to_moon"}),
    )
    assert r.ok is False
    assert "unknown browser action" in r.error.lower()


# ─── Edge: unknown legacy tool name ──────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_error():
    bt = BrowserTools()
    r = await bt.invoke(
        _call("browser_fly_to_moon", {}),
    )
    assert r.ok is False
    assert "unknown tool" in r.error.lower()
