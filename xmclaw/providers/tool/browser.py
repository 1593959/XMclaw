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
        "Open a URL in the AGENT's Playwright Chromium browser. "
        "This is MY browser — I control it via browser_click / "
        "browser_fill / browser_screenshot / browser_snapshot / "
        "browser_eval.\n\n"
        "★ Two modes — pick by whether the USER needs to see the page:\n"
        "  • ``visible=true`` (DEFAULT) → real browser window opens. "
        "The user CAN see it. I operate in the background; if a QR code "
        "or login form appears, I tell the user 'browser is open, please "
        "scan/login'. After the user finishes, I continue operating in "
        "the SAME window. This is the NORMAL mode — use it unless you "
        "have a specific reason not to.\n"
        "  • ``visible=false`` → headless, NO window. Use ONLY for pure "
        "unattended background scraping where the user definitely does "
        "NOT need to interact (e.g. reading a public article, batch data "
        "extraction).\n\n"
        "★ Need the user's saved logins/cookies?\n"
        "Set ``use_system_chrome=true`` to drive the user's installed "
        "Chrome.exe (with their real cookies/bookmarks). "
        "Or use ``persistent_profile=true`` + ``profile_name=<name>`` "
        "to create a persistent profile that keeps logins across sessions.\n\n"
        "★ CONTRAST with ``open_in_user_browser``:\n"
        "That tool is FIRE-AND-FORGET — I cannot see or control the page "
        "after opening. Use it ONLY when I just want to hand the user a "
        "link and don't need to operate the page.\n\n"
        "Returns the final URL + title."
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
                    "Show a real browser window. **Default TRUE** — "
                    "the user is watching by default; only set false "
                    "for unattended background work (scraping etc). "
                    "Visible mode uses a separate browser process; "
                    "subsequent browser_* calls in the same session "
                    "keep using whichever mode the session started in."
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
                    "the same session ignore it. INCOMPATIBLE with "
                    "``persistent_profile`` (the persistent profile "
                    "already carries its own cookies)."
                ),
            },
            "persistent_profile": {
                "type": "boolean",
                "description": (
                    "Wave-27 fix-LAT14: use a Playwright "
                    "``launch_persistent_context`` against a real "
                    "Chrome profile directory under "
                    "~/.xmclaw/v2/browser_profiles/<profile_name>/"
                    "user-data. Persists cookies + localStorage + "
                    "extensions + autofill + history across daemon "
                    "restarts. Requires ``profile_name``. Default "
                    "false."
                ),
            },
            "profile_name": {
                "type": "string",
                "description": (
                    "Namespace for the persistent profile directory. "
                    "Alphanumeric + dashes + underscores. Default "
                    "'default'. Use distinct names (e.g. 'chaoxing', "
                    "'github', 'qq') so site-specific cookies stay "
                    "isolated. Only meaningful when "
                    "``persistent_profile=true``."
                ),
            },
            "use_system_chrome": {
                "type": "boolean",
                "description": (
                    "Drive the user's system-installed Chrome "
                    "(channel='chrome') instead of Playwright's "
                    "bundled Chromium. Get the user's actual Chrome "
                    "extensions / version. Requires Chrome installed "
                    "on the host. Default false."
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
        "browser_snapshot again.\n\n"
        "★ When the selector matches MULTIPLE elements (common with "
        "``text=...`` against a page that has both a hidden modal AND "
        "a visible button with the same label), this tool **picks the "
        "first VISIBLE match by default** rather than blindly using "
        ".first(). Set ``visible_only=false`` to revert to literal "
        ".first(). Use ``nth=N`` selector prefix when you need a "
        "specific zero-indexed match regardless of visibility."
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
            "visible_only": {
                "type": "boolean",
                "description": (
                    "When the selector matches multiple elements, pick "
                    "the first one that is actually VISIBLE (not "
                    "display:none / off-screen / opacity:0 / behind "
                    "overlay). Default true — this fixes the common "
                    "case where a hidden modal element shadowed the "
                    "real visible button. Set false to keep the legacy "
                    ".first() behaviour."
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
        "are opt-in via ``full_page=true``.\n\n"
        "★ 2026-05-28 P1.4: pass ``annotate=true`` to overlay [N] "
        "labels at every ref'd element's bounding box (ref numbers "
        "match the last browser_snapshot). Vision-capable models can "
        "then say 'click 5' off the image directly. Falls back to a "
        "plain screenshot if PIL isn't installed."
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
            "annotate": {
                "type": "boolean",
                "description": (
                    "Overlay [N] ref labels from the last "
                    "browser_snapshot. Default false."
                ),
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


_BROWSER_DIALOG_ARM_SPEC = ToolSpec(
    name="browser_dialog_arm",
    description=(
        "Pre-arm the dialog handler: the NEXT dialog on this session "
        "is auto-resolved with the armed action and the arm "
        "self-clears. Useful when an action you're about to trigger "
        "is known to pop a confirm() and you don't want a round-trip "
        "through browser_snapshot + browser_dialog.\n\n"
        "Pass action='clear' to drop a pending arm without firing."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss", "clear"],
            },
            "text": {
                "type": "string",
                "description": (
                    "If action='accept' and the dialog turns out to be "
                    "a prompt(), this text is submitted as the answer."
                ),
            },
        },
        "required": ["action"],
    },
)


_BROWSER_NETWORK_LOG_SPEC = ToolSpec(
    name="browser_network_log",
    description=(
        "Return recent network activity on this session's page. "
        "**Each new page auto-captures requests + responses** into a "
        "bounded ring buffer (200 entries); call this tool to read "
        "them back, optionally filtered. With ``with_body=true`` "
        "the response body bytes are returned for matching entries "
        "— the canonical way to scrape an API response WITHOUT "
        "re-issuing the request via browser_eval(fetch(...)).\n\n"
        "★ 2026-05-28 P3.5: response-body capture tool.\n"
        "Filtering: ``url_glob`` accepts shell-glob patterns (e.g. "
        "``**/api/login`` or ``*.json``). Default: return all "
        "entries in the buffer.\n"
        "Output: list of {method, url, status, request_headers, "
        "response_headers, ts, body?}."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url_glob": {
                "type": "string",
                "description": "Filter entries by URL glob (e.g. **/api/*).",
            },
            "method": {
                "type": "string",
                "description": "Filter by HTTP method (GET/POST/...).",
            },
            "status_min": {
                "type": "integer",
                "description": "Only entries with status >= this. Default 0.",
            },
            "with_body": {
                "type": "boolean",
                "description": (
                    "Include response body. Body is capped at 64 KB "
                    "per entry. Default false."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned. Default 20.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the buffer after reading. Default false.",
            },
        },
    },
)


_BROWSER_CLICK_REF_SPEC = ToolSpec(
    name="browser_click_ref",
    description=(
        "Click an element by its [N] ref number from the last "
        "browser_snapshot. **Preferred over browser_click(selector)** "
        "— refs are dead-simple ('click 5') vs CSS selectors which "
        "LLMs commonly mistype. Refs are valid until the next "
        "browser_snapshot or any navigation; if a ref is stale you'll "
        "get a clear 'ref not found' error and should re-snapshot.\n\n"
        "Usage: ``browser_snapshot`` → see ``[3] button \"Login\"`` → "
        "``browser_click_ref(ref=3)``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "ref": {
                "type": "integer",
                "description": "Ref number from the last snapshot.",
            },
            "wait_for_navigation_ms": {
                "type": "integer",
                "description": (
                    "Settle window after click for SPA route changes "
                    "or full nav. Default 2000."
                ),
            },
        },
        "required": ["ref"],
    },
)


_BROWSER_TYPE_REF_SPEC = ToolSpec(
    name="browser_type_ref",
    description=(
        "Type text into an element by its [N] ref number from the "
        "last browser_snapshot. Equivalent to browser_fill but "
        "reference-based. Preferred for the same reason as "
        "browser_click_ref: no CSS-selector reasoning required."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "ref": {
                "type": "integer",
                "description": "Ref number from the last snapshot.",
            },
            "text": {
                "type": "string",
                "description": "Text to type.",
            },
            "submit": {
                "type": "boolean",
                "description": (
                    "If true, press Enter after typing. Useful for "
                    "search bars / single-field login forms. "
                    "Default false."
                ),
            },
        },
        "required": ["ref", "text"],
    },
)


_BROWSER_DIALOG_SPEC = ToolSpec(
    name="browser_dialog",
    description=(
        "Respond to a JS dialog (alert / confirm / prompt / "
        "beforeunload) that's blocking the page. Without this tool, "
        "clicks on a confirm() can stall the page indefinitely.\n\n"
        "Workflow: browser_snapshot's ``pending_dialogs`` field "
        "shows blocking dialogs; pick the ``id``, call "
        "``browser_dialog(id=..., action='accept'|'dismiss'|"
        "'respond', text='...')``. ``respond`` is only valid for "
        "prompt() dialogs (where ``text`` becomes the input)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Dialog id from snapshot's pending_dialogs. "
                    "Omit to act on the oldest pending dialog."
                ),
            },
            "action": {
                "type": "string",
                "description": "'accept' | 'dismiss' | 'respond'.",
                "enum": ["accept", "dismiss", "respond"],
            },
            "text": {
                "type": "string",
                "description": (
                    "For action='respond' on a prompt() dialog: "
                    "the text to submit. Ignored otherwise."
                ),
            },
        },
        "required": ["action"],
    },
)


