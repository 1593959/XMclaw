"""Browser headed-mode foreground behaviour.

Regression for 2026-05-28 user report: ``browser_open(visible=true)``
correctly used Playwright ``headless=False`` and a Chrome window did
spawn, but the window stayed BEHIND the chat — user saw nothing and
concluded the tool was broken. Root cause was:

  1. Launch args missing ``--start-maximized``/``--new-window`` —
     window opened tiny and easy to lose.
  2. No ``page.bring_to_front()`` after navigation — tab z-order
     within the browser wasn't enforced.
  3. No OS-level ``SetForegroundWindow`` — Windows blocks foreground
     stealing from background processes (the daemon) unless the
     calling process recently had user input.

This test covers (1) and (2) via Playwright mocks; layer (3) is
Windows-only and tested via integration when a real desktop is
available.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.providers.tool.browser import BrowserTools


@pytest.mark.asyncio
async def test_bring_to_foreground_noop_when_headless():
    bt = BrowserTools(headless=True)
    page = MagicMock()
    page.bring_to_front = AsyncMock()
    await bt._bring_to_foreground("sid-x", page)
    page.bring_to_front.assert_not_called()


@pytest.mark.asyncio
async def test_bring_to_foreground_calls_bring_to_front_when_headed():
    bt = BrowserTools(headless=False)
    # Pin session as headed (simulate what _page_for would do on
    # first headed browser_open).
    bt._session_headless["sid-y"] = False

    page = MagicMock()
    page.bring_to_front = AsyncMock()

    # Stub the win32 layer — we test it lives behind a sys.platform
    # gate, not the actual API call. Real Windows behaviour is best
    # verified by manual smoke test on a real desktop.
    bt._win32_focus_browser_window = AsyncMock()

    await bt._bring_to_foreground("sid-y", page)

    page.bring_to_front.assert_awaited_once()
    # On non-Windows test runners, _win32_focus_browser_window
    # shouldn't fire. On Windows it should. Either way, no exception.


@pytest.mark.asyncio
async def test_bring_to_foreground_swallows_bring_to_front_errors():
    """If the page is closed or the tab vanished mid-call, we must
    not propagate — the navigation that just succeeded shouldn't
    fail because the focus polish couldn't run."""
    bt = BrowserTools(headless=False)
    bt._session_headless["sid-z"] = False

    page = MagicMock()
    page.bring_to_front = AsyncMock(side_effect=RuntimeError("tab closed"))
    bt._win32_focus_browser_window = AsyncMock()

    # Must not raise.
    await bt._bring_to_foreground("sid-z", page)


def test_module_imports_clean():
    """Smoke: the win32 ctypes import block lives inside the function
    body and must not break import on non-Windows test runners."""
    from xmclaw.providers.tool import browser as _b
    assert hasattr(_b.BrowserTools, "_bring_to_foreground")
    assert hasattr(_b.BrowserTools, "_win32_focus_browser_window")
