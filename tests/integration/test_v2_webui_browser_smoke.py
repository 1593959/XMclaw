"""Mission Control browser smoke test.

This is intentionally lightweight and optional: it skips when Playwright or
the Chromium browser binary is unavailable, but when enabled it verifies that
the built React UI mounts in a real browser against the local FastAPI app.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn

pytest.importorskip("playwright")


def _chromium_installed() -> bool:
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


from playwright.async_api import async_playwright  # noqa: E402

from xmclaw.core.bus import InProcessEventBus  # noqa: E402
from xmclaw.daemon.app import create_app  # noqa: E402

CHROMIUM_AVAILABLE = _chromium_installed()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def webui_server():
    if not CHROMIUM_AVAILABLE:
        pytest.skip(
            "Chromium binary unavailable - run `playwright install chromium` "
            "to enable webui browser smoke tests.",
        )
    port = _free_port()
    app = create_app(bus=InProcessEventBus(), config={})
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("uvicorn test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_mission_control_react_ui_mounts_in_browser(webui_server: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            response = await page.goto(f"{webui_server}/ui/", wait_until="networkidle")
            assert response is not None and response.status == 200
            await page.wait_for_selector("#root", state="attached")
            mounted = await page.evaluate(
                "() => document.querySelector('#root')?.children.length || 0",
            )
            assert mounted > 0
            assert await page.locator("body").inner_text(timeout=5000)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_mission_control_mobile_domain_nav_is_visible(webui_server: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            response = await page.goto(f"{webui_server}/ui/", wait_until="networkidle")
            assert response is not None and response.status == 200
            for domain in ("tasks", "memory", "skills", "files", "team", "system"):
                button = page.locator(f'button[data-domain="{domain}"]').last
                await button.wait_for(state="visible", timeout=5000)
                assert await button.is_visible()
        finally:
            await browser.close()
