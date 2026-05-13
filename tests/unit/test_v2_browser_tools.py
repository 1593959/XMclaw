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
        "browser_open", "browser_click", "browser_press",
        "browser_fill", "browser_screenshot", "browser_snapshot",
        "browser_eval", "browser_close",
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


class _FakeLocatorChain:
    """Mimic the Playwright locator chain just enough for our tools."""

    def __init__(self, page: "_FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector
        self.first = self

    async def click(self, force: bool = False) -> None:
        self._page.last_click = self._selector
        self._page.last_click_force = force

    async def press(self, key: str) -> None:
        self._page.last_press = (self._selector, key)

    async def count(self) -> int:
        return 1


class _FakeKeyboard:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.last_press = (None, key)


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.title_value = "Untitled"
        self.evaluate_map: dict[str, Any] = {}
        self.last_click: str | None = None
        self.last_click_force: bool = False
        self.last_press: tuple[str | None, str] | None = None
        self.last_fill: tuple[str, str] | None = None
        self._closed = False
        self.keyboard = _FakeKeyboard(self)
        # Sites that "navigate" in tests can flip this so the URL
        # after click != before.
        self._navigate_to: str | None = None

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

    def locator(self, selector: str) -> _FakeLocatorChain:
        return _FakeLocatorChain(self, selector)

    async def wait_for_load_state(
        self, state: str = "load", timeout: int = 30000,
    ) -> None:
        # When the test set `_navigate_to`, simulate a redirect happening
        # during the load wait.
        if self._navigate_to:
            self.url = self._navigate_to
            self._navigate_to = None

    async def screenshot(
        self, full_page: bool = False, type: str = "png",
        quality: int = 80,
    ) -> bytes:
        # Tiny PNG/JPEG header -- enough to verify base64 encoding roundtrips.
        magic = b"\x89PNG\r\n\x1a\n" if type == "png" else b"\xff\xd8\xff\xe0"
        return magic + (b"FULL" if full_page else b"VIEW")

    async def evaluate(self, expr: str, *args: Any) -> Any:
        # Snapshot dispatches three different JS scripts; match the
        # most specific first.
        if "input, textarea, select" in expr:
            return [
                {
                    "kind": "input", "selector": "#q",
                    "name": "q", "type": "text",
                    "placeholder": "Search", "value": "",
                    "label": "Query", "text": None,
                },
                {
                    "kind": "button", "selector": "button[name=\"go\"]",
                    "name": "go", "type": "submit",
                    "placeholder": None, "value": None,
                    "label": "", "text": "Search",
                },
            ]
        if "a[href]" in expr:
            return [{"label": "Example Link", "href": "https://example.com/a"}]
        if "document.body" in expr or "innerText" in expr:
            return "hello page body"
        return self.evaluate_map.get(expr, expr)


@dataclass
class _FakeContext:
    pages: list[_FakePage] = field(default_factory=list)
    closed: bool = False

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p

    def set_default_timeout(self, _ms: int) -> None: pass

    async def close(self) -> None: self.closed = True


@dataclass
class _FakeBrowser:
    contexts: list[_FakeContext] = field(default_factory=list)

    async def new_context(self, **_: Any) -> _FakeContext:
        c = _FakeContext()
        self.contexts.append(c)
        return c

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


# ── Wave 22: enhanced click + press + snapshot forms + screenshot spill ──


@pytest.mark.asyncio
async def test_click_returns_rich_state_no_navigation(
    patched_browser: BrowserTools,
) -> None:
    """Click that doesn't navigate returns url unchanged + navigated=False."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_click", {"selector": "#some-button"}, session_id="s1",
    ))
    assert r.ok is True
    assert isinstance(r.content, dict)
    assert r.content["selector"] == "#some-button"
    assert r.content["url"] == "https://example.com"
    assert r.content["navigated"] is False
    # Fake page records the click target.
    assert patched_browser._pages["s1"].last_click == "#some-button"


@pytest.mark.asyncio
async def test_click_detects_navigation(
    patched_browser: BrowserTools,
) -> None:
    """When click triggers a URL change, navigated=True + new url + title."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/login"},
        session_id="s1",
    ))
    # Arm the fake to "navigate" on the next load_state wait.
    patched_browser._pages["s1"]._navigate_to = "https://example.com/dashboard"
    patched_browser._pages["s1"].title_value = "Dashboard"

    r = await patched_browser.invoke(_call(
        "browser_click", {"selector": "button.submit"}, session_id="s1",
    ))
    assert r.ok is True
    assert r.content["navigated"] is True
    assert r.content["url"] == "https://example.com/dashboard"
    assert r.content["title"] == "Dashboard"
    assert r.content["url_before"] == "https://example.com/login"


@pytest.mark.asyncio
async def test_click_force_flag_passes_through(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    await patched_browser.invoke(_call(
        "browser_click", {"selector": ".overlay-button", "force": True},
        session_id="s1",
    ))
    assert patched_browser._pages["s1"].last_click_force is True


@pytest.mark.asyncio
async def test_press_uses_keyboard_when_no_selector(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_press", {"key": "Enter"}, session_id="s1",
    ))
    assert r.ok is True
    assert r.content["key"] == "Enter"
    assert r.content["selector"] is None
    assert patched_browser._pages["s1"].last_press == (None, "Enter")


