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


# ── tool specs ────────────────────────────────────────────────────

_BROWSER_OPEN_SPEC = ToolSpec(
    name="browser_open",
    description=(
        "Navigate the browser to a URL. Opens a headless Chromium page "
        "tied to this session; subsequent browser tools operate on it. "
        "Returns the final URL after redirects + the page title."
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
        },
        "required": ["url"],
    },
)

_BROWSER_CLICK_SPEC = ToolSpec(
    name="browser_click",
    description=(
        "Click an element by CSS selector (or Playwright text= selector). "
        "Fails if the element isn't found within 5 seconds."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector or 'text=...' / 'role=...' Playwright locator.",
            },
        },
        "required": ["selector"],
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
        "Take a PNG screenshot of the current viewport. Returns a "
        "base64-encoded data URL. Full-page screenshots are opt-in via "
        "``full_page=true``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "Capture the entire scrollable page. Default false.",
            },
        },
    },
)

_BROWSER_SNAPSHOT_SPEC = ToolSpec(
    name="browser_snapshot",
    description=(
        "Return a lightweight text+link view of the current page -- "
        "good for LLM reasoning over content without the image-parsing "
        "overhead. Includes title, visible text (truncated), and the "
        "top N hyperlinks."
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
        self._headless = headless
        self._timeout_ms = timeout_ms
        # Shared across sessions -- Playwright / browser are expensive
        # to start (~1s), cheap per-session context on top.
        self._playwright = None
        self._browser = None
        self._contexts: dict[str, Any] = {}   # session_id -> BrowserContext
        self._pages:    dict[str, Any] = {}   # session_id -> Page
        # Guard the lazy init with a lock to avoid multiple concurrent
        # tool calls trying to spin up the browser at once.
        self._boot_lock = asyncio.Lock()

    def list_tools(self) -> list[ToolSpec]:
        # These always appear in the spec -- the model shouldn't have
        # to guess whether browser is enabled. If playwright is missing
        # the tool returns a structured install-me error when called,
        # which is much friendlier than "unknown tool".
        return [
            _BROWSER_OPEN_SPEC, _BROWSER_CLICK_SPEC, _BROWSER_FILL_SPEC,
            _BROWSER_SCREENSHOT_SPEC, _BROWSER_SNAPSHOT_SPEC,
            _BROWSER_EVAL_SPEC, _BROWSER_CLOSE_SPEC,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "browser_open":
                return await self._open(call, t0)
            if call.name == "browser_click":
                return await self._click(call, t0)
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

    async def shutdown(self) -> None:
        """Close every session + the shared browser. For daemon shutdown."""
        for sid in list(self._contexts):
            await self.close_session(sid)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001,S110
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001,S110
                pass
            self._playwright = None

    # ── internals ──────────────────────────────────────────────────

    async def _ensure_playwright(self):
        """Lazy import + boot. Raises _PlaywrightMissing if not installed."""
        if self._browser is not None:
            return
        async with self._boot_lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise _PlaywrightMissing(
                    "playwright not installed -- run "
                    "`pip install xmclaw[browser]` then `playwright install chromium`"
                ) from exc
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
            )

    async def _page_for(self, session_id: str):
        await self._ensure_playwright()
        page = self._pages.get(session_id)
        if page is not None and not page.is_closed():
            return page
        ctx = self._contexts.get(session_id)
        if ctx is None:
            ctx = await self._browser.new_context(
                accept_downloads=False,
                viewport={"width": 1280, "height": 800},
            )
            ctx.set_default_timeout(self._timeout_ms)
            self._contexts[session_id] = ctx
        page = await ctx.new_page()
        self._pages[session_id] = page
        return page

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

        page = await self._page_for(self._sid(call))
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
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _click(self, call: ToolCall, t0: float) -> ToolResult:
        sel = call.args.get("selector")
        if not isinstance(sel, str) or not sel:
            return _fail(call, t0, "missing or empty 'selector'")
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        await page.click(sel)
        return ToolResult(
            call_id=call.id, ok=True, content=f"clicked {sel!r}",
            side_effects=(), latency_ms=(time.perf_counter() - t0) * 1000.0,
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
        await page.fill(sel, val)
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"filled {sel!r} with {len(val)} chars",
            side_effects=(), latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _screenshot(self, call: ToolCall, t0: float) -> ToolResult:
        full = bool(call.args.get("full_page", False))
        page = await self._page_for(self._sid(call))
        if page is None or page.url == "about:blank":
            return _fail(call, t0, "no page open -- call browser_open first")
        png = await page.screenshot(full_page=full, type="png")
        b64 = base64.b64encode(png).decode("ascii")
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "mime": "image/png",
                "url": page.url,
                "bytes": len(png),
                # The agent likely wants a data-URL it can embed in a
                # reply to the user. Keep it short in the event log by
                # truncating for display -- full data stays in content.
                "data_url": f"data:image/png;base64,{b64}",
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _snapshot(self, call: ToolCall, t0: float) -> ToolResult:
        max_chars = int(call.args.get("max_chars", 8000))
        max_links = int(call.args.get("max_links", 30))
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
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "url": page.url,
                "title": title,
                "text": text or "",
                "links": links or [],
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

    def _sid(self, call: ToolCall) -> str:
        return call.session_id or "_default"


class _PlaywrightMissing(RuntimeError):
    """Sentinel used to distinguish missing-optional-dep from other errors."""


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
