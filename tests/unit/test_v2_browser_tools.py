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
        "browser_open", "browser_click", "browser_press",
        "browser_fill", "browser_hover", "browser_scroll",
        "browser_select_option", "browser_upload", "browser_wait_for",
        "browser_back", "browser_forward", "browser_reload",
        "browser_tabs", "browser_tab_switch", "browser_tab_close",
        "browser_download_next",
        "browser_save_state", "browser_list_states",
        # Wave-27 fix-LAT8: external cookie import for skipping
        # third-party logins.
        "browser_import_cookies",
        "browser_get_console",
        "browser_screenshot", "browser_snapshot",
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

    async def hover(self) -> None:
        self._page.last_hover = self._selector

    async def scroll_into_view_if_needed(self) -> None:
        self._page.last_scroll_target = self._selector

    async def select_option(self, arg: Any) -> list[str]:
        # Echo back the value(s) so the tool can return a useful payload.
        if isinstance(arg, dict):
            val = arg.get("value") or arg.get("label")
            self._page.last_select = (self._selector, val)
            return [str(val)]
        if isinstance(arg, list):
            self._page.last_select = (self._selector, arg)
            return [str(v) for v in arg]
        self._page.last_select = (self._selector, arg)
        return [str(arg)]

    async def set_input_files(self, files: Any) -> None:
        self._page.last_upload = (self._selector, files)

    async def wait_for(self, state: str = "visible", timeout: int = 10000) -> None:
        self._page.last_wait_for = (self._selector, state, timeout)


class _FakeKeyboard:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.last_press = (None, key)


class _FakeMouse:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def wheel(self, dx: float, dy: float) -> None:
        self._page.last_wheel = (dx, dy)


class _FakeFrame:
    """Minimal iframe stand-in — has `name`, `url`, and a `.locator()`
    that records hits on the parent page so tests can verify routing."""

    def __init__(self, page: "_FakePage", name: str, url: str) -> None:
        self._page = page
        self.name = name
        self.url = url

    def locator(self, selector: str) -> _FakeLocatorChain:
        # Tag the selector with the frame name so tests can confirm
        # action routed into the frame, not the top page.
        chain = _FakeLocatorChain(self._page, selector)
        self._page.last_frame_locator = (self.name, selector)
        return chain


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.title_value = "Untitled"
        self.evaluate_map: dict[str, Any] = {}
        self.last_click: str | None = None
        self.last_click_force: bool = False
        self.last_press: tuple[str | None, str] | None = None
        self.last_fill: tuple[str, str] | None = None
        self.last_hover: str | None = None
        self.last_scroll_target: str | None = None
        self.last_wheel: tuple[float, float] | None = None
        self.last_select: tuple[str, Any] | None = None
        self.last_upload: tuple[str, Any] | None = None
        self.last_wait_for: tuple[str, str, int] | None = None
        self.last_history: str | None = None
        self.last_frame_locator: tuple[str, str] | None = None
        self._console_handlers: list[Any] = []
        self._pageerror_handlers: list[Any] = []
        self._closed = False
        self.keyboard = _FakeKeyboard(self)
        self.mouse = _FakeMouse(self)
        # `name` is queried by _resolve_locator when matching frame_name=
        self.name = ""
        # Tests can populate fake iframes to drive _resolve_locator.
        self.frames: list[Any] = [self]  # top frame == this page
        # Sites that "navigate" in tests can flip this so the URL
        # after click != before.
        self._navigate_to: str | None = None

    def on(self, event: str, handler: Any) -> None:
        if event == "console":
            self._console_handlers.append(handler)
        elif event == "pageerror":
            self._pageerror_handlers.append(handler)

    def emit_console(self, level: str, text: str) -> None:
        """Test helper: simulate a console event."""
        class _M:
            type = level
        m = _M()
        m.text = text  # type: ignore[attr-defined]
        for h in list(self._console_handlers):
            h(m)

    def emit_pageerror(self, msg: str) -> None:
        for h in list(self._pageerror_handlers):
            h(msg)

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

    async def go_back(self) -> None:
        self.last_history = "back"
        if self._navigate_to:
            self.url = self._navigate_to
            self._navigate_to = None

    async def go_forward(self) -> None:
        self.last_history = "forward"
        if self._navigate_to:
            self.url = self._navigate_to
            self._navigate_to = None

    async def reload(self) -> None:
        self.last_history = "reload"

    async def bring_to_front(self) -> None:
        pass

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
        if "window.scrollTo" in expr:
            # scroll-to-top / scroll-to-bottom branches in _scroll.
            self.last_scroll_eval = expr
            return None
        if "document.body" in expr or "innerText" in expr:
            return "hello page body"
        return self.evaluate_map.get(expr, expr)


