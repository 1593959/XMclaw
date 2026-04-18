"""Web search tool — tries Playwright first, falls back to httpx."""
import httpx
import re
import urllib.parse
from xmclaw.tools.base import Tool


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for current information (weather, news, facts, etc.). Use this when you need up-to-date information from the internet."
    parameters = {
        "query": {
            "type": "string",
            "description": "Search query (e.g. 'Beijing weather tomorrow').",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum results to return. Default 5.",
        },
    }

    async def execute(self, query: str, max_results: int = 5) -> str:
        # Try Playwright first
        try:
            return await self._playwright_search(query, max_results)
        except Exception as _pw_err:
            pass

        # Fallback: HTTP-based search via DuckDuckGo HTML
        return await self._http_search(query, max_results)

    async def _playwright_search(self, query: str, max_results: int) -> str:
        """Search using Playwright + 360 search (best results, requires browser)."""
        from playwright.async_api import async_playwright

        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.so.com/s?q={encoded_query}"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            try:
                await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                lines = []
                results = await page.query_selector_all("li.res-list")

                for i, r in enumerate(results[:max_results]):
                    try:
                        title_el = await r.query_selector("h3 a, h3")
                        title = await title_el.inner_text() if title_el else ""
                        link_el = await r.query_selector("h3 a")
                        link = await link_el.get_attribute("href") or "" if link_el else ""
                        snippet_el = await r.query_selector("p.res-desc, p")
                        snippet = await snippet_el.inner_text() if snippet_el else ""

                        if title:
                            lines.append(f"- {title[:100]}")
                        if link:
                            lines.append(f"  {link[:150]}")
                        if snippet:
                            lines.append(f"  {snippet[:200]}")
                    except Exception:
                        pass

                await browser.close()
                return "\n".join(lines) if lines else "No results found."
            except Exception as e:
                await browser.close()
                raise e  # Re-raise so fallback is triggered

    async def _http_search(self, query: str, max_results: int) -> str:
        """Fallback HTTP search via DuckDuckGo HTML (no browser required)."""
        encoded = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                html = resp.text

            # Parse result snippets from DuckDuckGo HTML
            lines = []
            results = re.findall(
                r'<a class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<a class="result__snippet"[^>]*>(.*?)</a>',
                html, re.DOTALL,
            )
            for link, title_raw, snippet_raw in results[:max_results]:
                title = re.sub(r'<[^>]+>', '', title_raw).strip()
                snippet = re.sub(r'<[^>]+>', '', snippet_raw).strip()
                link = link.strip()
                if title:
                    lines.append(f"- {title[:120]}")
                if link:
                    lines.append(f"  {link[:150]}")
                if snippet:
                    lines.append(f"  {snippet[:200]}")

            if lines:
                return "\n".join(lines)

            # Second pattern: simpler result links
            links = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]{10,80})</a>', html)
            for href, text in links[:max_results]:
                text = text.strip()
                if text and not text.startswith("http"):
                    lines.append(f"- {text[:120]}")
                    lines.append(f"  {href[:150]}")

            return "\n".join(lines) if lines else (
                "[Search unavailable — install Playwright for better results:\n"
                "  pip install playwright && playwright install chromium]"
            )
        except Exception as e:
            return f"[Search Error: {e}]"
