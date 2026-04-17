"""Web search tool using browser automation."""
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
        """Use 360 search to find information."""
        from playwright.async_api import async_playwright
        import urllib.parse
        
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.so.com/s?q={encoded_query}"
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            try:
                await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                
                lines = []
                
                # 360 search result selectors
                results = await page.query_selector_all('li.res-list')
                
                for i, r in enumerate(results[:max_results]):
                    try:
                        # Get title
                        title_el = await r.query_selector('h3 a, h3')
                        title = await title_el.inner_text() if title_el else ""
                        
                        # Get link
                        link = ""
                        link_el = await r.query_selector('h3 a')
                        if link_el:
                            link = await link_el.get_attribute('href') or ""
                        
                        # Get snippet
                        snippet_el = await r.query_selector('p.res-desc, p')
                        snippet = await snippet_el.inner_text() if snippet_el else ""
                        
                        if title:
                            lines.append(f"- {title[:100]}")
                        if link:
                            lines.append(f"  {link[:150]}")
                        if snippet:
                            lines.append(f"  {snippet[:200]}")
                    except:
                        pass
                
                await browser.close()
                
                if lines:
                    return "\n".join(lines)
                else:
                    return "No search results found."
                    
            except Exception as e:
                await browser.close()
                return f"[Search Error: {e}]"
