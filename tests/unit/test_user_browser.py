"""Tests for the ``browser_use_my_browser`` path.

Two layers:

1. ``_user_browser_detect`` — pure helpers (no Playwright). Covered
   here via path mocks + a real ``socket`` probe for find_free_port.

2. ``BrowserTools._ensure_user_browser_context`` + ``_use_my_browser``
   — uses the same in-test Playwright stub the existing
   ``test_v2_browser_tools.py`` already established. We don't need
   to re-stub all of Playwright; we shim the CDP probe + browser
   detection so the resolver lands in a predictable mode, and
   verify routing.

Hard invariants under test:

  - CDP attach mode never destroys the user's Browser handle on
    ``browser_close`` (would kill all their tabs).
  - Sessions opened via use_my_browser are properly pinned so
    subsequent browser_click / browser_fill route through the same
    context, not the headless/headed split.
"""
from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xmclaw.providers.tool import _user_browser_detect as ubd
from xmclaw.providers.tool.browser import BrowserTools


# ─── _user_browser_detect: pure helpers ────────────────────────────


def test_detect_browsers_returns_a_list():
    """Smoke: doesn't crash on the test runner's actual environment.
    Result may be empty (CI) or non-empty (dev machine with Chrome
    installed) — both are valid."""
    result = ubd.detect_browsers()
    assert isinstance(result, list)
    for inst in result:
        assert isinstance(inst, ubd.BrowserInstall)
        assert inst.exe_path.is_file()
        assert inst.name in ("chrome", "edge", "brave")
        assert inst.playwright_channel in ("chrome", "msedge", "chromium")


def test_pick_browser_auto_returns_first(monkeypatch, tmp_path):
    fake = ubd.BrowserInstall(
        name="chrome",
        exe_path=tmp_path / "chrome.exe",
        user_data_dir=tmp_path / "User Data",
        playwright_channel="chrome",
    )
    monkeypatch.setattr(ubd, "detect_browsers", lambda: [fake])
    assert ubd.pick_browser(None) is fake
    assert ubd.pick_browser("auto") is fake


def test_pick_browser_name_match(monkeypatch, tmp_path):
    chrome = ubd.BrowserInstall(
        "chrome", tmp_path / "c.exe", tmp_path / "c-data",
        "chrome",
    )
    edge = ubd.BrowserInstall(
        "edge", tmp_path / "e.exe", tmp_path / "e-data",
        "msedge",
    )
    monkeypatch.setattr(ubd, "detect_browsers", lambda: [chrome, edge])
    assert ubd.pick_browser("edge") is edge
    assert ubd.pick_browser("chrome") is chrome
    assert ubd.pick_browser("nonexistent") is None


def test_pick_browser_empty_returns_none(monkeypatch):
    monkeypatch.setattr(ubd, "detect_browsers", lambda: [])
    assert ubd.pick_browser() is None
    assert ubd.pick_browser("chrome") is None