@dataclass
class _FakeContext:
    pages: list[_FakePage] = field(default_factory=list)
    closed: bool = False
    init_scripts: list[str] = field(default_factory=list)
    user_agent: str | None = None
    storage_state_load_path: str | None = None
    storage_state_save_path: str | None = None

    async def new_page(self) -> _FakePage:
        p = _FakePage()
        self.pages.append(p)
        return p

    def set_default_timeout(self, _ms: int) -> None: pass

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def storage_state(self, path: str | None = None) -> Any:
        if path is not None:
            from pathlib import Path as _P
            _P(path).parent.mkdir(parents=True, exist_ok=True)
            _P(path).write_text(
                '{"cookies":[],"origins":[]}', encoding="utf-8",
            )
            self.storage_state_save_path = path
        return {"cookies": [], "origins": []}

    async def close(self) -> None: self.closed = True


@dataclass
class _FakeBrowser:
    contexts: list[_FakeContext] = field(default_factory=list)
    last_launch_args: list[str] = field(default_factory=list)

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        c = _FakeContext(
            user_agent=kwargs.get("user_agent"),
            storage_state_load_path=(
                kwargs.get("storage_state")
                if isinstance(kwargs.get("storage_state"), str)
                else None
            ),
        )
        self.contexts.append(c)
        return c

    async def close(self) -> None: pass


@dataclass
class _FakeChromium:
    browser: _FakeBrowser = field(default_factory=_FakeBrowser)

    async def launch(
        self, headless: bool = True, args: list[str] | None = None,
    ) -> _FakeBrowser:
        if args:
            self.browser.last_launch_args = list(args)
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


# ── Wave 24: deep automation surface ─────────────────────────────


@pytest.mark.asyncio
async def test_hover_records_target(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_hover", {"selector": ".menu-trigger"}, session_id="s1",
    ))
    assert r.ok is True
    assert patched_browser._pages["s1"].last_hover == ".menu-trigger"


@pytest.mark.asyncio
async def test_scroll_to_selector(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_scroll", {"to_selector": "#footer"}, session_id="s1",
    ))
    assert r.ok is True
    assert patched_browser._pages["s1"].last_scroll_target == "#footer"


@pytest.mark.asyncio
async def test_scroll_direction_down_uses_mouse_wheel(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_scroll", {"direction": "down", "amount": 500},
        session_id="s1",
    ))
    assert r.ok is True
    assert patched_browser._pages["s1"].last_wheel == (0, 500)


@pytest.mark.asyncio
async def test_scroll_direction_top_uses_evaluate(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_scroll", {"direction": "top"}, session_id="s1",
    ))
    assert r.ok is True
    assert "scrollTo" in getattr(
        patched_browser._pages["s1"], "last_scroll_eval", "",
    )


@pytest.mark.asyncio
async def test_select_option_string_value(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_select_option",
        {"selector": "select[name=country]", "value": "CN"},
        session_id="s1",
    ))
    assert r.ok is True
    sel, val = patched_browser._pages["s1"].last_select
    assert sel == "select[name=country]"
    assert val == "CN"


