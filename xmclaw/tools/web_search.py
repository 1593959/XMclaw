"""Web search tool using Tavily."""
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
        if not api_key:
            return "[Error: TAVILY_API_KEY not set]"

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
                if not results:
                    return "No results found."
                lines = []
                for r in results:
                    lines.append(f"- {r.get('title')}: {r.get('url')}")
                    lines.append(f"  {r.get('content', '')[:200]}")
                return "\n".join(lines)
        except Exception as e:
            return f"[Search Error: {e}]"
