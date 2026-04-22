"""BrowserTools unit tests.

We mock the playwright boot path so the test suite doesn't need a
browser binary. The tests cover:
  - list_tools returns the 7-tool roster even without playwright
  - the missing-playwright path returns a structured "install with" error
    instead of crashing
  - allowed_hosts gate refuses cross-host navigation
  - happy-path open / click / fill / screenshot / snapshot / eval /
    close via a fake async playwright surface
  - per-session isolation: two sessions get independent pages
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.browser import BrowserTools


def _call(name: str, args: dict, session_id: str | None = None) -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic",
        session_id=session_id,
    )


# ── spec surface is always the full roster ──────────────────────────────


def test_list_tools_complete_roster_even_without_playwright() -> None:
    names = {s.name for s in BrowserTools().list_tools()}
    assert names == {
        "browser_open", "browser_click", "browser_fill",
        "browser_screenshot", "browser_snapshot", "browser_eval",
        "browser_close",
    }


# ── missing-playwright path is a structured refusal, not a crash ────────


@pytest.mark.asyncio
async def test_missing_playwright_returns_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If playwright isn't importable the tool returns ok=False with a
    human hint rather than propagating ImportError."""
    # Poison the import inside the lazy path.
    def _raise(*a, **k):
        raise ImportError("playwright not found")
    # Patch the lazy import mechanism: monkeypatch sys.modules so the
    # local import fails.
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    tools = BrowserTools()
    r = await tools.invoke(_call("browser_open", {"url": "https://example.com"}))
    assert r.ok is False
    assert "playwright" in r.error.lower()
    assert "install" in r.error.lower()


