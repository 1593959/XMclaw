"""Web search tool using Tavily or browser fallback."""
import os
import httpx
from xmclaw.tools.base import Tool


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for current information."
    parameters = {
        "query": {
            "type": "string",
            "description": "Search query.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum results to return. Default 5.",
        },
    }

    async def execute(self, query: str, max_results: int = 5) -> str:
        api_key = os.getenv("TAVILY_API_KEY", "")
        
        # Try Tavily first if API key is available
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": api_key,
                            "query": query,
                            "max_results": max_results,
                            "search_depth": "basic",
                        },
                    )
                    data = resp.json()
                    results = data.get("results", [])
                    if results:
                        lines = []
                        for r in results:
                            lines.append(f"- {r.get('title')}: {r.get('url')}")
                            lines.append(f"  {r.get('content', '')[:200]}")
                        return "\n".join(lines)
            except Exception as e:
                pass  # Fall through to browser fallback
        
        # Fallback: use browser to search via Bing/Google
        try:
            from playwright.async_api import async_playwright
            search_url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(search_url, timeout=15000)
                
                # Wait for search results
                await page.wait_for_selector('li.b_algo', timeout=8000)
                
                results = await page.query_selector_all('li.b_algo')
                lines = []
                for i, r in enumerate(results[:max_results]):
                    title_el = await r.query_selector('h2')
                    snippet_el = await r.query_selector('div.b_caption p')
                    if title_el:
                        title = await title_el.inner_text()
                        lines.append(f"- {title}")
                    if snippet_el:
                        snippet = await snippet_el.inner_text()
                        lines.append(f"  {snippet[:200]}")
                
                await browser.close()
                
                if lines:
                    return "\n".join(lines)
                else:
                    return "No search results found."
        except Exception as e:
            return f"[Search Error: {e}]"