def test_probe_cdp_endpoint_no_listener_returns_none():
    """Random unused high port → connection refused → None."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    # Port is now free; probe should return None.
    assert ubd.probe_cdp_endpoint(free_port, timeout=0.1) is None


def test_find_free_port_returns_in_range():
    port = ubd.find_free_port(50100, 50200)
    assert port is None or (50100 <= port <= 50200)


def test_is_user_data_dir_locked_missing_dir(tmp_path):
    assert ubd.is_user_data_dir_locked(tmp_path / "doesnt_exist") is False


def test_is_user_data_dir_locked_no_singleton_files(tmp_path):
    (tmp_path / "User Data").mkdir()
    assert ubd.is_user_data_dir_locked(tmp_path / "User Data") is False


def test_is_user_data_dir_locked_singleton_present(tmp_path):
    """Windows-style lockfile presence implies locked (conservative)."""
    udd = tmp_path / "User Data"
    udd.mkdir()
    (udd / "lockfile").write_text("")
    # On Windows the lockfile check fires; on Unix it doesn't and
    # we fall through to SingletonLock symlink check (absent) so
    # result is False. Test asserts the Windows path is wired.
    import sys
    if sys.platform == "win32":
        assert ubd.is_user_data_dir_locked(udd) is True
    else:
        # Unix: lockfile alone doesn't mean locked.
        assert ubd.is_user_data_dir_locked(udd) is False


# ─── _ensure_user_browser_context: mode resolution ─────────────────


@pytest.mark.asyncio
async def test_ensure_user_browser_cdp_attach_path(monkeypatch):
    """When :9222 is reachable, the resolver takes the CDP path."""
    bt = BrowserTools(headless=False)

    fake_ctx = MagicMock()
    fake_ctx.pages = []
    fake_browser = MagicMock()
    fake_browser.contexts = [fake_ctx]
    fake_browser.close = AsyncMock()

    fake_pw_chromium = MagicMock()
    fake_pw_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock(chromium=fake_pw_chromium)
    bt._playwright = fake_pw

    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.probe_cdp_endpoint",
        lambda port=9222, timeout=0.5: "http://127.0.0.1:9222",
    )

    ctx, mode = await bt._ensure_user_browser_context()

    assert mode == "cdp_attach"
    assert ctx is fake_ctx
    assert bt._user_browser_handle is fake_browser
    fake_pw_chromium.connect_over_cdp.assert_awaited_once_with(
        "http://127.0.0.1:9222",
    )


@pytest.mark.asyncio
async def test_ensure_user_browser_launches_real_profile_when_unlocked(
    monkeypatch, tmp_path,
):
    """No CDP listener + browser detected + profile not locked
    → tier 2: launch user's real profile via
    launch_persistent_context."""
    bt = BrowserTools(headless=False)
    fake_install = ubd.BrowserInstall(
        name="chrome",
        exe_path=tmp_path / "chrome.exe",
        user_data_dir=tmp_path / "user_data",
        playwright_channel="chrome",
    )

    fake_ctx = MagicMock()
    fake_ctx.pages = []
    fake_pw_chromium = MagicMock()
    fake_pw_chromium.launch_persistent_context = AsyncMock(
        return_value=fake_ctx,
    )
    bt._playwright = MagicMock(chromium=fake_pw_chromium)

    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.probe_cdp_endpoint",
        lambda port=9222, timeout=0.5: None,
    )
    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.pick_browser",
        lambda name=None: fake_install,
    )
    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.is_user_data_dir_locked",
        lambda _udd: False,
    )

    ctx, mode = await bt._ensure_user_browser_context(profile_dir="Default")

    assert mode == "launched_real_profile"
    assert ctx is fake_ctx
    call_kwargs = fake_pw_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["user_data_dir"] == str(fake_install.user_data_dir)
    assert call_kwargs["channel"] == "chrome"
    assert "--profile-directory=Default" in call_kwargs["args"]


@pytest.mark.asyncio
async def test_ensure_user_browser_raises_when_no_browser_found(monkeypatch):
    bt = BrowserTools(headless=False)
    bt._playwright = MagicMock()
    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.probe_cdp_endpoint",
        lambda port=9222, timeout=0.5: None,
    )
    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.pick_browser",
        lambda name=None: None,
    )
    monkeypatch.setattr(
        "xmclaw.providers.tool._user_browser_detect.detect_browsers",
        lambda: [],
    )
    with pytest.raises(RuntimeError, match="could not find"):
        await bt._ensure_user_browser_context()


@pytest.mark.asyncio
async def test_ensure_user_browser_caches_context_across_calls(monkeypatch):
    """Second call returns the cached context — we don't reconnect
    per browser_use_my_browser invocation."""
    bt = BrowserTools(headless=False)
    fake_ctx = MagicMock()
    fake_ctx.pages = []
    fake_ctx._xmclaw_user_browser_mode = "cdp_attach"
    bt._user_browser_context = fake_ctx

    ctx, mode = await bt._ensure_user_browser_context()
    assert ctx is fake_ctx
    assert mode == "cdp_attach"


# ─── close_session: never kill user's Chrome ────────────────────────


@pytest.mark.asyncio
async def test_close_session_does_not_close_user_cdp_context():
    """The hard invariant: user-CDP sessions detach, they DO NOT
    close the shared context. Closing it would kill the user's
    Chrome and lose all their tabs."""
    bt = BrowserTools(headless=False)
    bt._session_user_cdp["sid"] = True

    fake_page = MagicMock()
    fake_page.close = AsyncMock()
    bt._pages["sid"] = fake_page

    fake_ctx = MagicMock()
    fake_ctx.close = AsyncMock()
    bt._contexts["sid"] = fake_ctx

    await bt.close_session("sid")

    fake_page.close.assert_awaited_once()
    # CRITICAL: ctx.close must NOT have been called.
    fake_ctx.close.assert_not_called()
    # Session flag cleared so a re-open without user-CDP works.
    assert "sid" not in bt._session_user_cdp


def test_use_my_browser_spec_registered():
    """The tool must appear in list_tools() so the LLM can see it."""
    bt = BrowserTools(headless=False)
    names = {t.name for t in bt.list_tools()}
    assert "browser" in names