@pytest.mark.asyncio
async def test_upload_validates_files_exist(
    patched_browser: BrowserTools, tmp_path,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r_bad = await patched_browser.invoke(_call(
        "browser_upload",
        {"selector": "input[type=file]", "files": str(tmp_path / "missing")},
        session_id="s1",
    ))
    assert r_bad.ok is False
    assert "not found" in r_bad.error.lower()

    real = tmp_path / "ok.txt"
    real.write_text("hi", encoding="utf-8")
    r_good = await patched_browser.invoke(_call(
        "browser_upload",
        {"selector": "input[type=file]", "files": str(real)},
        session_id="s1",
    ))
    assert r_good.ok is True
    assert patched_browser._pages["s1"].last_upload[1] == [str(real)]


@pytest.mark.asyncio
async def test_wait_for_passes_state_and_timeout(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_wait_for",
        {"selector": "#dynamic", "state": "hidden", "timeout_ms": 5000},
        session_id="s1",
    ))
    assert r.ok is True
    last = patched_browser._pages["s1"].last_wait_for
    assert last == ("#dynamic", "hidden", 5000)


@pytest.mark.asyncio
async def test_wait_for_rejects_bad_state(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_wait_for",
        {"selector": "#x", "state": "vanished"},
        session_id="s1",
    ))
    assert r.ok is False
    assert "state" in r.error.lower()


@pytest.mark.asyncio
async def test_back_forward_reload(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/a"},
        session_id="s1",
    ))
    patched_browser._pages["s1"]._navigate_to = "https://example.com/prev"
    r_back = await patched_browser.invoke(_call(
        "browser_back", {}, session_id="s1",
    ))
    assert r_back.ok is True
    assert r_back.content["op"] == "back"
    assert patched_browser._pages["s1"].last_history == "back"

    r_fwd = await patched_browser.invoke(_call(
        "browser_forward", {}, session_id="s1",
    ))
    assert r_fwd.ok is True
    assert patched_browser._pages["s1"].last_history == "forward"

    r_rel = await patched_browser.invoke(_call(
        "browser_reload", {}, session_id="s1",
    ))
    assert r_rel.ok is True
    assert patched_browser._pages["s1"].last_history == "reload"


@pytest.mark.asyncio
async def test_tabs_list_returns_pages_in_context(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/tab1"},
        session_id="s1",
    ))
    ctx = patched_browser._contexts["s1"]
    second = await ctx.new_page()
    second.url = "https://example.com/popup"
    second.title_value = "Popup"

    r = await patched_browser.invoke(_call(
        "browser_tabs", {}, session_id="s1",
    ))
    assert r.ok is True
    tabs = r.content["tabs"]
    assert len(tabs) == 2
    urls = [t["url"] for t in tabs]
    assert "https://example.com/tab1" in urls
    assert "https://example.com/popup" in urls
    active = [t for t in tabs if t["active"]]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_tab_switch_changes_active_page(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/tab1"},
        session_id="s1",
    ))
    ctx = patched_browser._contexts["s1"]
    second = await ctx.new_page()
    second.url = "https://example.com/popup"
    second.title_value = "Popup"

    r = await patched_browser.invoke(_call(
        "browser_tab_switch", {"index": 1}, session_id="s1",
    ))
    assert r.ok is True
    assert r.content["index"] == 1
    assert patched_browser._pages["s1"].url == "https://example.com/popup"


@pytest.mark.asyncio
async def test_tab_switch_out_of_range(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_tab_switch", {"index": 99}, session_id="s1",
    ))
    assert r.ok is False
    assert "out of range" in r.error


@pytest.mark.asyncio
async def test_tab_close_removes_page(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com/tab1"},
        session_id="s1",
    ))
    ctx = patched_browser._contexts["s1"]
    second = await ctx.new_page()
    second.url = "https://example.com/popup"

    r = await patched_browser.invoke(_call(
        "browser_tab_close", {"index": 1}, session_id="s1",
    ))
    assert r.ok is True
    assert second.is_closed() is True


# ── Wave 24: stealth defaults ────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_launches_with_anti_automation_flag(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    launched = patched_browser._browser_headless
    assert launched is not None
    assert any(
        "AutomationControlled" in a for a in launched.last_launch_args
    )