_BROWSER_USE_MY_BROWSER_SPEC = ToolSpec(
    name="browser_use_my_browser",
    description=(
        "[DEPRECATED — use browser_open(use_system_chrome=true) instead] "
        "Open a URL in the user's real Chrome with AGENT control. "
        "This tool's functionality is fully covered by browser_open "
        "with use_system_chrome=true — use that instead."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full http(s) URL to navigate to.",
            },
            "browser": {
                "type": "string",
                "description": (
                    "Which browser family. 'auto' (default) picks the "
                    "first detected: chrome → edge → brave. Use a "
                    "specific name to pin."
                ),
                "enum": ["auto", "chrome", "edge", "brave"],
            },
            "profile": {
                "type": "string",
                "description": (
                    "Chrome profile directory name. 'Default' is the "
                    "main one most users have. Other examples: "
                    "'Profile 1', 'Profile 2' (work / personal splits). "
                    "Only used in launch modes — CDP-attach uses "
                    "whatever profile is already running."
                ),
            },
            "wait_until": {
                "type": "string",
                "description": "'load' | 'domcontentloaded' | 'networkidle'. Default 'load'.",
            },
        },
        "required": ["url"],
    },
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
        "Wait until one or more page-state conditions hold. **All** "
        "supplied conditions must be satisfied before this returns "
        "(logical AND). Specify at least one — selector / url_glob / "
        "load_state / js_predicate.\n\n"
        "★ 2026-05-28 P3.7: composite wait. One "
        "call can replace 'wait for selector → wait for URL change "
        "→ wait for network idle → wait for JS flag' chains.\n\n"
        "Conditions:\n"
        "  - selector + state: wait until DOM element matches "
        "(attached/detached/visible/hidden, default visible).\n"
        "  - url_glob: wait until ``page.url`` matches the glob "
        "(supports ``*`` and ``**``).\n"
        "  - load_state: 'load' | 'domcontentloaded' | "
        "'networkidle' — wait until the page reaches that state.\n"
        "  - js_predicate: JS expression that evaluates to truthy "
        "when ready (polled, not a one-shot eval). Example: "
        "``window.app && window.app.ready === true``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "state": {"type": "string"},
            "url_glob": {
                "type": "string",
                "description": "Glob (* / **) the page URL must match.",
            },
            "load_state": {
                "type": "string",
                "description": "'load' | 'domcontentloaded' | 'networkidle'.",
            },
            "js_predicate": {
                "type": "string",
                "description": "Truthy-when-ready JS expression.",
            },
            "timeout_ms": {"type": "integer"},
        },
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
        headless: bool = False,
        timeout_ms: int = 15_000,
        evaluate_enabled: bool = True,
    ) -> None:
        self._allowed = set(allowed_hosts) if allowed_hosts else None
        # 2026-05-28 P2.8: gate on arbitrary JS execution. When False,
        # browser_eval returns a structured refusal instead of running
        # the agent's expression. Matches the reference's
        # ``evaluateEnabled=true`` config flag — useful for audit /
        # untrusted-skill scenarios where the agent driving the
        # browser shouldn't have arbitrary-JS reach.
        self._evaluate_enabled = bool(evaluate_enabled)
        # ``headless`` is the DEFAULT for sessions that don't pass
        # ``visible`` explicitly. Sessions request visibility on first
        # browser_open; once chosen, the session sticks with it.
        # 2026-05-28: default flipped from True → False. The user is
        # watching the chat — they want to see the agent operate, not
        # have it scrape silently in the background. Background-only
        # tasks must opt into headless by passing visible=false.
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
        # Wave-27 fix-LAT14 (2026-05-17): per-profile-name persistent
        # browser context, opened via Playwright's
        # ``launch_persistent_context(user_data_dir=...)``. Mirrors
        # the reference's design (extensions/browser/src/browser/chrome.ts
        # in comparable agents): one independent Chrome profile per
        # ``profile_name`` under
        # ~/.xmclaw/v2/browser_profiles/<name>/user-data. Persists
        # cookies / localStorage / Chrome-side extensions / saved
        # passwords / autofill / history across daemon restarts —
        # everything a real Chrome profile carries, not just the
        # cookies+localStorage that storage_state JSON captures.
        # Multiple sessions targeting the same profile_name share
        # ONE context (Playwright + Chromium both forbid multiple
        # processes opening the same user_data_dir).
        self._persistent_contexts: dict[str, Any] = {}   # profile_name -> BrowserContext
        self._session_persistent_profile: dict[str, str] = {}  # sid -> profile_name
        # Wave-27 fix-LAT14b: per-session chrome channel preference
        # (set when ``use_system_chrome=True`` on browser_open). Eager
        # init so _ensure_persistent_context can read it without a
        # getattr-default dance.
        self._session_persistent_chrome_channel: dict[str, str | None] = {}
        # 2026-05-28: user-CDP attach. Third browser path beside the
        # headless / headed-clean-profile pair: connect into the USER'S
        # real Chrome (their cookies / logins / extensions / bookmarks)
        # so login-walled sites just work and the user watches their
        # familiar browser drive itself. See ``browser_use_my_browser``
        # tool + ``_user_browser_detect`` module.
        #
        # ``_user_browser`` is the Playwright Browser/Context handle
        # (cached process-wide — connecting to a CDP endpoint is ~50ms
        # but creating a context once is fine). ``_session_user_cdp``
        # is a per-session bool pinning the session to this path; once
        # set, ``_page_for`` routes through user-CDP rather than the
        # standard headless/headed launch.
        self._user_browser_context: Any = None
        self._user_browser_handle: Any = None  # only set in CDP-attach mode
        self._session_user_cdp: dict[str, bool] = {}
        # 2026-05-28 P0.1: per-session [N] ref system. browser_snapshot
        # assigns sequential refs to every interactive element and
        # stores ``{selector, bbox, kind, label}`` per ref here. The
        # next browser_click_ref(n) / browser_type_ref(n, text) call
        # resolves via this map — agent never has to handcraft CSS
        # selectors. Follows the reference's ``aria-ref`` system + the reference's
        # element-numbering snapshot. Refs are scoped to the page
        # current at snapshot time; ``_open`` clears the map (new
        # page = new refs) and each snapshot rebuilds it.
        self._session_refs: dict[str, dict[int, dict[str, Any]]] = {}
        # 2026-05-28 P0.2: dialog supervisor. Per-session pending +
        # recent JS dialog records (alert / confirm / prompt /
        # beforeunload). page.on('dialog') populates these; the
        # browser_dialog tool resolves pending ones; browser_snapshot
        # surfaces both lists so the agent SEES the dialog instead
        # of hanging on a blocked page. ``recent`` is a ring-buffer
        # capped at 20 entries per session.
        self._session_dialogs_pending: dict[str, list[dict[str, Any]]] = {}
        self._session_dialogs_recent: dict[str, list[dict[str, Any]]] = {}
        # Pending dialog objects await resolution; the live Playwright
        # ``Dialog`` handle lives here, keyed by ``(sid, dialog_id)``.
        # Removed once accepted/dismissed.
        self._dialog_handles: dict[tuple[str, str], Any] = {}
        # 2026-05-28 P2.4: dialog pre-arm. If set, the NEXT dialog on
        # this session is auto-resolved with the armed action and the
        # arm is cleared. Set via browser_dialog_arm.
        self._session_dialog_armed: dict[str, dict[str, Any]] = {}
        # 2026-05-28 P3.5: per-session network log. ``page.on('request')``
        # appends a stub; ``page.on('response')`` fills in status +
        # response headers. Buffer is bounded — ring-buffer pop on
        # overflow. Body capture is opt-in per browser_network_log
        # call to avoid storing huge payloads we don't need.
        self._network_buffers: dict[str, list[dict[str, Any]]] = {}
        self._network_buffer_cap = 200
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
            _BROWSER_USE_MY_BROWSER_SPEC,
            # 2026-05-28 P0.1 + P0.2: ref-based action tools +
            # dialog supervisor — major LLM accuracy wins.
            _BROWSER_CLICK_REF_SPEC, _BROWSER_TYPE_REF_SPEC,
            _BROWSER_DIALOG_SPEC,
            # 2026-05-28 P2.4 + P3.5: dialog pre-arm + network log.
            _BROWSER_DIALOG_ARM_SPEC, _BROWSER_NETWORK_LOG_SPEC,
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
            if call.name == "browser_use_my_browser":
                return await self._use_my_browser(call, t0)
            if call.name == "browser_click_ref":
                return await self._click_ref(call, t0)
            if call.name == "browser_type_ref":
                return await self._type_ref(call, t0)
            if call.name == "browser_dialog":
                return await self._dialog(call, t0)
            if call.name == "browser_dialog_arm":
                return await self._dialog_arm(call, t0)
            if call.name == "browser_network_log":
                return await self._network_log(call, t0)
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
            return _fail_with_hint(
                call, t0, "playwright not installed",
                exc=exc,
                hint=(
                    "the browser_* tools need playwright + a chromium "
                    "binary. Install via ``pip install 'xmclaw[browser]'`` "
                    "then ``playwright install chromium`` (one-time, "
                    "~150MB). Until installed, no browser_* tool will "
                    "be invocable."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # Epic #27 sweep #16 (2026-05-19): browser failures span a
            # huge surface (network errors, selectors not matching, JS
            # crashes, navigation cancellations). The exception type
            # gives the LLM enough signal — we just add a generic
            # debugging hint pointing at the most-common recovery
            # path.
            return _fail_with_hint(
                call, t0,
                f"browser tool {call.name!r} raised",
                exc=exc,
                hint=(
                    "common recoveries: (1) call ``browser_snapshot`` "
                    "first to see the DOM state, (2) re-check the "
                    "selector if it's a click/fill/hover, (3) wait "
                    "with ``browser_wait_for`` before interacting, "
                    "(4) if the page is gone, ``browser_open`` it "
                    "again. Persistent crashes likely mean the "
                    "playwright child process died — close + reopen "
                    "the session."
                ),
            )

    async def close_session(self, session_id: str) -> None:
        """Tear down a session's page + context. Safe to call repeatedly."""
        page = self._pages.pop(session_id, None)
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001,S110
                pass
        # Wave-27 fix-LAT14b: persistent profile sessions share a
        # single BrowserContext across multiple session_ids. Closing
        # ctx here would kill the profile for EVERY other session
        # using the same profile_name AND skip the cookie-flush-to-
        # disk on shutdown. So: skip ctx.close() when this session
        # is persistent. The shared context lives until shutdown()
        # or an explicit profile-reset call.
        is_persistent = self._session_persistent_profile.pop(
            session_id, None,
        )
        # 2026-05-28: user-CDP sessions share the user's real browser
        # context. CLOSING THAT CONTEXT WOULD CLOSE THE USER'S CHROME
        # AND LOSE ALL THEIR TABS — explicit hard rule. Detach by
        # forgetting the per-session page only; the shared context
        # stays alive for other sessions / future
        # browser_use_my_browser calls.
        is_user_cdp = self._session_user_cdp.pop(session_id, False)
        ctx = self._contexts.pop(session_id, None)
        if ctx is not None and is_persistent is None and not is_user_cdp:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001,S110
                pass
        # Drop any cached per-session persistent-chrome channel hint.
        self._session_persistent_chrome_channel.pop(session_id, None)
        # Wave 23: also forget the pinned visibility so a re-open
        # after close can choose a fresh mode.
        self._session_headless.pop(session_id, None)
        # Wave 25.2 / 25.3 / 25.4: drop per-session state.
        self._session_storage_state.pop(session_id, None)
        self._console_buffers.pop(session_id, None)
        # 2026-05-28 P0.1 / P0.2 / P2.4 / P3.5: drop ref map +
        # dialog records + pre-arm + network log on session close.
        self._session_refs.pop(session_id, None)
        self._session_dialogs_pending.pop(session_id, None)
        self._session_dialogs_recent.pop(session_id, None)
        self._session_dialog_armed.pop(session_id, None)
        self._network_buffers.pop(session_id, None)
        # Forget any cached Dialog handles for this session.
        for k in list(self._dialog_handles):
            if k[0] == session_id:
                self._dialog_handles.pop(k, None)
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
        # Wave-27 fix-LAT14: also close persistent profile contexts.
        # Closing flushes pending cookie / localStorage writes to the
        # user-data-dir on disk so next daemon boot reads consistent
        # state. Best-effort: a hung context shouldn't block other
        # shutdown steps.
        for profile_name, ctx in list(self._persistent_contexts.items()):
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001,S110
                pass
        # 2026-05-28: user-CDP cleanup. Two cases:
        #   - cdp_attach: we hold a Browser handle; DETACH it (calling
        #     ``browser.close()`` on a CDP-attached Browser does NOT
        #     close the underlying Chrome — Playwright docs are
        #     explicit — it only tears down our websocket. Safe.)
        #   - launched_real_profile: we own the context. The user's
        #     Chrome IS our spawned process — closing it shuts that
        #     instance down, which IS what we want on daemon shutdown
        #     (otherwise the orphan Chrome lives past the daemon).
        #   - side_profile_fallback: ctx lives in _persistent_contexts
        #     already and got closed above.
        if self._user_browser_handle is not None:
            try:
                await self._user_browser_handle.close()
            except Exception:  # noqa: BLE001,S110
                pass
            self._user_browser_handle = None
            self._user_browser_context = None
        elif self._user_browser_context is not None:
            mode = getattr(
                self._user_browser_context,
                "_xmclaw_user_browser_mode", "",
            )
            if mode == "launched_real_profile":
                try:
                    await self._user_browser_context.close()
                except Exception:  # noqa: BLE001,S110
                    pass
            self._user_browser_context = None
        self._persistent_contexts.clear()
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
            # Visibility args — when headed, force the window into a
            # known foreground-friendly position and size. Without
            # these the daemon-launched Chromium often opens behind
            # the active window (Windows focus-stealing prevention
            # blocks SetForegroundWindow from background processes)
            # OR lands at 0,0 with viewport-only size, easily hidden
            # under the chat window. Symptom the user reported
            # 2026-05-28: "他说他打开了浏览器但我看不到".
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if not headless:
                launch_args.extend([
                    "--start-maximized",
                    "--new-window",
                ])
            browser = await self._playwright.chromium.launch(
                headless=headless,
                args=launch_args,
            )
            setattr(self, attr, browser)

    async def _ensure_persistent_context(
        self, profile_name: str, *, headless: bool, session_id: str,
    ) -> Any:
        """Wave-27 fix-LAT14: lazy-launch a persistent BrowserContext
        keyed on ``profile_name``. Follows the reference's chrome.ts launch
        pattern (spawn Chromium with persistent user-data-dir + CDP
        debug port), but uses Playwright's
        ``launch_persistent_context()`` which encapsulates that.

        Multiple sessions targeting the same profile_name share the
        same context — Chromium / Chrome forbid two processes opening
        the same user-data-dir simultaneously, so this isn't a choice.

        Channel selection: if the session asked for ``use_system_chrome``,
        we pass ``channel="chrome"`` to drive the user's installed
        Chrome.exe (gets their version + can install standard Chrome
        extensions). Otherwise Playwright's bundled Chromium is used
        (no system-Chrome dependency).
        """
        existing = self._persistent_contexts.get(profile_name)
        if existing is not None:
            # Wave-27 fix-LAT14b: liveness probe via ``.pages``.
            # Note: ``existing.browser`` returns None for persistent
            # contexts (Playwright contract), so the previous probe
            # never detected staleness. ``.pages`` is a property
            # backed by Playwright's internal channel state — it
            # raises after the context is closed, which is the
            # signal we want. Stale → drop + re-launch below.
            try:
                _probe = existing.pages
                _ = len(_probe)
                return existing
            except Exception:  # noqa: BLE001
                self._persistent_contexts.pop(profile_name, None)

        async with self._boot_lock:
            existing = self._persistent_contexts.get(profile_name)
            if existing is not None:
                return existing
            if self._playwright is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise _PlaywrightMissing(
                        "playwright not installed -- run "
                        "`pip install xmclaw[browser]` then "
                        "`playwright install chromium`"
                    ) from exc
                self._playwright = await async_playwright().start()

            user_data_dir = _persistent_profile_dir(profile_name)
            try:
                user_data_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"could not create persistent profile dir "
                    f"{user_data_dir}: {exc}"
                ) from exc

            channel_map = getattr(
                self, "_session_persistent_chrome_channel", {},
            )
            channel = channel_map.get(session_id)

            # Same visibility args as the non-persistent path —
            # headed mode needs the window forced into a
            # foreground-friendly state or the daemon-launched
            # Chrome lands behind everything. ``viewport`` is
            # intentionally None for headed persistent so
            # ``--start-maximized`` actually fills the screen;
            # leaving the viewport explicit pins the inner-page
            # size and defeats the maximize args.
            persistent_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if not headless:
                persistent_args.extend([
                    "--start-maximized",
                    "--new-window",
                ])
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(user_data_dir),
                "headless": headless,
                "accept_downloads": True,
                # Wave-27 fix-LAT14b: real Chrome UA to defeat the
                # "HeadlessChrome" pattern blacklists that show
                # "your browser is too old" banners. Same UA the
                # non-persistent path uses (line ~915). When
                # ``channel="chrome"`` the system Chrome's own UA
                # would also work; we override for parity so the
                # stealth behaviour doesn't silently regress when
                # the user switches between bundled Chromium and
                # system Chrome.
                "user_agent": _REAL_CHROME_UA,
                "args": persistent_args,
            }
            # Headless still needs a fixed viewport (no window manager
            # to maximize against); headed lets the window's actual
            # size dictate page viewport so --start-maximized works.
            if headless:
                launch_kwargs["viewport"] = {"width": 1280, "height": 800}
            else:
                launch_kwargs["no_viewport"] = True
            if channel:
                launch_kwargs["channel"] = channel

            ctx = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs,
            )
            await ctx.add_init_script(_STEALTH_SCRIPT)
            ctx.set_default_timeout(self._timeout_ms)
            self._persistent_contexts[profile_name] = ctx
            return ctx

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

        # 2026-05-28: user-CDP branch. If this session was opened via
        # browser_use_my_browser, route through the shared user
        # browser context (CDP attach OR launched user real profile
        # OR side-profile fallback — all three modes look the same
        # at this point: one BrowserContext we share + a per-session
        # Page).
        if self._session_user_cdp.get(session_id):
            ctx, _mode = await self._ensure_user_browser_context()
            page = self._pages.get(session_id)
            if page is not None and not page.is_closed():
                return page
            self._contexts[session_id] = ctx
            page = await ctx.new_page()
            self._attach_console_listeners(session_id, page)
            self._attach_dialog_listener(session_id, page)
            self._attach_network_listener(session_id, page)
            self._pages[session_id] = page
            return page

        # Wave-27 fix-LAT14: persistent profile branch. Sessions
        # tagged with a profile_name route through a
        # ``launch_persistent_context``-managed shared context keyed
        # on profile_name. Different sessions targeting the SAME
        # profile_name share the context (Chrome won't open a
        # user-data-dir twice).
        profile_name = self._session_persistent_profile.get(session_id)
        if profile_name is not None:
            ctx = await self._ensure_persistent_context(
                profile_name, headless=pinned, session_id=session_id,
            )
            page = self._pages.get(session_id)
            if page is not None and not page.is_closed():
                return page
            self._contexts[session_id] = ctx
            page = await ctx.new_page()
            self._attach_console_listeners(session_id, page)
            self._attach_dialog_listener(session_id, page)
            self._attach_network_listener(session_id, page)
            self._pages[session_id] = page
            return page

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
        self._attach_dialog_listener(session_id, page)
        self._attach_network_listener(session_id, page)
        self._pages[session_id] = page
        return page

    def _attach_network_listener(
        self, session_id: str, page: Any,
    ) -> None:
        """2026-05-28 P3.5: capture every request + response into a
        bounded per-session ring buffer. Read back via
        ``browser_network_log`` — agent doesn't have to re-issue
        fetches via browser_eval to inspect API responses.
        """
        buf = self._network_buffers.setdefault(session_id, [])
        cap = self._network_buffer_cap

        # Index by request id ((url, method, ts)) so the response
        # handler can patch in status + headers without scanning.
        # Playwright assigns a Request object identity; we use id().
        index: dict[int, dict[str, Any]] = {}

        def _on_request(request: Any) -> None:
            try:
                entry = {
                    "method": getattr(request, "method", ""),
                    "url": getattr(request, "url", ""),
                    "request_headers": (
                        dict(getattr(request, "headers", {}) or {})
                    ),
                    "ts": time.time(),
                    # Filled in by _on_response.
                    "status": None,
                    "response_headers": None,
                    "_request_obj": request,
                    "_response_obj": None,
                }
                buf.append(entry)
                while len(buf) > cap:
                    dropped = buf.pop(0)
                    index.pop(id(dropped["_request_obj"]), None)
                index[id(request)] = entry
            except Exception:  # noqa: BLE001
                pass

        def _on_response(response: Any) -> None:
            try:
                req = getattr(response, "request", None)
                entry = index.get(id(req)) if req is not None else None
                if entry is None:
                    return
                entry["status"] = getattr(response, "status", None)
                entry["response_headers"] = dict(
                    getattr(response, "headers", {}) or {},
                )
                entry["_response_obj"] = response
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("request", _on_request)
            page.on("response", _on_response)
        except Exception:  # noqa: BLE001
            pass

    def _attach_dialog_listener(
        self, session_id: str, page: Any,
    ) -> None:
        """2026-05-28 P0.2: Auto-capture JS dialogs on this page.

        ``page.on('dialog')`` fires when the page calls alert() /
        confirm() / prompt() / triggers beforeunload. Without a
        handler, Playwright auto-dismisses the dialog AND the agent
        never sees it happened. With our handler, we:

          1. Stash the live ``Dialog`` handle in
             ``_dialog_handles[(sid, id)]`` so ``browser_dialog`` can
             resolve it later.
          2. Append a record to ``_session_dialogs_pending`` so
             ``browser_snapshot`` surfaces it to the agent.
          3. Do NOT auto-accept/dismiss — the policy is "agent must
             respond" (matches the upstream agent ``must_respond``). The page
             stays blocked until ``browser_dialog`` resolves it.
        """
        pending = self._session_dialogs_pending.setdefault(session_id, [])

        def _on_dialog(dialog: Any) -> None:
            try:
                # 2026-05-28 P2.4: pre-arm. If browser_dialog_arm was
                # called before this dialog fired, auto-resolve and
                # self-clear the arm — agent doesn't need a snapshot
                # / dialog round-trip per modal.
                armed = self._session_dialog_armed.pop(session_id, None)
                if armed is not None:
                    action = armed.get("action")
                    text = armed.get("text") or ""
                    if action == "accept":
                        coro = (
                            dialog.accept(text)
                            if getattr(dialog, "type", "") == "prompt"
                            else dialog.accept()
                        )
                        asyncio.ensure_future(coro)
                    elif action == "dismiss":
                        asyncio.ensure_future(dialog.dismiss())
                    # Record in recent for visibility.
                    rec = {
                        "id": "armed_" + str(int(time.time())),
                        "type": getattr(dialog, "type", "unknown"),
                        "message": (
                            getattr(dialog, "message", "") or ""
                        )[:500],
                        "resolved_action": f"pre_armed_{action}",
                        "resolved_ts": time.time(),
                    }
                    recent = self._session_dialogs_recent.setdefault(
                        session_id, [],
                    )
                    recent.append(rec)
                    while len(recent) > 20:
                        recent.pop(0)
                    return

                import uuid as _uuid
                did = _uuid.uuid4().hex[:8]
                record = {
                    "id": did,
                    "type": getattr(dialog, "type", "unknown"),
                    "message": (getattr(dialog, "message", "") or "")[:500],
                    "default_value": (
                        getattr(dialog, "default_value", "") or ""
                    )[:500],
                    "ts": time.time(),
                }
                pending.append(record)
                self._dialog_handles[(session_id, did)] = dialog
                # Safety timeout: if the agent never resolves this
                # within 5min, auto-dismiss so the page isn't blocked
                # forever. Matches the reference's 300s safety net.
                loop = asyncio.get_event_loop()
                loop.call_later(
                    300.0,
                    lambda: self._auto_dismiss_stale_dialog(session_id, did),
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("dialog", _on_dialog)
        except Exception:  # noqa: BLE001
            pass

    def _auto_dismiss_stale_dialog(
        self, session_id: str, dialog_id: str,
    ) -> None:
        """5-min safety net — dismiss a dialog the agent forgot."""
        handle = self._dialog_handles.pop((session_id, dialog_id), None)
        if handle is None:
            return
        try:
            asyncio.ensure_future(handle.dismiss())
        except Exception:  # noqa: BLE001
            pass
        # Move from pending → recent with stale marker.
        pending = self._session_dialogs_pending.get(session_id) or []
        for i, rec in enumerate(pending):
            if rec.get("id") == dialog_id:
                rec = dict(
                    rec, resolved_action="auto_dismiss_stale",
                    resolved_ts=time.time(),
                )
                pending.pop(i)
                recent = self._session_dialogs_recent.setdefault(session_id, [])
                recent.append(rec)
                while len(recent) > 20:
                    recent.pop(0)
                break

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

    async def _ensure_user_browser_context(
        self,
        *,
        browser_name: str | None = None,
        profile_dir: str = "Default",
    ) -> tuple[Any, str]:
        """Resolve a Playwright BrowserContext bound to the USER'S
        real Chrome-family browser.

        Returns ``(context, mode)`` where ``mode`` is one of:

          - ``"cdp_attach"`` — attached to an already-running Chrome
            via CDP at 127.0.0.1:9222. Most natural — uses the user's
            current session, current tabs, current login state. We
            DO NOT own this Browser process; the Browser handle
            stays in ``self._user_browser_handle`` and is detached
            (not closed) on cleanup.

          - ``"launched_real_profile"`` — user's Chrome is NOT
            running, so we spawned it ourselves via
            ``launch_persistent_context`` against the real
            ``%LOCALAPPDATA%\\Google\\Chrome\\User Data`` dir. Their
            cookies / logins / bookmarks are all present. Same
            no-close rule applies: if we close this context, the
            user's profile lock file may get corrupted.

          - ``"side_profile_fallback"`` — user's main Chrome IS
            running on the target profile (lock detected) so we
            couldn't grab the real User Data dir. Falls back to the
            existing ``persistent_profile`` machinery — a side
            profile under ``~/.xmclaw/v2/browser_profiles/<name>``
            that the user logs into separately. This dir IS owned
            by us, so close-on-cleanup is allowed.
        """
        if self._user_browser_context is not None:
            # Liveness probe — same pattern as
            # ``_ensure_persistent_context``. ``.pages`` raises after
            # the context is closed.
            try:
                _ = len(self._user_browser_context.pages)
                # Stored mode lives on the context object itself as
                # a custom attribute set on creation. Default to
                # cdp_attach for backward-safe handling.
                mode = getattr(
                    self._user_browser_context, "_xmclaw_user_browser_mode",
                    "cdp_attach",
                )
                return self._user_browser_context, mode
            except Exception:  # noqa: BLE001
                self._user_browser_context = None
                self._user_browser_handle = None

        from xmclaw.providers.tool._user_browser_detect import (
            BrowserInstall,
            detect_browsers,
            is_user_data_dir_locked,
            pick_browser,
            probe_cdp_endpoint,
        )

        async with self._boot_lock:
            if self._user_browser_context is not None:
                return self._user_browser_context, getattr(
                    self._user_browser_context, "_xmclaw_user_browser_mode",
                    "cdp_attach",
                )
            if self._playwright is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise _PlaywrightMissing(
                        "playwright not installed -- run "
                        "`pip install xmclaw[browser]` then "
                        "`playwright install chromium`"
                    ) from exc
                self._playwright = await async_playwright().start()

            # ── Tier 1: CDP attach (best — uses live user session) ──
            cdp = probe_cdp_endpoint(9222)
            if cdp is not None:
                try:
                    browser = await self._playwright.chromium.connect_over_cdp(cdp)
                except Exception as exc:  # noqa: BLE001
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).warning(
                        "user_browser: CDP attach at %s failed: %s",
                        cdp, exc,
                    )
                else:
                    # Take the first existing context (the user's
                    # main window). Fall back to creating one if
                    # they have no contexts (shouldn't happen with
                    # a real running Chrome).
                    ctx = (
                        browser.contexts[0] if browser.contexts
                        else await browser.new_context()
                    )
                    ctx._xmclaw_user_browser_mode = "cdp_attach"
                    self._user_browser_handle = browser
                    self._user_browser_context = ctx
                    return ctx, "cdp_attach"

            # ── Tier 2 / 3: need to spawn a browser ourselves ──
            install: BrowserInstall | None = pick_browser(browser_name)
            if install is None:
                installed = [b.name for b in detect_browsers()]
                raise RuntimeError(
                    "could not find Chrome / Edge / Brave on this "
                    f"system (detected: {installed!r}). Install one, "
                    "or use browser_open(visible=true) for a clean "
                    "Playwright Chromium window."
                )

            # ── Tier 2: launch user's real profile (if not locked) ──
            locked = is_user_data_dir_locked(install.user_data_dir)
            if not locked:
                launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(install.user_data_dir),
                    "executable_path": str(install.exe_path),
                    "headless": False,
                    "accept_downloads": True,
                    "no_viewport": True,
                    "args": [
                        "--start-maximized",
                        "--new-window",
                        f"--profile-directory={profile_dir}",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                # Use the Playwright channel only if it maps to a
                # known one — Brave goes through plain chromium with
                # ``executable_path`` doing the redirect.
                if install.playwright_channel in ("chrome", "msedge"):
                    launch_kwargs["channel"] = install.playwright_channel
                try:
                    ctx = await self._playwright.chromium.launch_persistent_context(
                        **launch_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).info(
                        "user_browser: real-profile launch failed (%s) — "
                        "falling back to side profile.", exc,
                    )
                else:
                    ctx._xmclaw_user_browser_mode = "launched_real_profile"
                    self._user_browser_context = ctx
                    return ctx, "launched_real_profile"

            # ── Tier 3: side-profile fallback (user's Chrome already
            # running). Reuses the existing persistent_profile dir
            # machinery so the side profile persists across daemon
            # restarts — user logs in once, future sessions inherit.
            side_profile_name = f"user_{install.name}_{profile_dir}"
            ctx = await self._ensure_persistent_context(
                side_profile_name,
                headless=False,
                session_id="__user_browser_side__",
            )
            ctx._xmclaw_user_browser_mode = "side_profile_fallback"
            self._user_browser_context = ctx
            return ctx, "side_profile_fallback"

    async def _bring_to_foreground(self, session_id: str, page: Any) -> None:
        """Force the browser window into the user's foreground.

        Layered, best-effort — each layer covers a different failure
        mode of the previous one. None of them throw on failure;
        worst case we still log so the doctor can diagnose later.

        Layer 1 — Playwright ``page.bring_to_front()``: brings this
            page's TAB to front within the browser. Fast, no OS calls.
            Doesn't touch the OS-level window z-order.

        Layer 2 — Windows ``SetForegroundWindow`` via ctypes: the
            actual fix for "Chrome window opens behind chat". Windows
            blocks foreground stealing from background processes by
            default, so we do the standard trick: send a self-keypress
            first which marks the process as "user interacted",
            unlocking ``SetForegroundWindow``. Only fires on Windows
            and only when the session is headed.

        Headless sessions are a no-op (no window to focus).
        """
        if self._session_headless.get(session_id, self._default_headless):
            return
        # Layer 1: tab to front within the browser.
        try:
            await page.bring_to_front()
        except Exception as exc:  # noqa: BLE001
            # noqa: BLE001 — best-effort; log+continue.
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).debug(
                    "browser._bring_to_foreground: bring_to_front failed: %s",
                    exc,
                )
            except Exception:  # noqa: BLE001
                pass
        # Layer 2: Windows OS-level focus steal. Other platforms get
        # their window-manager defaults (which usually work because
        # Linux/macOS don't have Windows' foreground-stealing block).
        import sys as _sys
        if _sys.platform != "win32":
            return
        try:
            await self._win32_focus_browser_window(page)
        except Exception as exc:  # noqa: BLE001
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).debug(
                    "browser._bring_to_foreground: win32 focus failed: %s",
                    exc,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _win32_focus_browser_window(self, page: Any) -> None:
        """Windows-specific: find the Chrome window owning ``page`` and
        force it foreground.

        The standard ``SetForegroundWindow`` from a background process
        is silently denied by Windows (returns 0) unless the calling
        process recently had user input. The unlock trick: send a
        keystroke to our OWN process (which counts as user input from
        Windows' POV) immediately before the SetForegroundWindow call.

        We can't reliably get Chrome's HWND from Playwright (no API),
        so we enumerate top-level windows and pick the most recent
        Chromium one — good enough when there's only one agent
        browser, and the user is using their own Chrome separately.
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. Find Chrome-family windows owned by the Playwright-spawned
        # Chrome process tree. We don't have the PID directly from
        # Playwright; instead we filter by window class name —
        # Chromium uses "Chrome_WidgetWin_1" for its main browser
        # frame. This catches our window AND any other Chrome on
        # screen; we pick the most-recently-created one.
        candidates: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            if cls_buf.value == "Chrome_WidgetWin_1":
                # Skip "no title" frames (devtools, popups w/o title).
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    candidates.append(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)
        if not candidates:
            return

        # Pick the most recently created Chrome window — heuristic for
        # "the one we just spawned". Z-order from EnumWindows is
        # top-of-stack first, so reverse.
        target_hwnd = candidates[0]

        # 2. Unlock foreground stealing by simulating a keystroke
        # to our OWN window. Per Microsoft docs, ``SetForegroundWindow``
        # only succeeds if "the foreground process is allowing the
        # current process to set the foreground window" — a
        # ``keybd_event`` self-input flips the allow bit.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        # 3. Restore if minimized, then bring to front.
        SW_RESTORE = 9
        user32.ShowWindow(target_hwnd, SW_RESTORE)
        user32.SetForegroundWindow(target_hwnd)

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

        # Wave-27 fix-LAT14: persistent profile route. When set, we
        # bypass the standard launch() + new_context() flow and go
        # through launch_persistent_context(user_data_dir=...) so
        # Chrome's own profile machinery (cookies + extensions +
        # autofill + history + saved passwords) survives across
        # daemon restarts. Mirrors comparable agents's
        # extensions/browser/src/browser/chrome.ts mechanism.
        persistent_profile = bool(call.args.get("persistent_profile", False))
        profile_name = call.args.get("profile_name") or "default"
        use_system_chrome = bool(call.args.get("use_system_chrome", False))
        load_state = call.args.get("load_state")
        if persistent_profile and load_state:
            return _fail(
                call, t0,
                "persistent_profile and load_state are mutually "
                "exclusive — the persistent profile already carries "
                "its own cookies. Pick one.",
            )
        if persistent_profile:
            try:
                _persistent_profile_dir(profile_name.strip())
            except ValueError as exc:
                return _fail(call, t0, str(exc))
            self._session_persistent_profile[sid] = profile_name.strip()
            # ``visible`` defaults to True under persistent_profile —
            # the whole point of the mode is "user can see + agent
            # operates" workflow. Caller can still pass visible=false
            # for headless persistent (e.g. when re-using a logged-in
            # profile for unattended automation).
            if headless is None:
                headless = False
            # Stash the visibility hint + chrome channel on the
            # session so _page_for picks them up at launch time.
            self._session_headless[sid] = headless
            self._session_persistent_chrome_channel[sid] = (
                "chrome" if use_system_chrome else None
            )
        elif call.args.get("profile_name") and not persistent_profile:
            # Wave-27 fix-LAT14b: explicit profile_name without
            # persistent_profile=true is almost certainly a caller
            # mistake (agent thinks profile_name auto-enables
            # persistence; it doesn't). Surface it so login state
            # doesn't silently fail to persist.
            return _fail(
                call, t0,
                "profile_name was set but persistent_profile=false. "
                "Set persistent_profile=true to actually use a real "
                "Chrome profile dir, or drop profile_name and use "
                "load_state for one-shot cookie injection.",
            )

        # Wave 25.2: pre-load saved storage state on first open of a
        # session. Ignored if the session already has a context (the
        # storage_state= kwarg only takes effect at context creation).
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
        # Headed mode: pop the window to the user's foreground so
        # they actually SEE the navigation. Without this the Chrome
        # window often opens behind the chat (Windows blocks
        # background-process foreground-stealing by default) — the
        # user-visible symptom is "agent says it opened the page
        # but I see nothing." See ``_bring_to_foreground`` for the
        # layered fallback strategy.
        await self._bring_to_foreground(sid, page)
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
        visible_only = bool(call.args.get("visible_only", True))
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
        #
        # Wave-27 fix-LAT11 (2026-05-17): when the selector matches
        # MULTIPLE elements (real-world example: ``text=知道了`` on a
        # learning page that has both a hidden modal AND a visible
        # button with the same label), Playwright's ``.first`` picks
        # the FIRST match in DOM order — which is often the hidden
        # one. The hidden match then fails 15s of "element not visible"
        # retries and the click times out, even though a perfectly
        # clickable visible match exists. ``visible_only=true`` (the
        # default) iterates matches and clicks the first VISIBLE one
        # instead. Net effect: clicks "just work" in the common case
        # without the agent having to add ``nth=N`` prefixes by hand.
        try:
            locator = self._resolve_locator(page, sel)
            target = locator
            picked_visible_idx: int | None = None
            if visible_only:
                # Probe match count; if >1 try to find a visible one.
                try:
                    n = await locator.count()
                except Exception:  # noqa: BLE001
                    n = 1
                if n > 1:
                    for i in range(min(n, 20)):  # cap probes
                        candidate = locator.nth(i)
                        try:
                            if await candidate.is_visible():
                                target = candidate
                                picked_visible_idx = i
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    # If no visible match found, fall through to the
                    # original .first behaviour — Playwright's own
                    # error message will then surface a useful
                    # "element not visible" timeout.
            await target.click(force=force)
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
            hint = ""
            if count and count > 1:
                hint = (
                    f" — {count} elements matched the selector but none "
                    f"were visible/actionable. Try a more specific "
                    f"selector, or pass ``visible_only=false`` if you "
                    f"actually want the literal first match, or "
                    f"``nth=N selector`` to pick a specific index."
                )
            return _fail(
                call, t0,
                f"click failed: {type(exc).__name__}: {exc}"
                + (f" (matched {count} elements)" if count is not None else "")
                + hint,
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

        # Keep the headed window in front as the agent operates —
        # otherwise the user loses sight of what's happening when
        # navigation/click triggers a popup or new tab.
        await self._bring_to_foreground(self._sid(call), page)
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
        await self._bring_to_foreground(self._sid(call), page)
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
        await self._bring_to_foreground(self._sid(call), page)
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
        # 2026-05-28 P1.4: annotate=true overlays [N] labels matching
        # the last browser_snapshot's refs onto the image. Vision LLMs
        # can then say "click 5" off the screenshot directly. PIL is
        # the only dep; absence = silent skip (returns plain image).
        annotated = False
        if call.args.get("annotate"):
            sid = self._sid(call)
            ref_map = self._session_refs.get(sid) or {}
            if ref_map:
                try:
                    png_or_jpeg = _draw_ref_overlay(
                        png_or_jpeg, ref_map, fmt=fmt,
                    )
                    annotated = True
                except _PILUnavailable:
                    pass  # silent fallback to unannotated
                except Exception as exc:  # noqa: BLE001
                    from xmclaw.utils.log import get_logger
                    get_logger(__name__).debug(
                        "browser._screenshot: annotate failed: %s", exc,
                    )
        mime = "image/png" if fmt == "png" else "image/jpeg"
        b64 = base64.b64encode(png_or_jpeg).decode("ascii")
        inline_size = len(b64) + len(f"data:{mime};base64,")

        content: dict[str, Any] = {
            "mime": mime,
            "url": page.url,
            "bytes": len(png_or_jpeg),
            "full_page": full,
            "annotated": annotated,
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

        # Wave-27 fix-LAT15b (2026-05-17): DO NOT inline base64
        # data_url into ``content``. content is what gets persisted
        # into ``messages`` and resent to the LLM on EVERY subsequent
        # turn. A typical 1280×800 PNG screenshot is ~70 KB of
        # base64 chars, and Kimi/GPT/Claude all tokenise high-entropy
        # base64 at roughly 1 char/token (NOT 4 like normal text).
        # Empirical: chat-c7040f1e on 2026-05-17 had ONE screenshot
        # message at 72303 chars in history → ~72K tokens at Kimi's
        # rate, while ContextCompressor estimated it as 18K
        # (chars/4). Result: ``ContextCompressor.fire tokens=143799
        # threshold=217600`` decided NOT to fire, then Kimi rejected
        # the actual 364K-token request. Drop the inline so the
        # vision pipeline (metadata.attach_image → next-turn vision
        # content block) carries the image without polluting
        # message history.
        if spill_ok:
            content["path"] = str(out)
        else:
            # No disk path AND no inline: the LLM has no way to see
            # this image. Mark explicitly so the agent doesn't think
            # the call returned a usable result.
            content["error"] = (
                "screenshot taken but could not be saved to disk; "
                "image is unavailable this turn"
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
        sid = self._sid(call)
        title = await page.title()
        # Pull visible innerText (simpler than the a11y tree; works
        # well enough for LLM reasoning over content).
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if text and len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        # 2026-05-28 P0.1: links are now ref'd too — clicking a link
        # by ref number is the common case for "follow that link in
        # the search results".
        links = await page.evaluate(
            """(max) => {
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const label = (a.innerText || a.title || a.href || '').trim();
                    if (!label) continue;
                    const r = a.getBoundingClientRect();
                    const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^\\w-]/g, ''));
                    const sel = a.id
                        ? `#${cssEscape(a.id)}`
                        : `a[href="${a.getAttribute('href').replace(/"/g, '\\\\"')}"]`;
                    out.push({
                        label: label.slice(0, 120),
                        href: a.href,
                        selector: sel,
                        bbox: r.width && r.height
                            ? {x: Math.round(r.left), y: Math.round(r.top),
                               w: Math.round(r.width), h: Math.round(r.height)}
                            : null,
                    });
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
                    const r = el.getBoundingClientRect();
                    out.push({
                        kind,
                        selector: sel,
                        name: el.name || null,
                        type: el.type || null,
                        placeholder: el.placeholder || null,
                        value: typeof el.value === 'string' ? el.value.slice(0, 120) : null,
                        label: labelFor(el),
                        text: kind === 'button' ? (el.innerText || el.value || '').slice(0, 60) : null,
                        bbox: {
                            x: Math.round(r.left), y: Math.round(r.top),
                            w: Math.round(r.width), h: Math.round(r.height),
                        },
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

        # ── 2026-05-28 P0.1: build [N] ref system ─────────────────
        # Assign sequential refs to all interactive elements: form
        # inputs/buttons first, then links. The agent can call
        # browser_click_ref(ref=N) / browser_type_ref(ref=N, text=...)
        # instead of crafting CSS selectors. Map is per-session,
        # invalidated whenever a new snapshot rebuilds it.
        ref_map: dict[int, dict[str, Any]] = {}
        next_ref = 1
        for inp in (inputs or []):
            label_parts: list[str] = []
            if inp.get("kind") == "button":
                label_parts.append(inp.get("text") or inp.get("label") or "button")
            else:
                kind_tag = inp.get("type") or inp.get("kind") or "input"
                label_parts.append(kind_tag)
                if inp.get("label"):
                    label_parts.append(f'"{inp["label"]}"')
                elif inp.get("placeholder"):
                    label_parts.append(f'placeholder="{inp["placeholder"]}"')
                elif inp.get("name"):
                    label_parts.append(f'name="{inp["name"]}"')
            ref_map[next_ref] = {
                "selector": inp.get("selector"),
                "bbox": inp.get("bbox"),
                "kind": inp.get("kind"),
                "label": " ".join(label_parts)[:80],
            }
            inp["ref"] = next_ref
            next_ref += 1
        for link in (links or []):
            ref_map[next_ref] = {
                "selector": link.get("selector"),
                "bbox": link.get("bbox"),
                "kind": "link",
                "label": f'link "{(link.get("label") or "")[:60]}"',
            }
            link["ref"] = next_ref
            next_ref += 1
        # Replace the session's prior ref map atomically (next ref
        # numbers always start from 1 on each snapshot).
        self._session_refs[sid] = ref_map

        # ── 2026-05-28 P0.2: dialog surface for the agent ─────────
        pending_dialogs = list(self._session_dialogs_pending.get(sid, []))
        recent_dialogs = list(self._session_dialogs_recent.get(sid, []))

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "url": page.url,
                "title": title,
                "text": text or "",
                "links": links or [],
                "inputs": inputs or [],
                # P0.1: tells the agent the ref range is [1, next_ref-1]
                # and how to act on those refs.
                "ref_count": len(ref_map),
                "ref_hint": (
                    "Each input/button/link has a 'ref' field. Use "
                    "browser_click_ref(ref=N) / browser_type_ref(ref=N, "
                    "text=...) instead of crafting CSS selectors."
                ) if ref_map else None,
                # P0.2: blocking + recent dialog records. Empty lists
                # are the normal case.
                "pending_dialogs": pending_dialogs,
                "recent_dialogs": recent_dialogs,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _click_ref(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 P0.1: click via [N] ref number from last snapshot."""
        ref = call.args.get("ref")
        if not isinstance(ref, int):
            return _fail(call, t0, "missing or non-integer 'ref'")
        sid = self._sid(call)
        ref_map = self._session_refs.get(sid) or {}
        entry = ref_map.get(ref)
        if entry is None:
            return _fail(
                call, t0,
                f"ref {ref} not found in current snapshot. Call "
                f"browser_snapshot first to get fresh refs "
                f"(valid range: 1..{len(ref_map) or 'none'}).",
            )
        # Delegate to the standard click handler by forging the call's
        # selector arg. Inherits the visible-only / nav-wait logic.
        forged = ToolCall(
            name="browser_click",
            args={
                "selector": entry["selector"],
                "wait_for_navigation_ms": call.args.get(
                    "wait_for_navigation_ms", 2000,
                ),
            },
            provenance=getattr(call, "provenance", "ref_dispatch"),
            session_id=getattr(call, "session_id", None),
        )
        return await self._click(forged, t0)

    async def _type_ref(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 P0.1: fill via [N] ref number from last snapshot."""
        ref = call.args.get("ref")
        text = call.args.get("text")
        submit = bool(call.args.get("submit", False))
        if not isinstance(ref, int):
            return _fail(call, t0, "missing or non-integer 'ref'")
        if not isinstance(text, str):
            return _fail(call, t0, "missing or non-string 'text'")
        sid = self._sid(call)
        ref_map = self._session_refs.get(sid) or {}
        entry = ref_map.get(ref)
        if entry is None:
            return _fail(
                call, t0,
                f"ref {ref} not found in current snapshot. Call "
                f"browser_snapshot first to get fresh refs "
                f"(valid range: 1..{len(ref_map) or 'none'}).",
            )
        forged = ToolCall(
            name="browser_fill",
            args={"selector": entry["selector"], "value": text},
            provenance=getattr(call, "provenance", "ref_dispatch"),
            session_id=getattr(call, "session_id", None),
        )
        result = await self._fill(forged, t0)
        if result.ok and submit:
            # Common case: search box / single-field form. Press
            # Enter on the same selector after filling.
            page = await self._page_for(sid)
            try:
                await self._resolve_locator(page, entry["selector"]).press("Enter")
                await self._bring_to_foreground(sid, page)
            except Exception as exc:  # noqa: BLE001
                # The fill succeeded; report partial success.
                return ToolResult(
                    call_id=call.id, ok=True,
                    content=(
                        f"typed into ref {ref} but submit (Enter) "
                        f"failed: {type(exc).__name__}: {exc}"
                    ),
                    side_effects=(),
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
        return result

    async def _dialog_arm(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 P2.4: pre-arm the next dialog."""
        action = call.args.get("action")
        if action not in ("accept", "dismiss", "clear"):
            return _fail(
                call, t0,
                f"action must be accept/dismiss/clear, got {action!r}",
            )
        sid = self._sid(call)
        if action == "clear":
            cleared = self._session_dialog_armed.pop(sid, None)
            return ToolResult(
                call_id=call.id, ok=True,
                content={"cleared": cleared is not None},
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        self._session_dialog_armed[sid] = {
            "action": action,
            "text": call.args.get("text") or "",
        }
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "armed_action": action,
                "note": (
                    "The next dialog on this session will be "
                    f"auto-{action}-ed and the arm will self-clear."
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _dialog(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 P0.2: resolve a blocking JS dialog."""
        action = call.args.get("action")
        if action not in ("accept", "dismiss", "respond"):
            return _fail(
                call, t0,
                f"action must be 'accept' | 'dismiss' | 'respond', "
                f"got {action!r}",
            )
        sid = self._sid(call)
        pending = self._session_dialogs_pending.get(sid) or []
        if not pending:
            return _fail(call, t0, "no pending dialog on this session")
        dialog_id = call.args.get("id")
        if dialog_id is None:
            # Default to oldest pending — usually what the agent wants.
            target = pending[0]
        else:
            target = next(
                (d for d in pending if d.get("id") == dialog_id), None,
            )
            if target is None:
                return _fail(
                    call, t0,
                    f"dialog id={dialog_id!r} not pending (pending ids: "
                    f"{[d.get('id') for d in pending]})",
                )
        handle = self._dialog_handles.get((sid, target["id"]))
        if handle is None:
            return _fail(call, t0, "dialog handle expired (page may have closed)")
        try:
            if action == "accept":
                await handle.accept()
            elif action == "dismiss":
                await handle.dismiss()
            else:  # respond
                text = call.args.get("text") or ""
                if target.get("type") != "prompt":
                    return _fail(
                        call, t0,
                        f"'respond' only valid for prompt() dialogs, "
                        f"this is a {target.get('type')!r}",
                    )
                await handle.accept(text)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"dialog {action} failed: {type(exc).__name__}: {exc}",
            )
        # Move from pending → recent (ring-buffered).
        self._session_dialogs_pending[sid] = [
            d for d in pending if d.get("id") != target["id"]
        ]
        target = dict(target, resolved_action=action, resolved_ts=time.time())
        recent = self._session_dialogs_recent.setdefault(sid, [])
        recent.append(target)
        while len(recent) > 20:
            recent.pop(0)
        self._dialog_handles.pop((sid, target["id"]), None)
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "id": target["id"],
                "type": target.get("type"),
                "action": action,
                "message": target.get("message"),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _eval(self, call: ToolCall, t0: float) -> ToolResult:
        if not self._evaluate_enabled:
            return _fail(
                call, t0,
                "browser_eval is disabled by config "
                "(tools.browser.evaluate_enabled=false). Use targeted "
                "tools (browser_click_ref / browser_type_ref / "
                "browser_snapshot / browser_network_log) instead of "
                "arbitrary-JS reach.",
            )
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

    async def _network_log(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 P3.5: read recent request/response entries.

        Filters: ``url_glob`` (shell glob), ``method``, ``status_min``.
        Optionally fetches response bodies (``with_body``) — capped
        at 64 KB per entry to bound the payload.
        """
        import fnmatch
        sid = self._sid(call)
        buf = self._network_buffers.get(sid) or []
        url_glob = call.args.get("url_glob")
        # fnmatch's '*' already matches '/'; ergonomic alias '**' → '*'
        # so shell-style ``**/api/*`` patterns just work.
        if isinstance(url_glob, str):
            url_glob = url_glob.replace("**", "*")
        method = call.args.get("method")
        status_min = int(call.args.get("status_min", 0) or 0)
        with_body = bool(call.args.get("with_body", False))
        limit = int(call.args.get("limit", 20))
        clear = bool(call.args.get("clear", False))

        # Walk newest-first so the LLM gets the most-recent responses
        # (most relevant after a click that fired XHR).
        out: list[dict[str, Any]] = []
        for entry in reversed(buf):
            url = entry.get("url") or ""
            if url_glob and not fnmatch.fnmatch(url, url_glob):
                continue
            if method and (entry.get("method") or "").upper() != method.upper():
                continue
            status = entry.get("status")
            if status is None and status_min > 0:
                continue
            if status is not None and status < status_min:
                continue
            record = {
                "method": entry.get("method"),
                "url": url,
                "status": status,
                "request_headers": entry.get("request_headers"),
                "response_headers": entry.get("response_headers"),
                "ts": entry.get("ts"),
            }
            if with_body:
                resp_obj = entry.get("_response_obj")
                if resp_obj is not None:
                    try:
                        body = await resp_obj.body()
                        if isinstance(body, bytes):
                            if len(body) > 64 * 1024:
                                body = body[:64 * 1024]
                                record["body_truncated"] = True
                            try:
                                record["body"] = body.decode("utf-8")
                            except UnicodeDecodeError:
                                record["body_base64"] = (
                                    base64.b64encode(body).decode("ascii")
                                )
                    except Exception as exc:  # noqa: BLE001
                        record["body_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
            out.append(record)
            if len(out) >= limit:
                break

        if clear:
            self._network_buffers[sid] = []

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "total_in_buffer": len(buf),
                "returned": len(out),
                "entries": out,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _use_my_browser(self, call: ToolCall, t0: float) -> ToolResult:
        """Open ``url`` in the user's real Chrome with full agent
        control. The 3-mode resolution lives in
        ``_ensure_user_browser_context``; this handler just pins the
        session, navigates, and reports back which mode landed.

        Once a session has been opened via this tool, EVERY other
        browser_* call in the same session routes through the same
        user-Chrome context — agent gets click/fill/snapshot/eval
        the same way as a normal browser_open session.
        """
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _fail(call, t0, "missing or empty 'url'")
        if not (url.startswith("http://") or url.startswith("https://")):
            return _fail(call, t0, f"url must start with http(s)://, got {url!r}")
        self._check_host(url)
        wait_until = call.args.get("wait_until") or "load"
        if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
            return _fail(call, t0, f"wait_until={wait_until!r} not supported")

        browser_name = call.args.get("browser") or "auto"
        profile = call.args.get("profile") or "Default"

        sid = self._sid(call)
        # Pin the session to user-CDP routing before _page_for runs.
        # Once pinned, every subsequent browser_* call on this sid
        # routes through ``_ensure_user_browser_context``.
        self._session_user_cdp[sid] = True
        # Headed mode is implicit (the user's real Chrome is visible
        # by definition). Pin so _bring_to_foreground does its work.
        self._session_headless[sid] = False

        try:
            ctx, mode = await self._ensure_user_browser_context(
                browser_name=browser_name if browser_name != "auto" else None,
                profile_dir=profile,
            )
        except _PlaywrightMissing as exc:
            self._session_user_cdp.pop(sid, None)
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._session_user_cdp.pop(sid, None)
            return _fail(
                call, t0,
                f"user_browser setup failed: {type(exc).__name__}: {exc}",
            )

        page = await self._page_for(sid)
        try:
            resp = await page.goto(url, wait_until=wait_until)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"navigation failed: {type(exc).__name__}: {exc}",
            )
        await self._bring_to_foreground(sid, page)
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "url": page.url,
                "title": await page.title(),
                "status": resp.status if resp is not None else None,
                "mode": mode,
                "uses_user_real_session": mode in (
                    "cdp_attach", "launched_real_profile",
                ),
                "note": {
                    "cdp_attach": (
                        "Attached to user's already-running Chrome via "
                        "CDP. All their tabs / cookies / logins are "
                        "live. Closing this session does NOT close "
                        "their browser."
                    ),
                    "launched_real_profile": (
                        "Spawned user's Chrome with their real "
                        "profile dir. Cookies / logins / bookmarks "
                        "all present. Daemon shutdown closes this "
                        "Chrome instance."
                    ),
                    "side_profile_fallback": (
                        "User's Chrome is already running on this "
                        "profile so we couldn't grab the lock. Opened "
                        "a side profile under ~/.xmclaw/ — first time "
                        "the user must log in to target sites in this "
                        "window; logins persist thereafter."
                    ),
                }[mode],
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
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
        await self._bring_to_foreground(self._sid(call), page)
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
            await self._bring_to_foreground(self._sid(call), page)
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
        await self._bring_to_foreground(self._sid(call), page)
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
        await self._bring_to_foreground(self._sid(call), page)
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
        """2026-05-28 P3.7: composite wait — AND of {selector, url_glob,
        load_state, js_predicate}. Legacy single-selector calls keep
        working unchanged (selector is just one of N possible
        conditions).
        """
        sel = call.args.get("selector")
        url_glob = call.args.get("url_glob")
        load_state = call.args.get("load_state")
        js_predicate = call.args.get("js_predicate")
        state = (call.args.get("state") or "visible").lower()
        timeout = int(call.args.get("timeout_ms", 10_000))

        if state not in ("attached", "detached", "visible", "hidden"):
            return _fail(
                call, t0,
                f"state must be attached/detached/visible/hidden, got {state!r}",
            )
        if load_state and load_state not in (
            "load", "domcontentloaded", "networkidle",
        ):
            return _fail(
                call, t0,
                f"load_state must be load/domcontentloaded/networkidle, "
                f"got {load_state!r}",
            )

        conds: list[tuple[str, Any]] = []
        if isinstance(sel, str) and sel:
            conds.append(("selector", sel))
        if isinstance(url_glob, str) and url_glob:
            conds.append(("url_glob", url_glob))
        if isinstance(load_state, str) and load_state:
            conds.append(("load_state", load_state))
        if isinstance(js_predicate, str) and js_predicate.strip():
            conds.append(("js_predicate", js_predicate))
        if not conds:
            return _fail(
                call, t0,
                "at least one condition required: selector / url_glob / "
                "load_state / js_predicate",
            )

        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")

        satisfied: list[str] = []
        try:
            for kind, value in conds:
                if kind == "selector":
                    await self._resolve_locator(page, value).wait_for(
                        state=state, timeout=timeout,
                    )
                elif kind == "url_glob":
                    await page.wait_for_url(value, timeout=timeout)
                elif kind == "load_state":
                    await page.wait_for_load_state(value, timeout=timeout)
                elif kind == "js_predicate":
                    # Wrap so JS expressions like `x === true` work
                    # without the agent writing `() => (x === true)`.
                    wrapped = f"() => Boolean({value})"
                    await page.wait_for_function(wrapped, timeout=timeout)
                satisfied.append(kind)
        except ValueError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"wait_for timed out on condition={kind!r}: "
                f"{type(exc).__name__}: {exc}. Satisfied so far: "
                f"{satisfied}.",
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "satisfied": satisfied,
                "url": page.url,
                "selector": sel,
                "state": state if sel else None,
            },
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


def _persistent_profile_dir(name: str) -> Any:
    """Wave-27 fix-LAT14: resolve a persistent profile name to its
    Chromium user-data-dir under
    ``~/.xmclaw/v2/browser_profiles/<name>/user-data``. The Chrome
    binary writes its full profile state (cookies / localStorage /
    extensions / passwords / autofill / history) into this directory
    — distinct from the storage_state JSON files at
    ~/.xmclaw/v2/browser_state/ which carry only cookies +
    localStorage.

    Same sanitization rule as ``_state_profile_path``: alphanumeric +
    dash + underscore only, so agents can't smuggle path-traversal
    via a hand-crafted profile_name.
    """
    import re
    from pathlib import Path as _P

    from xmclaw.utils.paths import data_dir
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(
            f"profile_name {name!r} must match [A-Za-z0-9_-]+"
        )
    return _P(data_dir() / "v2" / "browser_profiles" / name / "user-data")


class _PILUnavailable(RuntimeError):
    """Raised when ``Pillow`` isn't installed and an annotate=true
    screenshot was requested. Callers catch this and fall back to a
    plain image (the annotation is a nice-to-have, not a hard
    dependency)."""


def _draw_ref_overlay(
    image_bytes: bytes,
    ref_map: dict[int, dict[str, Any]],
    *,
    fmt: str = "png",
) -> bytes:
    """2026-05-28 P1.4: overlay ``[N]`` labels on a screenshot.

    Draws a small filled badge at each ref'd element's bounding box
    upper-left corner. Vision-capable models read these directly:
    "click 5" maps unambiguously to the element wrapped in [5].

    Returns the re-encoded image bytes (same format as input).
    Raises ``_PILUnavailable`` if Pillow isn't installed.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError as exc:
        raise _PILUnavailable("Pillow not installed") from exc

    import io
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Try a decent system font; fall back to PIL's bitmap default
    # (worse-looking but always present).
    font = None
    for font_name in ("arial.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            font = ImageFont.truetype(font_name, 14)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()

    for ref_num, entry in ref_map.items():
        bbox = entry.get("bbox")
        if not bbox:
            continue
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        # Bounding-box outline so the user knows which element each
        # number refers to.
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        if w > 0 and h > 0:
            draw.rectangle(
                [(x, y), (x + w, y + h)],
                outline=(255, 50, 50, 220), width=2,
            )
        # Badge at upper-left of element with the ref number.
        label = f"[{ref_num}]"
        # Background rectangle for legibility.
        try:
            text_w, text_h = draw.textsize(label, font=font)  # type: ignore[attr-defined]
        except AttributeError:
            # Pillow >=10 dropped textsize. Use textbbox.
            l, t, r, b = draw.textbbox((0, 0), label, font=font)
            text_w, text_h = r - l, b - t
        pad = 3
        badge_x0 = max(0, x - 2)
        badge_y0 = max(0, y - text_h - pad * 2)
        badge_x1 = badge_x0 + text_w + pad * 2
        badge_y1 = badge_y0 + text_h + pad * 2
        draw.rectangle(
            [(badge_x0, badge_y0), (badge_x1, badge_y1)],
            fill=(255, 50, 50, 230),
        )
        draw.text(
            (badge_x0 + pad, badge_y0 + pad),
            label, fill=(255, 255, 255, 255), font=font,
        )

    composited = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    if fmt == "jpeg":
        composited.save(out, format="JPEG", quality=85)
    else:
        composited.save(out, format="PNG", optimize=True)
    return out.getvalue()


class _PlaywrightMissing(RuntimeError):
    """Sentinel used to distinguish missing-optional-dep from other errors."""


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


# Epic #27 sweep #16 (2026-05-19): re-export from _helpers so the
# browser tool's exception handlers can use the same structured-error
# envelope as the other built-in tools (file_write etc).
from xmclaw.providers.tool._helpers import _fail_with_hint  # noqa: E402,F401