@pytest.mark.asyncio
async def test_press_uses_locator_when_selector_given(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    await patched_browser.invoke(_call(
        "browser_press",
        {"key": "Tab", "selector": "input[name=q]"},
        session_id="s1",
    ))
    assert patched_browser._pages["s1"].last_press == (
        "input[name=q]", "Tab",
    )


@pytest.mark.asyncio
async def test_press_detects_navigation_after_enter(
    patched_browser: BrowserTools,
) -> None:
    """Common case: user fills a search box, presses Enter → submits
    form → navigation. We expose that delta so the agent knows to
    snapshot fresh."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://search.example/start"},
        session_id="s1",
    ))
    patched_browser._pages["s1"]._navigate_to = "https://search.example/results?q=foo"
    patched_browser._pages["s1"].title_value = "Results"
    r = await patched_browser.invoke(_call(
        "browser_press", {"key": "Enter"}, session_id="s1",
    ))
    assert r.content["navigated"] is True
    assert "results" in r.content["url"]


@pytest.mark.asyncio
async def test_press_missing_key_returns_error(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_press", {}, session_id="s1",
    ))
    assert r.ok is False
    assert "key" in r.error.lower()


@pytest.mark.asyncio
async def test_snapshot_includes_inputs_and_buttons(
    patched_browser: BrowserTools,
) -> None:
    """Wave 22 — snapshot must surface form inputs + buttons so the
    agent doesn't have to guess CSS selectors."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/form"},
        session_id="s1",
    ))
    snap = await patched_browser.invoke(_call(
        "browser_snapshot", {}, session_id="s1",
    ))
    assert snap.ok is True
    assert "inputs" in snap.content
    inputs = snap.content["inputs"]
    kinds = {i["kind"] for i in inputs}
    assert "input" in kinds
    assert "button" in kinds
    # Selector + label info should make it through.
    by_sel = {i["selector"]: i for i in inputs}
    assert "#q" in by_sel
    assert by_sel["#q"]["placeholder"] == "Search"


@pytest.mark.asyncio
async def test_screenshot_jpeg_format(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_screenshot", {"format": "jpeg", "quality": 60},
        session_id="s1",
    ))
    assert r.ok is True
    assert r.content["mime"] == "image/jpeg"
    assert r.content["data_url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_screenshot_spills_to_disk_when_over_cap(
    patched_browser: BrowserTools, tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the encoded payload exceeds max_inline_bytes, the tool
    writes to disk + returns the path instead of bloating LLM context."""
    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir", lambda: tmp_path,
    )
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    # Set the cap absurdly low so even our tiny fake-PNG payload
    # spills.
    r = await patched_browser.invoke(_call(
        "browser_screenshot",
        {"full_page": True, "max_inline_bytes": 10},
        session_id="s1",
    ))
    assert r.ok is True
    assert "data_url" not in r.content
    assert "path" in r.content
    assert r.content["truncated"] is True
    assert str(tmp_path) in r.content["path"]
    # Side effects record the file path so HonestGrader can verify.
    assert r.side_effects and tmp_path.as_posix() in r.side_effects[0].replace("\\", "/")


# ── Wave 23: visible vs headless per-call selection ──────────────


@pytest.mark.asyncio
async def test_open_default_headless(patched_browser: BrowserTools) -> None:
    """No ``visible`` arg → session inherits BrowserTools' default
    (headless=True from constructor)."""
    r = await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    assert r.ok is True
    assert r.content["visible"] is False
    assert patched_browser._session_headless["s1"] is True


@pytest.mark.asyncio
async def test_open_visible_true_flips_mode(
    patched_browser: BrowserTools,
) -> None:
    """``visible: true`` pins the session to a visible window."""
    r = await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com", "visible": True},
        session_id="s_visible",
    ))
    assert r.ok is True
    assert r.content["visible"] is True
    assert patched_browser._session_headless["s_visible"] is False


@pytest.mark.asyncio
async def test_session_mode_sticks_after_first_open(
    patched_browser: BrowserTools,
) -> None:
    """Once a session has been opened with visible=True, a second
    browser_open in the same session ignores a contradictory flag —
    the agent doesn't have to re-thread visibility on every call."""
    await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com", "visible": True},
        session_id="s_pin",
    ))
    # Now open a second URL WITHOUT visible — should keep visible.
    r2 = await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.org"},
        session_id="s_pin",
    ))
    assert r2.content["visible"] is True
    assert patched_browser._session_headless["s_pin"] is False


@pytest.mark.asyncio
async def test_close_forgets_pinned_mode(
    patched_browser: BrowserTools,
) -> None:
    """After close, a fresh open can choose a different mode."""
    await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com", "visible": True},
        session_id="s_close",
    ))
    await patched_browser.invoke(_call(
        "browser_close", {}, session_id="s_close",
    ))
    assert "s_close" not in patched_browser._session_headless
    # Re-open without flag → falls back to default (headless).
    r3 = await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com"},
        session_id="s_close",
    ))
    assert r3.content["visible"] is False


@pytest.mark.asyncio
async def test_headless_and_visible_browsers_independent(
    patched_browser: BrowserTools,
) -> None:
    """Two sessions in different modes should land in different
    browser handles — verified via the internal cache attrs."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://h.example"}, session_id="hidden",
    ))
    await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://v.example", "visible": True},
        session_id="visible",
    ))
    # Both browsers booted.
    assert patched_browser._browser_headless is not None
    assert patched_browser._browser_headed is not None
    # Each session pinned its own mode.
    assert patched_browser._session_headless["hidden"] is True
    assert patched_browser._session_headless["visible"] is False
