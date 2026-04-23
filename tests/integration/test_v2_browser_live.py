"""Browser live integration — real Playwright chromium against a local
test server. No external network needed; we stand up an aiohttp-style
static HTML server on localhost so tests are hermetic and fast.

Skipped cleanly when playwright isn't installed. Run with:

    pytest tests/integration/test_v2_browser_live.py -v
"""
from __future__ import annotations

import asyncio
import base64
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("playwright")


def _chromium_installed() -> bool:
    """Smoke-launch chromium at collect-time.

    The ``playwright`` Python package is listed as a dev dep (so
    ``importorskip`` passes on CI), but the chromium *binary* is a
    separate download that contributors / CI workflows must trigger
    explicitly via ``playwright install chromium``. Without it,
    ``BrowserType.launch`` raises at use-site with the "Executable
    doesn't exist" message — which shows up as 6 FAILURES in this
    module, not a clean skip. We probe once here so the whole module
    skips cleanly when the binary is missing.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return False

    async def _probe() -> bool:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
                await browser.close()
                return True
            except Exception:
                return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


if not _chromium_installed():
    pytest.skip(
        "Chromium binary unavailable — run `playwright install chromium` "
        "to enable browser-live tests.",
        allow_module_level=True,
    )


from xmclaw.core.ir import ToolCall  # noqa: E402 — after skip gate
from xmclaw.providers.tool.browser import BrowserTools  # noqa: E402


_PAGE_HTML = b"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"><title>XMclaw Live Fixture</title>
</head><body>
  <h1 id="hero">Hello, Browser Tools</h1>
  <p id="body">This is a local fixture for testing.</p>
  <input id="q" type="text" placeholder="search">
  <button id="go">Go</button>
  <a href="https://example.com/first">First link</a>
  <a href="https://example.com/second">Second link</a>
  <script>
    document.getElementById('go').addEventListener('click', () => {
      document.body.setAttribute('data-clicked', '1');
    });
  </script>
</body></html>"""


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    return port


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE_HTML)))
        self.end_headers()
        self.wfile.write(_PAGE_HTML)

    def log_message(self, *_a, **_k):  # silence per-request noise
        return


@pytest.fixture(scope="module")
def fixture_server():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/"
    server.shutdown()


@pytest.fixture
async def browser_tools():
    tools = BrowserTools(headless=True, timeout_ms=8_000)
    yield tools
    await tools.shutdown()


def _call(name: str, args: dict, session_id: str = "live") -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_open_returns_title_and_url(browser_tools, fixture_server) -> None:
    r = await browser_tools.invoke(_call(
        "browser_open", {"url": fixture_server},
    ))
    assert r.ok is True, f"open failed: {r.error}"
    assert r.content["title"] == "XMclaw Live Fixture"
    assert r.content["url"].startswith("http://127.0.0.1:")
    assert r.content["status"] == 200


@pytest.mark.asyncio
async def test_snapshot_extracts_title_text_links(
    browser_tools, fixture_server,
) -> None:
    await browser_tools.invoke(_call("browser_open", {"url": fixture_server}))
    r = await browser_tools.invoke(_call("browser_snapshot", {"max_links": 5}))
    assert r.ok is True
    body = r.content
    assert body["title"] == "XMclaw Live Fixture"
    assert "Hello, Browser Tools" in body["text"]
    assert "local fixture" in body["text"]
    hrefs = {link["href"] for link in body["links"]}
    assert "https://example.com/first" in hrefs
    assert "https://example.com/second" in hrefs


@pytest.mark.asyncio
async def test_click_and_fill_actually_mutate_page(
    browser_tools, fixture_server,
) -> None:
    await browser_tools.invoke(_call("browser_open", {"url": fixture_server}))
    r1 = await browser_tools.invoke(_call(
        "browser_fill", {"selector": "#q", "value": "xmclaw tests"},
    ))
    assert r1.ok is True
    r2 = await browser_tools.invoke(_call(
        "browser_click", {"selector": "#go"},
    ))
    assert r2.ok is True
    # Verify via eval that the JS click handler fired.
    r3 = await browser_tools.invoke(_call(
        "browser_eval", {"expression": "document.body.getAttribute('data-clicked')"},
    ))
    assert r3.ok is True
    assert r3.content == "1"
    # And that the fill value stuck.
    r4 = await browser_tools.invoke(_call(
        "browser_eval", {"expression": "document.getElementById('q').value"},
    ))
    assert r4.ok is True
    assert r4.content == "xmclaw tests"


@pytest.mark.asyncio
async def test_screenshot_returns_valid_png(
    browser_tools, fixture_server,
) -> None:
    await browser_tools.invoke(_call("browser_open", {"url": fixture_server}))
    r = await browser_tools.invoke(_call("browser_screenshot", {}))
    assert r.ok is True
    du = r.content["data_url"]
    assert du.startswith("data:image/png;base64,")
    png = base64.b64decode(du.split(",", 1)[1])
    # PNG magic: 89 50 4E 47 0D 0A 1A 0A
    assert png[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {png[:8]!r}"
    assert len(png) > 200  # real screenshots are much larger than the fake 12-byte mock


@pytest.mark.asyncio
async def test_sessions_have_independent_pages(
    browser_tools, fixture_server,
) -> None:
    await browser_tools.invoke(_call(
        "browser_open", {"url": fixture_server},
        session_id="alpha",
    ))
    await browser_tools.invoke(_call(
        "browser_open", {"url": fixture_server + "?q=beta"},
        session_id="beta",
    ))
    # Each should report its own URL.
    r_a = await browser_tools.invoke(_call(
        "browser_eval", {"expression": "location.href"},
        session_id="alpha",
    ))
    r_b = await browser_tools.invoke(_call(
        "browser_eval", {"expression": "location.href"},
        session_id="beta",
    ))
    assert "?q=beta" not in r_a.content
    assert "?q=beta" in r_b.content


@pytest.mark.asyncio
async def test_close_session_tears_down_without_affecting_others(
    browser_tools, fixture_server,
) -> None:
    await browser_tools.invoke(_call(
        "browser_open", {"url": fixture_server},
        session_id="keep",
    ))
    await browser_tools.invoke(_call(
        "browser_open", {"url": fixture_server},
        session_id="drop",
    ))
    await browser_tools.invoke(_call(
        "browser_close", {}, session_id="drop",
    ))
    # The other session is still usable.
    r = await browser_tools.invoke(_call(
        "browser_eval", {"expression": "1+1"},
        session_id="keep",
    ))
    assert r.ok is True
    assert r.content == 2


@pytest.mark.asyncio
async def test_open_refuses_non_http_scheme(browser_tools) -> None:
    r = await browser_tools.invoke(_call(
        "browser_open", {"url": "file:///etc/passwd"},
    ))
    assert r.ok is False
    assert "http" in r.error.lower()


@pytest.mark.asyncio
async def test_allowed_hosts_blocks_external(
    fixture_server,
) -> None:
    """Unlike the per-test browser_tools fixture, use one with a strict
    allowed_hosts containing only a placeholder -- the fixture server's
    host won't be in it."""
    tools = BrowserTools(headless=True, allowed_hosts=["example.com"])
    try:
        r = await tools.invoke(_call("browser_open", {"url": fixture_server}))
        assert r.ok is False
        assert "allowed_hosts" in r.error
    finally:
        await tools.shutdown()
