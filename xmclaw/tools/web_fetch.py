"""Web page content fetch tool."""
import httpx
from xmclaw.tools.base import Tool


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch the content of a web page or URL."
    parameters = {
        "url": {
            "type": "string",
            "description": "URL to fetch.",
        },
        "max_length": {
            "type": "integer",
            "description": "Maximum characters to return. Default 4000.",
        },
    }

    async def execute(self, url: str, max_length: int = 4000) -> str:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"
                }
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                content = resp.text
                # Simple HTML tag stripping for readability
                import re
                text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", content)
                text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:max_length] if len(text) > max_length else text
        except Exception as e:
            return f"[Fetch Error: {e}]"
