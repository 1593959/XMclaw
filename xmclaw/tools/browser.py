"""Browser automation tool using Playwright."""
from xmclaw.tools.base import Tool


class BrowserTool(Tool):
    name = "browser"
    description = "Open a URL, take a screenshot, or interact with a web page."
    parameters = {
        "action": {
            "type": "string",
            "description": "One of: open, screenshot, click, type, snapshot",
        },
        "url": {
            "type": "string",
            "description": "URL for open action.",
        },
        "selector": {
            "type": "string",
            "description": "CSS selector for click/type.",
        },
        "text": {
            "type": "string",
            "description": "Text to type.",
        },
        "headless": {
            "type": "boolean",
            "description": "Run browser in headless mode. Default False.",
        },
    }

    async def execute(
        self,
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        headless: bool = False,
    ) -> str:
        from playwright.async_api import async_playwright

        result = ""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()

            try:
                if action == "open" and url:
                    await page.goto(url)
                    result = f"Opened {url}"
                elif action == "screenshot":
                    path = "screenshot.png"
                    await page.screenshot(path=path, full_page=True)
                    result = f"Screenshot saved to {path}"
                elif action == "click" and selector:
                    await page.click(selector)
                    result = f"Clicked {selector}"
                elif action == "type" and selector and text:
                    await page.fill(selector, text)
                    result = f"Typed into {selector}"
                elif action == "snapshot":
                    content = await page.content()
                    result = content[:4000]
                else:
                    result = "[Error: Invalid action or missing parameters]"
            except Exception as e:
                result = f"[Browser Error: {e}]"
            finally:
                await browser.close()

        return result