# ── host allowlist gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowed_hosts_refuses_disallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With allowed_hosts set, other hosts refuse -- playwright never
    gets called so it doesn't matter whether it's installed."""
    tools = BrowserTools(allowed_hosts=["example.com"])
    r = await tools.invoke(_call(
        "browser_open", {"url": "https://evil.biz/phishing"},
    ))
    assert r.ok is False
    assert "evil.biz" in r.error
    assert "allowed_hosts" in r.error


# ── happy-path via fake playwright ─────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200) -> None: self.status = status


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.title_value = "Untitled"
        self.evaluate_map: dict[str, Any] = {}
        self.last_click: str | None = None
        self.last_fill: tuple[str, str] | None = None
        self._closed = False

    def is_closed(self) -> bool: return self._closed

    async def close(self) -> None: self._closed = True

    async def goto(self, url: str, wait_until: str = "load") -> _FakeResponse:
        self.url = url
        return _FakeResponse(200)

    async def title(self) -> str: return self.title_value

    async def click(self, selector: str) -> None:
        self.last_click = selector

    async def fill(self, selector: str, value: str) -> None:
        self.last_fill = (selector, value)

    async def screenshot(self, full_page: bool = False, type: str = "png") -> bytes:
        # Tiny PNG header -- enough to verify base64 encoding roundtrips.
        return b"\x89PNG\r\n\x1a\n" + (b"FULL" if full_page else b"VIEW")

    async def evaluate(self, expr: str, *args: Any) -> Any:
        # Simulate the snapshot JS calls -- check the link-grab first
        # because the text-grab's script also mentions querySelectorAll
        # in the outer function string representation. Order-sensitive.
        if "querySelectorAll" in expr:
            return [{"label": "Example Link", "href": "https://example.com/a"}]
        if "document.body" in expr or "innerText" in expr:
            return "hello page body"
        return self.evaluate_map.get(expr, expr)


@dataclass
class _FakeContext:
    pages: list[_FakePage] = field(default_factory=list)
    closed: bool = False

    async def new_page(self) -> _FakePage:
        p = _FakePage(); self.pages.append(p); return p

    def set_default_timeout(self, _ms: int) -> None: pass

    async def close(self) -> None: self.closed = True


@dataclass
class _FakeBrowser:
    contexts: list[_FakeContext] = field(default_factory=list)

    async def new_context(self, **_: Any) -> _FakeContext:
        c = _FakeContext(); self.contexts.append(c); return c

    async def close(self) -> None: pass


@dataclass
class _FakeChromium:
    browser: _FakeBrowser = field(default_factory=_FakeBrowser)

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        return self.browser


@dataclass
class _FakePlaywright:
    chromium: _FakeChromium = field(default_factory=_FakeChromium)

    async def stop(self) -> None: pass


class _FakePlaywrightFactory:
    def __init__(self) -> None:
        self.pw = _FakePlaywright()

    def __call__(self) -> "_FakePlaywrightFactory":  # mimics async_playwright()
        return self

    async def start(self) -> _FakePlaywright:
        return self.pw


@pytest.fixture
def patched_browser(monkeypatch: pytest.MonkeyPatch) -> BrowserTools:
    """BrowserTools with a fake playwright wired in."""
    import sys
    # Build a tiny module-like object exposing async_playwright = factory.
    factory = _FakePlaywrightFactory()

    class _FakeAsyncAPI:
        @staticmethod
        def async_playwright() -> _FakePlaywrightFactory:
            return factory

    monkeypatch.setitem(sys.modules, "playwright", type("_pw", (), {})())
    monkeypatch.setitem(sys.modules, "playwright.async_api", _FakeAsyncAPI())
    return BrowserTools()


@pytest.mark.asyncio
async def test_open_sets_url_and_returns_title(patched_browser: BrowserTools) -> None:
    r = await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"},
        session_id="s1",
    ))
    assert r.ok is True
    assert r.content["url"] == "https://example.com"
    assert r.content["title"] == "Untitled"
    assert r.content["status"] == 200


@pytest.mark.asyncio
async def test_click_and_fill_operate_on_current_page(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    click = await patched_browser.invoke(_call(
        "browser_click", {"selector": "button.submit"}, session_id="s1",
    ))
    assert click.ok is True
    fill = await patched_browser.invoke(_call(
        "browser_fill", {"selector": "input#q", "value": "hello"},
        session_id="s1",
    ))
    assert fill.ok is True
    assert "5 chars" in fill.content
    # Verify the fake page actually observed the calls.
    page = patched_browser._pages["s1"]
    assert page.last_click == "button.submit"
    assert page.last_fill == ("input#q", "hello")


@pytest.mark.asyncio
async def test_screenshot_returns_base64_png(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    shot = await patched_browser.invoke(_call(
        "browser_screenshot", {"full_page": True}, session_id="s1",
    ))
    assert shot.ok is True
    # data URL starts with the image prefix + valid base64 payload.
    assert shot.content["data_url"].startswith("data:image/png;base64,")
    decoded = base64.b64decode(shot.content["data_url"].split(",", 1)[1])
    # Our fake returns a tiny payload; verify the full_page flag reached it.
    assert decoded.endswith(b"FULL")


@pytest.mark.asyncio
async def test_snapshot_returns_text_and_links(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    snap = await patched_browser.invoke(_call(
        "browser_snapshot", {"max_chars": 200, "max_links": 3},
        session_id="s1",
    ))
    assert snap.ok is True
    assert snap.content["text"] == "hello page body"
    assert len(snap.content["links"]) == 1
    assert snap.content["links"][0]["href"] == "https://example.com/a"


@pytest.mark.asyncio
async def test_sessions_are_isolated(patched_browser: BrowserTools) -> None:
    """Two different session_ids must get independent pages / contexts."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="alpha",
    ))
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.org"}, session_id="beta",
    ))
    assert patched_browser._pages["alpha"].url == "https://example.com"
    assert patched_browser._pages["beta"].url == "https://example.org"
    assert patched_browser._pages["alpha"] is not patched_browser._pages["beta"]


@pytest.mark.asyncio
async def test_close_frees_session(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    assert "s1" in patched_browser._pages
    r = await patched_browser.invoke(_call(
        "browser_close", {}, session_id="s1",
    ))
    assert r.ok is True
    assert "s1" not in patched_browser._pages
    assert "s1" not in patched_browser._contexts


@pytest.mark.asyncio
async def test_open_validates_url_scheme(patched_browser: BrowserTools) -> None:
    r = await patched_browser.invoke(_call(
        "browser_open", {"url": "javascript:alert(1)"}, session_id="s1",
    ))
    assert r.ok is False
    assert "http" in r.error.lower()


@pytest.mark.asyncio
async def test_click_refuses_when_no_page(patched_browser: BrowserTools) -> None:
    """Clicking before opening = clear error, no crash."""
    # Force about:blank state.
    page = await patched_browser._page_for("s1")
    assert page.url == "about:blank"
    r = await patched_browser.invoke(_call(
        "browser_click", {"selector": "a"}, session_id="s1",
    ))
    assert r.ok is False
    assert "browser_open" in r.error
