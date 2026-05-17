"""Playwright-backed browser tools: open / click / fill / screenshot /
snapshot / close.

Design notes
------------
*Lazy import* — playwright is an optional extra (``pip install xmclaw[browser]``)
and brings in ~200MB of bundled browser binaries. We refuse to pay the
import cost (or crash the daemon) if it's not installed. Every tool
invocation checks availability via ``_ensure_playwright`` and returns a
structured error if the library is missing -- the rest of the daemon
keeps working either way.

*Per-session context* — each session_id gets its own persistent
``BrowserContext`` (one shared browser process across sessions). That
way a user can do a sequence of ``browser_open -> browser_fill ->
browser_click -> browser_screenshot`` and have state survive across
tool calls within one agent turn. Sessions are garbage-collected when
the agent loop calls ``close_session`` (hooked into AgentLoop's
clear_session path) or the BrowserTools instance is destroyed.

*Security posture* — this is a local agent the user installed; by
default we launch headless Chromium with the user-level network. Two
opt-in guards exist:

  - ``allowed_hosts`` — if set, navigation to any other host is refused.
    None (default) means "go anywhere the user's network allows". Bot
    detection / captcha are NOT bypassed; the user gets whatever
    raw behavior Chromium produces.
  - ``download_dir`` — where file downloads land. None means downloads
    are DISABLED (Playwright's default accept_downloads=False).

Tools exposed
-------------
browser_open(url)            -> navigates the active page (creates one if needed)
browser_click(selector)      -> clicks an element by CSS/text selector
browser_fill(selector, value) -> fills a form field
browser_screenshot()         -> returns base64 PNG of the viewport
browser_snapshot()           -> returns extracted text + link list (lightweight a11y view)
browser_eval(expression)     -> runs JS in the page and returns the result (JSON-safe)
browser_close()              -> closes this session's page/context

The agent sees these as plain tools with JSON schemas -- no XMclaw-
specific API surface. Failures come back as ``ToolResult(ok=False,
error=...)`` with human-readable reasons.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib.parse import urlparse

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# Wave 24 stealth — pretend we're a normal headed Chrome. Sites pattern-
# match "HeadlessChrome" in UA → "your browser is too old" banner; we
# strip that and overwrite a few other automation fingerprints in the
# init script below. This is not a full bot bypass — modern Cloudflare /
# Akamai will still fingerprint us via canvas / WebGL / timing. But for
# the 90% of normal sites that just check UA + navigator.webdriver this
# is enough.
_REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_STEALTH_SCRIPT = r"""
// Drop the automation marker most bot detectors check.
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
// Real Chrome exposes a non-empty `plugins` array; headless ships an
// empty one which is a giveaway.
try {
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });
} catch (e) {}
// Real Chrome exposes `window.chrome` with a runtime stub.
try {
    if (!window.chrome) {
        window.chrome = { runtime: {} };
    }
} catch (e) {}
// Permission API spoof — sites that check Notification permission to
// detect headless get the same answer regardless of state.
try {
    const _origQuery = navigator.permissions.query;
    navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery.call(navigator.permissions, parameters)
    );
} catch (e) {}
"""


# ── tool specs ────────────────────────────────────────────────────

_BROWSER_OPEN_SPEC = ToolSpec(
    name="browser_open",
    description=(
        "Navigate **MY internal Playwright Chromium** to a URL. This "
        "is the AGENT's browser, not the user's — I drive it via "
        "browser_click / browser_fill / browser_eval / etc., and the "
        "user only sees what I take screenshots of (browser_screenshot) "
        "or describe.\n\n"
        "★ Decision rule — pick the right tool:\n"
        "  • ``open_in_user_browser(url)`` → the USER'S real desktop "
        "browser (Chrome/Edge), with their bookmarks / extensions / "
        "saved logins / 2FA. Use when the user needs to SEE, INTERACT, "
        "CAPTCHA, log in manually, or just look at a result link.\n"
        "  • ``browser_open(url)`` (THIS tool) → my own Playwright "
        "instance. Use for automation: scraping, data extraction, "
        "filling forms FOR the user (not BY the user), running JS, "
        "taking screenshots for ME to look at. The user can NOT see "
        "this browser.\n\n"
        "If you need the user to log into a site, use "
        "open_in_user_browser → user logs in → user runs the cookie-"
        "export extension → user pastes cookies → "
        "browser_import_cookies(name=..., cookies_json=...) → "
        "browser_open(url=..., load_state=name) — that's how you get "
        "MY headless browser into a logged-in state without me ever "
        "touching the user's password.\n\n"
        "``visible``: false (default) → headless. true → real visible "
        "Chromium window — but this is STILL MY Playwright instance, "
        "NOT the user's normal browser; bookmarks/extensions/cookies "
        "are all empty fresh state. Almost always you want "
        "open_in_user_browser instead. Returns the final URL + title."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full http(s) URL to navigate to.",
            },
            "wait_until": {
                "type": "string",
                "description": "'load' | 'domcontentloaded' | 'networkidle'. Default 'load'.",
            },
            "visible": {
                "type": "boolean",
                "description": (
                    "Show a real browser window (default false = "
                    "headless). Visible mode uses a separate browser "
                    "process; subsequent browser_* calls in the same "
                    "session keep using whichever mode the session "
                    "started in."
                ),
            },
            "load_state": {
                "type": "string",
                "description": (
                    "Name of a previously saved storage-state profile "
                    "(see browser_save_state). When set, the session's "
                    "context starts pre-populated with that profile's "
                    "cookies + localStorage so the page sees you as "
                    "already logged in. Must be passed on the FIRST "
                    "browser_open of a session; subsequent calls in "
                    "the same session ignore it."
                ),
            },
        },
        "required": ["url"],
    },
)

_BROWSER_CLICK_SPEC = ToolSpec(
    name="browser_click",
    description=(
        "Click an element. Auto-waits for the element to become visible "
        "(timeout via context default ~15s), scrolls it into view, "
        "clicks, then detects whether a navigation occurred. Returns "
        "the post-click URL + title so the agent knows whether to "
        "browser_snapshot again."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector, 'text=...' / 'role=...' Playwright "
                    "locator, or 'nth=N selector' to pick the Nth match "
                    "(0-indexed) when the selector is ambiguous."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Bypass Playwright's actionability checks (covered "
                    "by overlay, not stable, etc.). Default false."
                ),
            },
            "wait_for_navigation_ms": {
                "type": "integer",
                "description": (
                    "Max ms to wait for a navigation that the click "
                    "might trigger. 0 = don't wait, just report URL "
                    "delta. Default 2000."
                ),
            },
        },
        "required": ["selector"],
    },
)

_BROWSER_PRESS_SPEC = ToolSpec(
    name="browser_press",
    description=(
        "Press a key (or chord) on the focused element. Use this after "
        "browser_fill to submit a form via Enter, navigate dropdowns "
        "with Arrow keys, dismiss modals with Escape, etc. Key syntax "
        "matches Playwright: 'Enter', 'Tab', 'Escape', 'ArrowDown', "
        "'PageDown', 'Control+A', 'Shift+Tab', etc."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Single key or chord like 'Control+A'.",
            },
            "selector": {
                "type": "string",
                "description": (
                    "Optional — if set, focus this element first via "
                    "page.locator(selector).press(key). If omitted, "
                    "uses page.keyboard.press(key) on whatever is "
                    "currently focused."
                ),
            },
        },
        "required": ["key"],
    },
)

_BROWSER_FILL_SPEC = ToolSpec(
    name="browser_fill",
    description="Fill an input/textarea element's value.",
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "value":    {"type": "string"},
        },
        "required": ["selector", "value"],
    },
)

_BROWSER_SCREENSHOT_SPEC = ToolSpec(
    name="browser_screenshot",
    description=(
        "Take a screenshot of the current viewport. Returns a base64 "
        "data URL by default; if the encoded payload would exceed "
        "``max_inline_bytes`` (default 512 KB) it falls back to "
        "writing a file under ~/.xmclaw/v2/screenshots/ and returns "
        "the path instead — so a full-page capture of a long article "
        "doesn't blow the LLM context window. Full-page screenshots "
        "are opt-in via ``full_page=true``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "Capture the entire scrollable page. Default false.",
            },
            "format": {
                "type": "string",
                "description": "'png' (lossless) or 'jpeg' (smaller). Default 'png'.",
            },
            "quality": {
                "type": "integer",
                "description": "1-100, JPEG only. Default 80.",
            },
            "max_inline_bytes": {
                "type": "integer",
                "description": "Cap on the inline data_url. Larger captures spill to disk. Default 524288.",
            },
        },
    },
)

_BROWSER_SNAPSHOT_SPEC = ToolSpec(
    name="browser_snapshot",
    description=(
        "Return a lightweight text+link+form view of the current page -- "
        "good for LLM reasoning over content without the image-parsing "
        "overhead. Includes title, visible text (truncated), top-N "
        "hyperlinks, AND form inputs + buttons with their selectors so "
        "the agent can fill / click without guessing CSS."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Visible-text truncation cap. Default 8000.",
            },
            "max_links": {
                "type": "integer",
                "description": "Top-N links to surface. Default 30.",
            },
            "max_inputs": {
                "type": "integer",
                "description": "Top-N inputs / buttons to surface. Default 20.",
            },
        },
    },
)

_BROWSER_EVAL_SPEC = ToolSpec(
    name="browser_eval",
    description=(
        "Execute JavaScript in the page context and return its result. "
        "Use for quick DOM queries, checking page state, or getting "
        "computed values. Result must be JSON-serializable."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
        },
        "required": ["expression"],
    },
)

_BROWSER_CLOSE_SPEC = ToolSpec(
    name="browser_close",
    description="Close this session's browser page + context.",
    parameters_schema={"type": "object", "properties": {}},
)


# ── Wave 24: deeper automation surface ───────────────────────────

_BROWSER_HOVER_SPEC = ToolSpec(
    name="browser_hover",
    description=(
        "Hover the mouse over an element without clicking. Use this "
        "before clicking a menu item that only appears on hover (drop-"
        "downs, popovers, file/edit menus in web apps)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"selector": {"type": "string"}},
        "required": ["selector"],
    },
)

_BROWSER_SCROLL_SPEC = ToolSpec(
    name="browser_scroll",
    description=(
        "Scroll the page. Two modes:\n"
        "  • Pixel mode — ``direction`` ∈ {'up','down','top','bottom'} "
        "+ optional ``amount`` (default 800 px for up/down)\n"
        "  • Selector mode — ``to_selector`` scrolls until the element "
        "is in view. Useful when content lazy-loads on scroll."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "direction": {"type": "string"},
            "amount": {"type": "integer"},
            "to_selector": {"type": "string"},
        },
    },
)

_BROWSER_SELECT_OPTION_SPEC = ToolSpec(
    name="browser_select_option",
    description=(
        "Pick an option in a native <select> dropdown. ``value`` can be "
        "the option's value attribute or its visible label — Playwright "
        "matches either. For multi-select pass a list."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "value": {
                "type": ["string", "array"],
                "description": "Single value/label or list for multi-select.",
            },
        },
        "required": ["selector", "value"],
    },
)

_BROWSER_UPLOAD_SPEC = ToolSpec(
    name="browser_upload",
    description=(
        "Attach one or more local files to an <input type='file'>. "
        "``files`` is a list of absolute paths the daemon can read."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "files": {
                "type": ["string", "array"],
                "description": "Single path or list of paths to attach.",
            },
        },
        "required": ["selector", "files"],
    },
)

_BROWSER_WAIT_FOR_SPEC = ToolSpec(
    name="browser_wait_for",
    description=(
        "Explicit wait until a selector reaches a state — useful for "
        "post-AJAX content where auto-wait isn't enough. ``state`` ∈ "
        "{'attached','detached','visible','hidden'}, default 'visible'."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "state": {"type": "string"},
            "timeout_ms": {"type": "integer"},
        },
        "required": ["selector"],
    },
)

_BROWSER_BACK_SPEC = ToolSpec(
    name="browser_back",
    description="Navigate back one step in this page's history.",
    parameters_schema={"type": "object", "properties": {}},
)

_BROWSER_FORWARD_SPEC = ToolSpec(
    name="browser_forward",
    description="Navigate forward one step in this page's history.",
    parameters_schema={"type": "object", "properties": {}},
)

_BROWSER_RELOAD_SPEC = ToolSpec(
    name="browser_reload",
    description="Reload the current page.",
    parameters_schema={"type": "object", "properties": {}},
)

_BROWSER_TABS_SPEC = ToolSpec(
    name="browser_tabs",
    description=(
        "List every tab/page in this session's browser context. Returns "
        "{index, url, title, active} for each. Use index with "
        "browser_tab_switch / browser_tab_close. New tabs that open via "
        "target=_blank or window.open() show up here automatically."
    ),
    parameters_schema={"type": "object", "properties": {}},
)

_BROWSER_TAB_SWITCH_SPEC = ToolSpec(
    name="browser_tab_switch",
    description=(
        "Make a different tab the active page for subsequent browser_* "
        "calls in this session. Index from browser_tabs."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    },
)

_BROWSER_TAB_CLOSE_SPEC = ToolSpec(
    name="browser_tab_close",
    description=(
        "Close a tab by index. If the active tab is closed, the next "
        "remaining tab becomes active. Closing the last tab leaves the "
        "session without an active page; browser_open creates a new one."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    },
)

_BROWSER_SAVE_STATE_SPEC = ToolSpec(
    name="browser_save_state",
    description=(
        "Snapshot the current session's cookies + localStorage to a "
        "named profile at ~/.xmclaw/v2/browser_state/<name>.json. "
        "Pair with browser_open(load_state=name) on a future session "
        "to skip a manual login. Profiles are local files — they ride "
        "filesystem permissions, not encryption."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Profile name (alphanumeric / dashes / underscores).",
            },
        },
        "required": ["name"],
    },
)

_BROWSER_LIST_STATES_SPEC = ToolSpec(
    name="browser_list_states",
    description=(
        "List saved storage-state profiles (from browser_save_state). "
        "Returns [{name, saved_ts, size_bytes}]."
    ),
    parameters_schema={"type": "object", "properties": {}},
)


_BROWSER_IMPORT_COOKIES_SPEC = ToolSpec(
    name="browser_import_cookies",
    description=(
        "Import cookies from an EXTERNAL source (Chrome 'EditThisCookie' "
        "export, raw cookies array, or a JSON file on disk) into a "
        "storage-state profile that browser_open(load_state=name) can "
        "use later. Wave-27 fix-LAT8 — the third-party-login workflow "
        "the user is realistically going to take is: open the site in "
        "Chrome manually, log in via 2FA / CAPTCHA themselves, export "
        "cookies with the EditThisCookie extension, paste the JSON "
        "here. That import lands in "
        "~/.xmclaw/v2/browser_state/<name>.json, then a future agent "
        "turn calls browser_open(load_state=name, url=...) and the "
        "Playwright context starts already-logged-in. Pass EITHER "
        "``cookies_json`` (a JSON-serialised array of cookie objects "
        "OR a full {cookies, origins} object) OR ``cookies_path`` (a "
        "path to a JSON file). The 'array of cookie objects' shape "
        "is what most browser extensions produce."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Profile name (alphanumeric/dash/underscore). "
                    "Used as the filename: <name>.json under "
                    "~/.xmclaw/v2/browser_state/."
                ),
            },
            "cookies_json": {
                "type": "string",
                "description": (
                    "Inline JSON. Either a Playwright storage_state "
                    "object ``{cookies: [...], origins: [...]}`` or a "
                    "bare array ``[{...cookie...}, ...]`` which will "
                    "be wrapped as ``{cookies: <arr>, origins: []}``."
                ),
            },
            "cookies_path": {
                "type": "string",
                "description": (
                    "Path to a JSON file with the same shape as "
                    "``cookies_json``. Use this for files >100 KB to "
                    "avoid blowing your tool-arg budget."
                ),
            },
        },
        "required": ["name"],
    },
)

_BROWSER_GET_CONSOLE_SPEC = ToolSpec(
    name="browser_get_console",
    description=(
        "Return console messages + page errors captured since this "
        "session's browser_open. Useful when a page misbehaves and "
        "the agent wants to see JS errors / warnings without "
        "browser_eval-ing console.log directly. Buffer is per-session, "
        "capped at the most recent 200 entries."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "description": (
                    "Filter by level: 'error' / 'warning' / 'info' / "
                    "'log' / 'debug'. Default 'all'."
                ),
            },
            "max": {"type": "integer", "description": "Default 50."},
            "clear": {
                "type": "boolean",
                "description": (
                    "Drain the buffer after reading. Default false."
                ),
            },
        },
    },
)

_BROWSER_DOWNLOAD_NEXT_SPEC = ToolSpec(
    name="browser_download_next",
    description=(
        "Arm a download listener, perform an action that triggers a "
        "download, and wait for the file to finish writing. Two-step: "
        "(1) call browser_download_next FIRST with a timeout — it "
        "returns immediately with a ticket; (2) trigger the download "
        "(typically browser_click on the download link); the next time "
        "you call browser_download_next with the same ticket id, it "
        "returns the saved file path. Or pass ``and_then`` with a "
        "selector to click in the same call (one-shot mode)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "and_then_click": {
                "type": "string",
                "description": "Selector to click immediately after arming.",
            },
            "timeout_ms": {"type": "integer"},
            "save_dir": {
                "type": "string",
                "description": "Override default ~/.xmclaw/v2/downloads/.",
            },
        },
    },
)


# ── module ────────────────────────────────────────────────────────

class BrowserTools(ToolProvider):
    """Per-session Playwright browser wrapper.

    Parameters
    ----------
    allowed_hosts
        If non-empty, navigation refuses any host not in the set.
    headless
        True by default. Set False for local debugging; will open a
        visible Chromium window.
    timeout_ms
        Default timeout for navigation/click/fill/eval actions.
    """

    def __init__(
        self,
        allowed_hosts: list[str] | None = None,
        *,
        headless: bool = True,
        timeout_ms: int = 15_000,
    ) -> None:
        self._allowed = set(allowed_hosts) if allowed_hosts else None
        # ``headless`` is now the DEFAULT for sessions that don't pass
        # ``visible`` explicitly. Sessions request visible mode on
        # first browser_open; once chosen, the session sticks with it.
        self._default_headless = headless
        self._timeout_ms = timeout_ms
        # Shared across sessions -- Playwright / browser are expensive
        # to start (~1s), cheap per-session context on top. Wave 23
        # split: maintain TWO long-lived browsers (one headless, one
        # headed) so we don't reload chromium just to flip visibility.
        # Headed only spins up the first time a session asks for it.
        self._playwright = None
        self._browser_headless: Any = None
        self._browser_headed: Any = None
        self._contexts: dict[str, Any] = {}   # session_id -> BrowserContext
        self._pages:    dict[str, Any] = {}   # session_id -> Page
        # Track which session is in which mode so callers don't have
        # to re-pass ``visible`` on every browser_* call.
        self._session_headless: dict[str, bool] = {}
        # Wave 25.2: per-session pending storage_state path. If set
        # when _page_for first creates a context, the file is read +
        # passed to new_context(storage_state=...).
        self._session_storage_state: dict[str, str] = {}
        # Wave 25.3: per-session console log buffer. Bounded list so
        # a noisy page doesn't grow unbounded — drops oldest first.
        self._console_buffers: dict[str, list[dict[str, Any]]] = {}
        # Wave 25.4: per-session pending download tickets, keyed by
        # ticket id. Each entry: {task, save_dir, timeout_ms,
        # armed_ts}. Task awaits page.wait_for_event('download').
        self._pending_downloads: dict[
            tuple[str, str], dict[str, Any],
        ] = {}
        # Guard the lazy init with a lock to avoid multiple concurrent
        # tool calls trying to spin up the browser at once.
        self._boot_lock = asyncio.Lock()

    def list_tools(self) -> list[ToolSpec]:
        # These always appear in the spec -- the model shouldn't have
        # to guess whether browser is enabled. If playwright is missing
        # the tool returns a structured install-me error when called,
        # which is much friendlier than "unknown tool".
        return [
            _BROWSER_OPEN_SPEC, _BROWSER_CLICK_SPEC, _BROWSER_PRESS_SPEC,
            _BROWSER_FILL_SPEC, _BROWSER_HOVER_SPEC,
            _BROWSER_SCROLL_SPEC, _BROWSER_SELECT_OPTION_SPEC,
            _BROWSER_UPLOAD_SPEC, _BROWSER_WAIT_FOR_SPEC,
            _BROWSER_BACK_SPEC, _BROWSER_FORWARD_SPEC,
            _BROWSER_RELOAD_SPEC,
            _BROWSER_TABS_SPEC, _BROWSER_TAB_SWITCH_SPEC,
            _BROWSER_TAB_CLOSE_SPEC, _BROWSER_DOWNLOAD_NEXT_SPEC,
            _BROWSER_SAVE_STATE_SPEC, _BROWSER_LIST_STATES_SPEC,
            _BROWSER_IMPORT_COOKIES_SPEC,
            _BROWSER_GET_CONSOLE_SPEC,
            _BROWSER_SCREENSHOT_SPEC,
            _BROWSER_SNAPSHOT_SPEC, _BROWSER_EVAL_SPEC, _BROWSER_CLOSE_SPEC,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "browser_open":
                return await self._open(call, t0)
            if call.name == "browser_click":
                return await self._click(call, t0)
            if call.name == "browser_press":
                return await self._press(call, t0)
            if call.name == "browser_fill":
                return await self._fill(call, t0)
            if call.name == "browser_screenshot":
                return await self._screenshot(call, t0)
            if call.name == "browser_snapshot":
                return await self._snapshot(call, t0)
            if call.name == "browser_eval":
                return await self._eval(call, t0)
            if call.name == "browser_close":
                return await self._close(call, t0)
            if call.name == "browser_hover":
                return await self._hover(call, t0)
            if call.name == "browser_scroll":
                return await self._scroll(call, t0)
            if call.name == "browser_select_option":
                return await self._select_option(call, t0)
            if call.name == "browser_upload":
                return await self._upload(call, t0)
            if call.name == "browser_wait_for":
                return await self._wait_for(call, t0)
            if call.name == "browser_back":
                return await self._history_nav(call, t0, "back")
            if call.name == "browser_forward":
                return await self._history_nav(call, t0, "forward")
            if call.name == "browser_reload":
                return await self._history_nav(call, t0, "reload")
            if call.name == "browser_tabs":
                return await self._tabs_list(call, t0)
            if call.name == "browser_tab_switch":
                return await self._tab_switch(call, t0)
            if call.name == "browser_tab_close":
                return await self._tab_close(call, t0)
            if call.name == "browser_download_next":
                return await self._download_next(call, t0)
            if call.name == "browser_save_state":
                return await self._save_state(call, t0)
            if call.name == "browser_list_states":
                return await self._list_states(call, t0)
            if call.name == "browser_import_cookies":
                return await self._import_cookies(call, t0)
            if call.name == "browser_get_console":
                return await self._get_console(call, t0)
            return _fail(call, t0, f"unknown tool: {call.name!r}")
        except _PlaywrightMissing as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

    async def close_session(self, session_id: str) -> None:
        """Tear down a session's page + context. Safe to call repeatedly."""
        page = self._pages.pop(session_id, None)
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001,S110
                pass
        ctx = self._contexts.pop(session_id, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001,S110
                pass
        # Wave 23: also forget the pinned visibility so a re-open
        # after close can choose a fresh mode.
        self._session_headless.pop(session_id, None)
        # Wave 25.2 / 25.3 / 25.4: drop per-session state.
        self._session_storage_state.pop(session_id, None)
        self._console_buffers.pop(session_id, None)
        # Cancel any pending download tasks tied to this session.
        for key in list(self._pending_downloads):
            if key[0] == session_id:
                entry = self._pending_downloads.pop(key)
                try:
                    entry["task"].cancel()
                except Exception:  # noqa: BLE001
                    pass

    async def shutdown(self) -> None:
        """Close every session + both browsers. For daemon shutdown."""
        for sid in list(self._contexts):
            await self.close_session(sid)
        for attr in ("_browser_headless", "_browser_headed"):
            b = getattr(self, attr, None)
            if b is not None:
                try:
                    await b.close()
                except Exception:  # noqa: BLE001,S110
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001,S110
                pass
            self._playwright = None

    # ── internals ──────────────────────────────────────────────────

    async def _ensure_playwright(self, headless: bool):
        """Lazy import + boot. Raises _PlaywrightMissing if not installed.
        Spins up either the headless or headed browser on demand —
        keeping each cached for subsequent calls."""
        attr = "_browser_headless" if headless else "_browser_headed"
        if getattr(self, attr) is not None:
            return
        async with self._boot_lock:
            if getattr(self, attr) is not None:
                return
            if self._playwright is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise _PlaywrightMissing(
                        "playwright not installed -- run "
                        "`pip install xmclaw[browser]` then `playwright install chromium`"
                    ) from exc
                self._playwright = await async_playwright().start()
            # Wave 24 stealth defaults — sites have UA blacklists that
            # match "HeadlessChrome" and pop "your browser is too old"
            # banners (Chromium 145 is current — it's pattern matching,
            # not a real version check). Drop the AutomationControlled
            # blink feature so navigator.webdriver isn't set, and we
            # override UA per-context so the stamp matches normal Chrome.
            browser = await self._playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            setattr(self, attr, browser)

    def _browser_for_session(self, session_id: str) -> Any:
        """Pick the right browser handle based on the session's pinned
        visibility mode (set at first browser_open)."""
        # Default to constructor's setting when session hasn't been
        # opened yet (covers code paths that touch _page_for before
        # browser_open — shouldn't happen, but guarded).
        headless = self._session_headless.get(
            session_id, self._default_headless,
        )
        return (
            self._browser_headless if headless else self._browser_headed
        )

    async def _page_for(
        self, session_id: str, *, headless: bool | None = None,
    ):
        # Resolve which mode this session is in. The first call with
        # an explicit ``headless`` pins the session; subsequent calls
        # ignore the argument so the agent doesn't have to thread it.
        if session_id not in self._session_headless:
            self._session_headless[session_id] = (
                self._default_headless if headless is None else headless
            )
        pinned = self._session_headless[session_id]
        await self._ensure_playwright(pinned)
        page = self._pages.get(session_id)
        if page is not None and not page.is_closed():
            return page
        ctx = self._contexts.get(session_id)
        if ctx is None:
            browser = self._browser_for_session(session_id)
            # Wave 24 stealth: real Chrome UA (drops "HeadlessChrome"
            # tag that sites pattern-match into "browser too old"
            # warnings) + accept_downloads=True so browser_download_next
            # has something to capture; the daemon still gates where
            # files land via save_dir kwarg.
            # Wave 25.2: if the session pre-loaded a storage profile
            # via browser_open(load_state=...), pass the path so the
            # new context hydrates cookies + localStorage.
            ctx_kwargs: dict[str, Any] = {
                "accept_downloads": True,
                "viewport": {"width": 1280, "height": 800},
                "user_agent": _REAL_CHROME_UA,
            }
            state_path = self._session_storage_state.get(session_id)
            if state_path:
                ctx_kwargs["storage_state"] = state_path
            ctx = await browser.new_context(**ctx_kwargs)
            # Hide automation traces from window/navigator probes — most
            # bot detectors look at navigator.webdriver, window.chrome,
            # the missing plugins array, etc.
            await ctx.add_init_script(_STEALTH_SCRIPT)
            ctx.set_default_timeout(self._timeout_ms)
            self._contexts[session_id] = ctx
        page = await ctx.new_page()
        # Wave 25.3: attach console + pageerror listeners to every new
        # page so the agent can query the buffer later via
        # browser_get_console. Idempotent — Playwright dedups handlers.
        self._attach_console_listeners(session_id, page)
        self._pages[session_id] = page
        return page

    def _attach_console_listeners(
        self, session_id: str, page: Any,
    ) -> None:
        """Wave 25.3: register console + pageerror event handlers and
        funnel into the per-session bounded buffer."""
        buf = self._console_buffers.setdefault(session_id, [])
        cap = 200

        def _on_console(msg: Any) -> None:
            try:
                buf.append({
                    "type": "console",
                    "level": getattr(msg, "type", "log") or "log",
                    "text": (getattr(msg, "text", "") or "")[:1000],
                    "ts": time.time(),
                })
                while len(buf) > cap:
                    buf.pop(0)
            except Exception:  # noqa: BLE001
                pass

        def _on_pageerror(err: Any) -> None:
            try:
                buf.append({
                    "type": "pageerror",
                    "level": "error",
                    "text": str(err)[:1000],
                    "ts": time.time(),
                })
                while len(buf) > cap:
                    buf.pop(0)
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
        except Exception:  # noqa: BLE001
            pass

    def _check_host(self, url: str) -> None:
        if self._allowed is None:
            return
        host = urlparse(url).hostname or ""
        if host not in self._allowed:
            raise PermissionError(
                f"host {host!r} not in browser allowed_hosts={sorted(self._allowed)}"
            )

    # ── tool bodies ────────────────────────────────────────────────

    async def _open(self, call: ToolCall, t0: float) -> ToolResult:
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _fail(call, t0, "missing or empty 'url'")
        if not (url.startswith("http://") or url.startswith("https://")):
            return _fail(call, t0, f"url must start with http(s)://, got {url!r}")
        self._check_host(url)
        wait_until = call.args.get("wait_until") or "load"
        if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
            return _fail(call, t0, f"wait_until={wait_until!r} not supported")

        # Wave 23: ``visible: true`` flips this session to a real
        # window for the rest of its life. ``visible: false`` (or
        # omitted) keeps it headless. Only the FIRST browser_open in
        # a session decides; subsequent calls in the same session
        # ignore the flag to keep the pinned mode stable.
        sid = self._sid(call)
        visible_arg = call.args.get("visible")
        if isinstance(visible_arg, bool):
            headless = not visible_arg
        else:
            headless = None  # defer to default / pinned
        # Wave 25.2: pre-load saved storage state on first open of a
        # session. Ignored if the session already has a context (the
        # storage_state= kwarg only takes effect at context creation).
        load_state = call.args.get("load_state")
        if (
            isinstance(load_state, str)
            and load_state.strip()
            and sid not in self._contexts
        ):
            state_path = _state_profile_path(load_state.strip())
            if not state_path.exists():
                return _fail(
                    call, t0,
                    f"load_state: profile {load_state!r} not found at {state_path}",
                )
            self._session_storage_state[sid] = str(state_path)
        page = await self._page_for(sid, headless=headless)
        resp = await page.goto(url, wait_until=wait_until)
        final_url = page.url
        title = await page.title()
        status = resp.status if resp is not None else None
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "url": final_url,
                "title": title,
                "status": status,
                "visible": not self._session_headless.get(
                    sid, self._default_headless,
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _click(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        force = bool(call.args.get("force", False))
        wait_nav_ms = int(call.args.get("wait_for_navigation_ms", 2000))
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")

        url_before = page.url
        title_before = ""
        try:
            title_before = await page.title()
        except Exception:  # noqa: BLE001
            pass

        # Click with auto-wait. Playwright's locator API auto-waits for
        # the element to be visible + stable + receive events before
        # firing — much more reliable than the legacy ``page.click(sel)``
        # path which would race JS-rendered widgets. ``force`` skips the
        # actionability checks for the rare case where an overlay
        # blocks the hit-test but the click should still go through.
        # Wave 25.1: _resolve_locator handles iframe prefixes.
        try:
            locator = self._resolve_locator(page, sel)
            await locator.click(force=force)
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            # Try to give the agent useful diagnostics: how many
            # elements matched, was the page still loading, etc.
            count = None
            try:
                count = await page.locator(sel).count()
            except Exception:  # noqa: BLE001
                pass
            return _fail(
                call, t0,
                f"click failed: {type(exc).__name__}: {exc}"
                + (f" (matched {count} elements)" if count is not None else ""),
            )

        # Detect a navigation kicked off by the click. We don't strictly
        # need page.expect_navigation here — we just compare URLs after
        # a short settle window. If the click was a SPA route change
        # without a real network nav, page.wait_for_load_state still
        # gives JS frameworks a moment to render the new DOM.
        navigated = False
        new_url = url_before
        new_title = title_before
        if wait_nav_ms > 0:
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=wait_nav_ms,
                )
            except Exception:  # noqa: BLE001 — timeout is fine, just no nav
                pass
            try:
                new_url = page.url
                new_title = await page.title()
            except Exception:  # noqa: BLE001
                pass
        navigated = new_url != url_before

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "selector": sel,
                "url": new_url,
                "title": new_title,
                "navigated": navigated,
                "url_before": url_before,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _press(self, call: ToolCall, t0: float) -> ToolResult:
        key = call.args.get("key")
        if not isinstance(key, str) or not key.strip():
            return _fail(call, t0, "missing or empty 'key'")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        sel = call.args.get("selector")
        url_before = page.url
        try:
            if isinstance(sel, str) and sel:
                await self._resolve_locator(page, sel).press(key)
            else:
                await page.keyboard.press(key)
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"press failed: {type(exc).__name__}: {exc}",
            )
        # Same auto-wait posture as click — Enter often submits a form
        # which navigates.
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=2000,
            )
        except Exception:  # noqa: BLE001
            pass
        new_url = page.url
        try:
            new_title = await page.title()
        except Exception:  # noqa: BLE001
            new_title = ""
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "key": key,
                "selector": sel or None,
                "url": new_url,
                "title": new_title,
                "navigated": new_url != url_before,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _fill(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        val = call.args.get("value")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        if not isinstance(val, str):
            return _fail(call, t0, "'value' must be a string")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        # Wave 25.1: use _resolve_locator for iframe support; fall back
        # to legacy page.fill for plain selectors so tests that patch
        # _FakePage.fill keep working.
        try:
            if "frame_name=" in sel or "frame_url=" in sel or "frame_index=" in sel:
                await self._resolve_locator(page, sel).fill(val)
            else:
                await page.fill(sel, val)
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"fill failed: {type(exc).__name__}: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"filled {sel!r} with {len(val)} chars",
            side_effects=(), latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _screenshot(self, call: ToolCall, t0: float) -> ToolResult:
        full = bool(call.args.get("full_page", False))
        fmt = (call.args.get("format") or "png").lower()
        if fmt not in ("png", "jpeg"):
            return _fail(call, t0, f"format must be 'png' or 'jpeg', got {fmt!r}")
        quality = int(call.args.get("quality", 80))
        max_inline = int(call.args.get("max_inline_bytes", 512 * 1024))
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")

        shot_kwargs: dict[str, Any] = {"full_page": full, "type": fmt}
        if fmt == "jpeg":
            shot_kwargs["quality"] = max(1, min(100, quality))
        png_or_jpeg = await page.screenshot(**shot_kwargs)
        mime = "image/png" if fmt == "png" else "image/jpeg"
        b64 = base64.b64encode(png_or_jpeg).decode("ascii")
        inline_size = len(b64) + len(f"data:{mime};base64,")

        content: dict[str, Any] = {
            "mime": mime,
            "url": page.url,
            "bytes": len(png_or_jpeg),
            "full_page": full,
        }

        # Wave-27 fix-LAT10 (2026-05-17): ALWAYS spill the screenshot
        # bytes to disk so we can set ``metadata.attach_image`` —
        # which is what (a) hop_loop reads to inject the image into
        # the NEXT LLM turn's vision input AND (b) the chat UI reads
        # (via TOOL_INVOCATION_FINISHED.images → /api/v2/media/...)
        # to render the screenshot inline in the user's chat bubble.
        # Pre-fix the inline-data-url branch saved nothing to disk →
        # ``normalize_attachments`` read empty metadata → UI got
        # ``images=[]`` → user saw a green "ok" badge with no image
        # and got told "截图已生成但可能没显示出来". The data_url
        # is STILL inlined when small (LLM gets a copy in content
        # too — harmless redundancy), but the disk file is now the
        # source of truth for the UI render.
        from pathlib import Path as _Path

        from xmclaw.utils.paths import data_dir
        dest_dir = data_dir() / "v2" / "screenshots"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        ext = ".png" if fmt == "png" else ".jpg"
        out = dest_dir / f"shot_{int(time.time()*1000)}_{call.id[:8]}{ext}"
        try:
            out.write_bytes(png_or_jpeg)
            spill_ok = True
        except OSError:
            spill_ok = False
        side_effects: tuple[str, ...] = (
            (str(out),) if spill_ok else ()
        )

        if inline_size <= max_inline:
            content["data_url"] = f"data:{mime};base64,{b64}"
        if spill_ok:
            content["path"] = str(out)
        if inline_size > max_inline:
            content["truncated"] = True
            content["hint"] = (
                f"Screenshot was {inline_size} bytes inline — over the "
                f"{max_inline}-byte cap. Saved to {_Path(out).name} on "
                "disk; data_url omitted from this content. Set "
                "max_inline_bytes higher (or format=jpeg with "
                "quality<80) to inline it."
            )

        # B-VISION: ``attach_image`` is the universal handshake to
        # both the vision pipeline (hop_loop.py:1024 →
        # Message.images on next LLM call) and the chat UI media
        # renderer (hop_loop.py:739 normalize_attachments →
        # TOOL_INVOCATION_FINISHED.images → ToolMediaImages
        # component). When the disk spill failed (rare), skip the
        # attach so the UI doesn't 404 on a missing file.
        metadata: dict[str, Any] = {}
        if spill_ok:
            metadata["attach_image"] = str(out)

        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=side_effects,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata=metadata,
        )

    async def _snapshot(self, call: ToolCall, t0: float) -> ToolResult:
        max_chars = int(call.args.get("max_chars", 8000))
        max_links = int(call.args.get("max_links", 30))
        max_inputs = int(call.args.get("max_inputs", 20))
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        title = await page.title()
        # Pull visible innerText (simpler than the a11y tree; works
        # well enough for LLM reasoning over content).
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if text and len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        # Top N links.
        links = await page.evaluate(
            """(max) => {
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const label = (a.innerText || a.title || a.href || '').trim();
                    if (!label) continue;
                    out.push({label: label.slice(0, 120), href: a.href});
                    if (out.length >= max) break;
                }
                return out;
            }""", max_links,
        )
        # Wave 22: form inputs + buttons. Without these, the agent has
        # to guess selectors and burns hops on "selector not found".
        # Returns {kind, selector, name, type, placeholder, value, label}
        # for each so the LLM can craft a precise browser_fill /
        # browser_click without an extra eval round-trip. ``selector``
        # is a stable CSS path the agent can pass straight into our
        # other tools.
        inputs = await page.evaluate(
            """(max) => {
                const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^\\w-]/g, ''));
                const visible = (el) => {
                    if (!el.offsetParent && el.tagName !== 'BUTTON') return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    return true;
                };
                const buildSel = (el) => {
                    if (el.id) return `#${cssEscape(el.id)}`;
                    if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
                    if (el.getAttribute && el.getAttribute('data-testid'))
                        return `[data-testid="${el.getAttribute('data-testid')}"]`;
                    return el.tagName.toLowerCase();
                };
                const labelFor = (el) => {
                    if (el.labels && el.labels.length) return el.labels[0].innerText.slice(0, 60);
                    if (el.getAttribute && el.getAttribute('aria-label')) return el.getAttribute('aria-label').slice(0, 60);
                    return '';
                };
                const out = [];
                const seen = new Set();
                const push = (kind, el) => {
                    const sel = buildSel(el);
                    const key = kind + ':' + sel;
                    if (seen.has(key)) return;
                    seen.add(key);
                    if (!visible(el)) return;
                    out.push({
                        kind,
                        selector: sel,
                        name: el.name || null,
                        type: el.type || null,
                        placeholder: el.placeholder || null,
                        value: typeof el.value === 'string' ? el.value.slice(0, 120) : null,
                        label: labelFor(el),
                        text: kind === 'button' ? (el.innerText || el.value || '').slice(0, 60) : null,
                    });
                };
                for (const el of document.querySelectorAll('input, textarea, select')) {
                    push('input', el);
                    if (out.length >= max) break;
                }
                if (out.length < max) {
                    for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]')) {
                        push('button', el);
                        if (out.length >= max) break;
                    }
                }
                return out;
            }""", max_inputs,
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "url": page.url,
                "title": title,
                "text": text or "",
                "links": links or [],
                "inputs": inputs or [],
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _eval(self, call: ToolCall, t0: float) -> ToolResult:
        expr = call.args.get("expression")
        if not isinstance(expr, str) or not expr.strip():
            return _fail(call, t0, "missing or empty 'expression'")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        result = await page.evaluate(expr)
        # Playwright returns Python primitives already; coerce anything
        # weird into a string so the LLM sees something sane.
        try:
            json.dumps(result)
            safe = result
        except (TypeError, ValueError):
            safe = repr(result)
        return ToolResult(
            call_id=call.id, ok=True, content=safe,
            side_effects=(), latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _close(self, call: ToolCall, t0: float) -> ToolResult:
        await self.close_session(self._sid(call))
        return ToolResult(
            call_id=call.id, ok=True, content="browser session closed",
            side_effects=(), latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── Wave 24 handlers ──────────────────────────────────────────

    async def _hover(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        try:
            await self._resolve_locator(page, sel).hover()
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"hover failed: {type(exc).__name__}: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={"selector": sel, "url": page.url},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _scroll(self, call: ToolCall, t0: float) -> ToolResult:
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        to_sel = call.args.get("to_selector")
        if isinstance(to_sel, str) and to_sel:
            try:
                await self._resolve_locator(
                    page, to_sel,
                ).scroll_into_view_if_needed()
            except ValueError as exc:
                return _fail(call, t0, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"scroll-to-selector failed: {exc}")
            return ToolResult(
                call_id=call.id, ok=True,
                content={"mode": "to_selector", "selector": to_sel},
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        direction = (call.args.get("direction") or "down").lower()
        amount = int(call.args.get("amount", 800))
        if direction == "top":
            await page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)",
            )
        elif direction == "up":
            await page.mouse.wheel(0, -abs(amount))
        elif direction == "down":
            await page.mouse.wheel(0, abs(amount))
        else:
            return _fail(
                call, t0,
                f"direction must be up/down/top/bottom, got {direction!r}",
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content={"mode": "direction", "direction": direction, "amount": amount},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _select_option(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        val = call.args.get("value")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        if val is None:
            return _fail(call, t0, "missing 'value'")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        # Playwright's select_option accepts {label} / {value} / string —
        # easier on the agent to just try value-then-label by passing
        # both shapes when input is a string.
        if isinstance(val, str):
            try_args = [{"value": val}, {"label": val}]
            last_err: Exception | None = None
            chosen: list[str] = []
            for arg in try_args:
                try:
                    chosen = await self._resolve_locator(
                        page, sel,
                    ).select_option(arg)
                    last_err = None
                    break
                except ValueError as exc:
                    return _fail(call, t0, str(exc))
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            if last_err is not None:
                return _fail(
                    call, t0,
                    f"select_option failed for {val!r}: "
                    f"{type(last_err).__name__}: {last_err}",
                )
        elif isinstance(val, list):
            try:
                chosen = await self._resolve_locator(
                    page, sel,
                ).select_option(val)
            except ValueError as exc:
                return _fail(call, t0, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"select_option failed: {exc}")
        else:
            return _fail(call, t0, "'value' must be string or list")
        return ToolResult(
            call_id=call.id, ok=True,
            content={"selector": sel, "selected": chosen},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _upload(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        files = call.args.get("files")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list) or not files:
            return _fail(call, t0, "'files' must be a path or non-empty list")
        # Validate paths upfront so the agent gets a clear error instead
        # of a stack trace from Playwright.
        from pathlib import Path as _P
        bad = [f for f in files if not isinstance(f, str) or not _P(f).is_file()]
        if bad:
            return _fail(call, t0, f"files not found / not strings: {bad}")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        try:
            await self._resolve_locator(page, sel).set_input_files(files)
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"upload failed: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={"selector": sel, "files": files, "count": len(files)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _wait_for(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        state = (call.args.get("state") or "visible").lower()
        if state not in ("attached", "detached", "visible", "hidden"):
            return _fail(
                call, t0,
                f"state must be attached/detached/visible/hidden, got {state!r}",
            )
        timeout = int(call.args.get("timeout_ms", 10_000))
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        try:
            await self._resolve_locator(page, sel).wait_for(
                state=state, timeout=timeout,
            )
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"wait_for timed out / failed: {type(exc).__name__}: {exc}",
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content={"selector": sel, "state": state, "url": page.url},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _history_nav(
        self, call: ToolCall, t0: float, op: str,
    ) -> ToolResult:
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        url_before = page.url
        try:
            if op == "back":
                await page.go_back()
            elif op == "forward":
                await page.go_forward()
            else:
                await page.reload()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{op} failed: {exc}")
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=5_000,
            )
        except Exception:  # noqa: BLE001
            pass
        title = ""
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "op": op,
                "url_before": url_before,
                "url": page.url,
                "title": title,
                "navigated": page.url != url_before,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _tabs_list(self, call: ToolCall, t0: float) -> ToolResult:
        sid = self._sid(call)
        ctx = self._contexts.get(sid)
        if ctx is None:
            return _fail(call, t0, "no browser context — call browser_open first")
        pages = list(ctx.pages)
        active_page = self._pages.get(sid)
        rows: list[dict[str, Any]] = []
        for i, p in enumerate(pages):
            try:
                title = await p.title()
            except Exception:  # noqa: BLE001
                title = ""
            rows.append({
                "index": i,
                "url": p.url,
                "title": title,
                "active": p is active_page,
            })
        return ToolResult(
            call_id=call.id, ok=True,
            content={"tabs": rows, "count": len(rows)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _tab_switch(self, call: ToolCall, t0: float) -> ToolResult:
        idx = call.args.get("index")
        if not isinstance(idx, int):
            return _fail(call, t0, "missing or non-integer 'index'")
        sid = self._sid(call)
        ctx = self._contexts.get(sid)
        if ctx is None:
            return _fail(call, t0, "no browser context — call browser_open first")
        pages = list(ctx.pages)
        if idx < 0 or idx >= len(pages):
            return _fail(
                call, t0,
                f"index {idx} out of range (have {len(pages)} tabs)",
            )
        target = pages[idx]
        try:
            await target.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        self._pages[sid] = target
        title = ""
        try:
            title = await target.title()
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            call_id=call.id, ok=True,
            content={"index": idx, "url": target.url, "title": title},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _tab_close(self, call: ToolCall, t0: float) -> ToolResult:
        idx = call.args.get("index")
        if not isinstance(idx, int):
            return _fail(call, t0, "missing or non-integer 'index'")
        sid = self._sid(call)
        ctx = self._contexts.get(sid)
        if ctx is None:
            return _fail(call, t0, "no browser context — call browser_open first")
        pages = list(ctx.pages)
        if idx < 0 or idx >= len(pages):
            return _fail(
                call, t0,
                f"index {idx} out of range (have {len(pages)} tabs)",
            )
        target = pages[idx]
        was_active = target is self._pages.get(sid)
        try:
            await target.close()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"close failed: {exc}")
        if was_active:
            remaining = [p for p in ctx.pages if not p.is_closed()]
            self._pages[sid] = remaining[-1] if remaining else None  # type: ignore[assignment]
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "closed_index": idx,
                "was_active": was_active,
                "remaining": len(ctx.pages),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _download_next(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        timeout = int(call.args.get("timeout_ms", 30_000))
        save_dir_arg = call.args.get("save_dir")
        and_then_click = call.args.get("and_then_click")
        ticket = call.args.get("ticket")
        from pathlib import Path as _P
        if isinstance(save_dir_arg, str) and save_dir_arg.strip():
            save_dir = _P(save_dir_arg).expanduser()
        else:
            from xmclaw.utils.paths import data_dir
            save_dir = data_dir() / "v2" / "downloads"
        save_dir.mkdir(parents=True, exist_ok=True)
        sid = self._sid(call)
        page = await self._page_for(sid)
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")

        # Mode 1 — one-shot: arm + trigger + wait in a single call.
        if isinstance(and_then_click, str) and and_then_click.strip():
            try:
                async with page.expect_download(timeout=timeout) as dl_info:
                    await self._resolve_locator(
                        page, and_then_click,
                    ).click()
                dl = await dl_info.value
            except Exception as exc:  # noqa: BLE001
                return _fail(
                    call, t0,
                    f"download wait failed: {type(exc).__name__}: {exc}",
                )
            return await self._finish_download(call, t0, dl, save_dir)

        # Mode 2 — two-step ticketed (Wave 25.4):
        #   First call WITHOUT ticket: arm background task that awaits
        #   page.wait_for_event('download', timeout). Return ticket.
        #   Later call WITH ticket: check the task — if done, save +
        #   return path; if not done and caller's timeout=0, return
        #   "armed, not ready"; otherwise await up to timeout.
        if isinstance(ticket, str) and ticket.strip():
            key = (sid, ticket.strip())
            entry = self._pending_downloads.get(key)
            if entry is None:
                return _fail(call, t0, f"unknown ticket: {ticket!r}")
            task: asyncio.Task = entry["task"]
            if task.done():
                self._pending_downloads.pop(key, None)
                if task.cancelled():
                    return _fail(call, t0, "download task was cancelled")
                exc = task.exception()
                if exc is not None:
                    return _fail(
                        call, t0,
                        f"download wait failed: {type(exc).__name__}: {exc}",
                    )
                dl = task.result()
                return await self._finish_download(
                    call, t0, dl, entry["save_dir"],
                )
            # Not ready yet — caller asked for non-blocking poll?
            if timeout <= 0:
                return ToolResult(
                    call_id=call.id, ok=True,
                    content={
                        "ticket": ticket,
                        "status": "armed",
                        "armed_ms_ago": int(
                            (time.time() - entry["armed_ts"]) * 1000,
                        ),
                    },
                    side_effects=(),
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
            try:
                dl = await asyncio.wait_for(asyncio.shield(task), timeout / 1000.0)
            except asyncio.TimeoutError:
                return _fail(
                    call, t0,
                    f"download not ready after {timeout}ms — call again with ticket {ticket}",
                )
            self._pending_downloads.pop(key, None)
            return await self._finish_download(
                call, t0, dl, entry["save_dir"],
            )

        # Mode 3 — bare arm (no ticket, no click): spin up a background
        # task that listens for the next download, return a fresh ticket
        # the caller will use to collect.
        import uuid as _uuid
        ticket_new = _uuid.uuid4().hex[:16]

        async def _wait_for_dl() -> Any:
            return await page.wait_for_event(
                "download", timeout=timeout,
            )
        task = asyncio.create_task(
            _wait_for_dl(), name=f"browser-download-{ticket_new}",
        )
        self._pending_downloads[(sid, ticket_new)] = {
            "task": task,
            "save_dir": save_dir,
            "timeout_ms": timeout,
            "armed_ts": time.time(),
        }
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "ticket": ticket_new,
                "status": "armed",
                "timeout_ms": timeout,
                "hint": (
                    "Trigger the download now (browser_click on the link "
                    "or whatever), then call browser_download_next again "
                    f"with ticket='{ticket_new}' to collect the file."
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _finish_download(
        self, call: ToolCall, t0: float, dl: Any, save_dir: Any,
    ) -> ToolResult:
        suggested = dl.suggested_filename or f"download_{int(time.time())}"
        dest = save_dir / suggested
        try:
            await dl.save_as(str(dest))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"save_as failed: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(dest),
                "filename": suggested,
                "url": dl.url,
                "bytes": dest.stat().st_size if dest.exists() else 0,
            },
            side_effects=(str(dest),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _save_state(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        name = call.args.get("name")
        if not isinstance(name, str) or not name.strip():
            return _fail(call, t0, "missing or empty 'name'")
        try:
            path = _state_profile_path(name.strip())
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        sid = self._sid(call)
        ctx = self._contexts.get(sid)
        if ctx is None:
            return _fail(
                call, t0,
                "no browser context — call browser_open first",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await ctx.storage_state(path=str(path))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"storage_state save failed: {exc}")
        size = path.stat().st_size if path.exists() else 0
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "name": name,
                "path": str(path),
                "bytes": size,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _import_cookies(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Convert an external cookie export into a storage_state file
        that ``browser_open(load_state=name)`` can use.

        Accepts either an inline JSON blob (``cookies_json``) or a
        path on disk (``cookies_path``). Both can be either a full
        Playwright storage_state object ``{cookies: [...], origins:
        [...]}`` or a bare cookie array (what Chrome extensions like
        EditThisCookie produce). Normalises to the storage_state
        shape and writes to ``~/.xmclaw/v2/browser_state/<name>.json``.
        """
        import json as _json
        from pathlib import Path as _P

        name = call.args.get("name")
        if not isinstance(name, str) or not name.strip():
            return _fail(call, t0, "missing or empty 'name'")
        try:
            out_path = _state_profile_path(name.strip())
        except ValueError as exc:
            return _fail(call, t0, str(exc))

        cookies_json = call.args.get("cookies_json")
        cookies_path = call.args.get("cookies_path")
        if not cookies_json and not cookies_path:
            return _fail(
                call, t0,
                "supply either 'cookies_json' (inline) or "
                "'cookies_path' (file)",
            )

        # Read raw payload.
        if cookies_json:
            raw_text = cookies_json
        else:
            src = _P(cookies_path)
            if not src.is_file():
                return _fail(
                    call, t0,
                    f"cookies_path does not exist or is not a file: {src}",
                )
            try:
                raw_text = src.read_text(encoding="utf-8")
            except OSError as exc:
                return _fail(call, t0, f"read failed: {exc}")

        try:
            parsed = _json.loads(raw_text)
        except _json.JSONDecodeError as exc:
            return _fail(call, t0, f"input is not valid JSON: {exc}")

        # Normalise to storage_state shape.
        if isinstance(parsed, list):
            storage_state = {"cookies": parsed, "origins": []}
        elif isinstance(parsed, dict):
            if "cookies" in parsed:
                storage_state = {
                    "cookies": parsed.get("cookies") or [],
                    "origins": parsed.get("origins") or [],
                }
            else:
                return _fail(
                    call, t0,
                    "JSON object must have 'cookies' key, or pass a "
                    "JSON array of cookies directly",
                )
        else:
            return _fail(
                call, t0,
                "JSON must be either an array of cookies or an object "
                f"with 'cookies' key (got {type(parsed).__name__})",
            )

        cookies_list = storage_state["cookies"]
        if not isinstance(cookies_list, list):
            return _fail(call, t0, "'cookies' must be an array")
        # Light validation + normalisation of each cookie. Playwright
        # is strict about ``sameSite`` values: ``Strict`` / ``Lax`` /
        # ``None``. Chrome extensions often emit ``no_restriction`` /
        # ``lax`` (lowercase) / null — translate them.
        _SAMESITE = {
            "no_restriction": "None", "unspecified": "None",
            "none": "None", "lax": "Lax", "strict": "Strict",
            "None": "None", "Lax": "Lax", "Strict": "Strict",
        }
        normalised: list[dict[str, Any]] = []
        for c in cookies_list:
            if not isinstance(c, dict):
                continue
            if not c.get("name") or "value" not in c:
                continue
            # Playwright wants ``expires`` (number, seconds since
            # epoch). Chrome export uses ``expirationDate`` (float).
            exp = c.get("expires")
            if exp is None and "expirationDate" in c:
                try:
                    exp = float(c["expirationDate"])
                except (TypeError, ValueError):
                    exp = None
            row: dict[str, Any] = {
                "name": str(c["name"]),
                "value": str(c["value"]),
                "domain": str(c.get("domain") or ""),
                "path": str(c.get("path") or "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
            }
            if exp is not None:
                row["expires"] = float(exp)
            ss_raw = c.get("sameSite")
            if ss_raw is not None:
                row["sameSite"] = _SAMESITE.get(
                    str(ss_raw), "None",
                )
            normalised.append(row)
        storage_state["cookies"] = normalised

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            out_path.write_text(
                _json.dumps(storage_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            return _fail(call, t0, f"write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "name": name.strip(),
                "path": str(out_path),
                "cookie_count": len(normalised),
                "origins_count": len(storage_state["origins"]),
                "note": (
                    f"Saved. Next call browser_open(url=..., "
                    f"load_state={name.strip()!r}) to use these cookies."
                ),
            },
            side_effects=(str(out_path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _list_states(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        from pathlib import Path as _P

        from xmclaw.utils.paths import data_dir
        root = _P(data_dir() / "v2" / "browser_state")
        if not root.exists():
            return ToolResult(
                call_id=call.id, ok=True,
                content={"profiles": []},
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        rows: list[dict[str, Any]] = []
        for p in sorted(root.glob("*.json")):
            try:
                stat = p.stat()
                rows.append({
                    "name": p.stem,
                    "saved_ts": stat.st_mtime,
                    "size_bytes": stat.st_size,
                })
            except Exception:  # noqa: BLE001
                pass
        return ToolResult(
            call_id=call.id, ok=True,
            content={"profiles": rows, "count": len(rows)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _get_console(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        sid = self._sid(call)
        level = (call.args.get("level") or "all").lower()
        cap = max(1, min(int(call.args.get("max", 50)), 200))
        clear = bool(call.args.get("clear", False))
        buf = self._console_buffers.get(sid, [])
        rows = list(buf)
        if level != "all":
            rows = [r for r in rows if r["level"] == level]
        # Most recent first.
        rows.reverse()
        rows = rows[:cap]
        if clear and sid in self._console_buffers:
            # In-place clear — the page-attached handler closes over
            # the original list. Reassigning would orphan future emits
            # to a buffer nobody reads.
            self._console_buffers[sid].clear()
        return ToolResult(
            call_id=call.id, ok=True,
            content={"entries": rows, "count": len(rows)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _sid(self, call: ToolCall) -> str:
        return call.session_id or "_default"

    def _resolve_locator(self, page: Any, selector: str) -> Any:
        """Wave 25.1: parse the iframe-aware selector syntax.

        Three prefixes let agents reach into iframes without us having
        to expose a separate frame-handle API:

          ``frame_name=NAME>>INNER``  — iframe by ``name`` attribute
          ``frame_url=SUBSTRING>>INNER`` — iframe whose URL contains
              SUBSTRING (matches across redirect chains)
          ``frame_index=N>>INNER`` — iframe by 0-based position in
              ``page.frames`` (top frame is 0; first iframe is 1)

        Without a prefix, behaves like the legacy ``page.locator(sel)``
        path. All eight action verbs (click / press / fill / hover /
        scroll / select_option / upload / wait_for) call this helper
        so iframe traversal is uniform.

        Returns a Playwright locator. Raises ValueError if a prefix is
        present but the named frame can't be found — surfaces as a
        clean tool error instead of a generic timeout.
        """
        if "::" not in selector and ">>" not in selector:
            return page.locator(selector).first
        # Use ">>" as the separator. Frame prefix is the first token,
        # inner selector is everything after. Don't split on later >>
        # because those might appear inside the inner selector
        # (Playwright supports chained `>>` for compound matching).
        if ">>" not in selector:
            return page.locator(selector).first
        prefix, _, inner = selector.partition(">>")
        prefix = prefix.strip()
        inner = inner.strip()
        if not inner:
            raise ValueError(
                f"frame selector {selector!r} missing inner selector "
                "after '>>'"
            )
        if prefix.startswith("frame_name="):
            name = prefix[len("frame_name="):].strip()
            frame = next(
                (f for f in page.frames if getattr(f, "name", "") == name),
                None,
            )
            if frame is None:
                raise ValueError(
                    f"no iframe with name={name!r} (frames have names: "
                    f"{[getattr(f, 'name', '') for f in page.frames]})"
                )
            return frame.locator(inner).first
        if prefix.startswith("frame_url="):
            needle = prefix[len("frame_url="):].strip()
            frame = next(
                (f for f in page.frames if needle in (f.url or "")),
                None,
            )
            if frame is None:
                raise ValueError(
                    f"no iframe whose url contains {needle!r}"
                )
            return frame.locator(inner).first
        if prefix.startswith("frame_index="):
            try:
                idx = int(prefix[len("frame_index="):])
            except ValueError as exc:
                raise ValueError(
                    f"frame_index must be int, got "
                    f"{prefix[len('frame_index='):]!r}"
                ) from exc
            frames = list(page.frames)
            if idx < 0 or idx >= len(frames):
                raise ValueError(
                    f"frame_index={idx} out of range (have "
                    f"{len(frames)} frames)"
                )
            return frames[idx].locator(inner).first
        # Unknown prefix — treat the whole string as a normal selector
        # (Playwright's own chained-locator >> syntax for compound
        # matching, e.g. ``css=.row >> text=Submit``).
        return page.locator(selector).first


def _state_profile_path(name: str) -> Any:
    """Wave 25.2: resolve a storage_state profile name to a file path
    under ~/.xmclaw/v2/browser_state/. Sanitizes the name to prevent
    path traversal (the only allowed chars are alphanumeric / dash /
    underscore — agents that emit weird names get a clean error)."""
    import re
    from pathlib import Path as _P

    from xmclaw.utils.paths import data_dir
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(
            f"profile name {name!r} must match [A-Za-z0-9_-]+"
        )
    return _P(data_dir() / "v2" / "browser_state" / f"{name}.json")


class _PlaywrightMissing(RuntimeError):
    """Sentinel used to distinguish missing-optional-dep from other errors."""


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