@pytest.mark.asyncio
async def test_context_uses_real_chrome_ua_and_init_script(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    ctx = patched_browser._contexts["s1"]
    assert ctx.user_agent is not None
    assert "HeadlessChrome" not in ctx.user_agent
    assert "Chrome/" in ctx.user_agent
    joined = "\n".join(ctx.init_scripts)
    # The stealth script defines navigator.webdriver via
    # Object.defineProperty(navigator, 'webdriver', ...) — check for the
    # property name itself (which is the load-bearing identifier).
    assert "'webdriver'" in joined
    assert "'plugins'" in joined
    assert "window.chrome" in joined


# ── Wave 25.1: iframe traversal selector syntax ──────────────────


@pytest.mark.asyncio
async def test_iframe_click_by_name(patched_browser: BrowserTools) -> None:
    """frame_name=foo>>selector routes the click into the named iframe."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.frames = [
        page,
        _FakeFrame(page, "payment", "https://payments.example/inner"),
    ]
    r = await patched_browser.invoke(_call(
        "browser_click",
        {"selector": "frame_name=payment>>button.pay"},
        session_id="s1",
    ))
    assert r.ok is True
    assert page.last_frame_locator == ("payment", "button.pay")


@pytest.mark.asyncio
async def test_iframe_unknown_name_returns_error(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_click",
        {"selector": "frame_name=nope>>button"},
        session_id="s1",
    ))
    assert r.ok is False
    assert "nope" in r.error


@pytest.mark.asyncio
async def test_iframe_by_url_substring(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.frames = [
        page,
        _FakeFrame(page, "", "https://stripe.com/v3/payment-form"),
        _FakeFrame(page, "", "https://example.com/sidebar"),
    ]
    r = await patched_browser.invoke(_call(
        "browser_click",
        {"selector": "frame_url=stripe.com>>#submit"},
        session_id="s1",
    ))
    assert r.ok is True
    assert page.last_frame_locator == ("", "#submit")


@pytest.mark.asyncio
async def test_iframe_by_index(patched_browser: BrowserTools) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.frames = [
        page,
        _FakeFrame(page, "a", "https://example.com/a"),
        _FakeFrame(page, "b", "https://example.com/b"),
    ]
    r = await patched_browser.invoke(_call(
        "browser_click",
        {"selector": "frame_index=2>>button"},
        session_id="s1",
    ))
    assert r.ok is True
    assert page.last_frame_locator == ("b", "button")


@pytest.mark.asyncio
async def test_iframe_index_out_of_range(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_click",
        {"selector": "frame_index=99>>button"},
        session_id="s1",
    ))
    assert r.ok is False
    assert "out of range" in r.error


@pytest.mark.asyncio
async def test_iframe_syntax_works_for_hover(
    patched_browser: BrowserTools,
) -> None:
    """Every verb should accept frame selectors uniformly."""
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.frames = [page, _FakeFrame(page, "menu", "https://example.com/menu")]
    r = await patched_browser.invoke(_call(
        "browser_hover",
        {"selector": "frame_name=menu>>.dropdown"},
        session_id="s1",
    ))
    assert r.ok is True
    assert page.last_frame_locator == ("menu", ".dropdown")


# ── Wave 25.2: storage_state save/load ───────────────────────────


@pytest.mark.asyncio
async def test_save_and_list_state(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r_save = await patched_browser.invoke(_call(
        "browser_save_state", {"name": "github_login"}, session_id="s1",
    ))
    assert r_save.ok is True
    assert r_save.content["name"] == "github_login"
    assert "github_login.json" in r_save.content["path"]

    r_list = await patched_browser.invoke(_call(
        "browser_list_states", {}, session_id="s1",
    ))
    assert r_list.ok is True
    names = [p["name"] for p in r_list.content["profiles"]]
    assert "github_login" in names


@pytest.mark.asyncio
async def test_save_state_rejects_bad_name(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_save_state",
        {"name": "../traversal"},  # path traversal attempt
        session_id="s1",
    ))
    assert r.ok is False
    assert "name" in r.error


@pytest.mark.asyncio
async def test_open_load_state_hydrates_context(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    """Save in session A, load_state in fresh session B → ctx gets the
    storage_state path."""
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s_save",
    ))
    await patched_browser.invoke(_call(
        "browser_save_state", {"name": "my_login"}, session_id="s_save",
    ))
    await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com", "load_state": "my_login"},
        session_id="s_load",
    ))
    ctx = patched_browser._contexts["s_load"]
    assert ctx.storage_state_load_path is not None
    assert "my_login.json" in ctx.storage_state_load_path


@pytest.mark.asyncio
async def test_load_state_missing_profile_clean_error(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    r = await patched_browser.invoke(_call(
        "browser_open",
        {"url": "https://example.com", "load_state": "never_saved"},
        session_id="s1",
    ))
    assert r.ok is False
    assert "not found" in r.error


# ── Wave 25.3: console + pageerror capture ───────────────────────


@pytest.mark.asyncio
async def test_console_buffer_collects_messages(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.emit_console("error", "TypeError: undefined is not a function")
    page.emit_console("warning", "deprecated API")
    page.emit_console("log", "ok")
    page.emit_pageerror("ReferenceError: x is not defined")

    r = await patched_browser.invoke(_call(
        "browser_get_console", {}, session_id="s1",
    ))
    assert r.ok is True
    entries = r.content["entries"]
    # 3 console + 1 pageerror, newest first.
    assert len(entries) == 4
    texts = [e["text"] for e in entries]
    assert "ReferenceError: x is not defined" in texts
    assert "TypeError: undefined is not a function" in texts


@pytest.mark.asyncio
async def test_console_filter_by_level(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.emit_console("error", "boom")
    page.emit_console("log", "noise")
    page.emit_console("error", "second boom")

    r = await patched_browser.invoke(_call(
        "browser_get_console", {"level": "error"}, session_id="s1",
    ))
    assert r.ok is True
    entries = r.content["entries"]
    assert len(entries) == 2
    assert all(e["level"] == "error" for e in entries)


@pytest.mark.asyncio
async def test_console_clear_drains_buffer(
    patched_browser: BrowserTools,
) -> None:
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    page = patched_browser._pages["s1"]
    page.emit_console("log", "first")
    await patched_browser.invoke(_call(
        "browser_get_console", {"clear": True}, session_id="s1",
    ))
    page.emit_console("log", "second")
    r = await patched_browser.invoke(_call(
        "browser_get_console", {}, session_id="s1",
    ))
    texts = [e["text"] for e in r.content["entries"]]
    assert texts == ["second"]


# ── Wave 25.4: two-step download ticketing ───────────────────────


@pytest.mark.asyncio
async def test_download_arm_returns_ticket(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    """Bare arm (no and_then_click, no ticket) → register listener +
    return a ticket the agent uses to collect later."""
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    # _FakePage doesn't have wait_for_event — patch it to a future the
    # test resolves manually.
    page = patched_browser._pages["s1"]
    armed_future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _fake_wait(event, timeout):
        return await armed_future

    page.wait_for_event = _fake_wait  # type: ignore[attr-defined]

    r = await patched_browser.invoke(_call(
        "browser_download_next", {"timeout_ms": 30_000}, session_id="s1",
    ))
    assert r.ok is True
    ticket = r.content["ticket"]
    assert r.content["status"] == "armed"
    assert (("s1", ticket) in patched_browser._pending_downloads)
    # Clean up — cancel the pending task.
    armed_future.cancel()
    patched_browser._pending_downloads.pop(("s1", ticket), None)


@pytest.mark.asyncio
async def test_download_collect_with_bad_ticket(
    patched_browser: BrowserTools, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    await patched_browser.invoke(_call(
        "browser_open", {"url": "https://example.com"}, session_id="s1",
    ))
    r = await patched_browser.invoke(_call(
        "browser_download_next",
        {"ticket": "bogus_ticket"},
        session_id="s1",
    ))
    assert r.ok is False
    assert "unknown ticket" in r.error
