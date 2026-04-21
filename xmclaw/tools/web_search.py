"""Web search tool — uses httpx with browser-like headers + cookies to fetch Baidu search results."""
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
        try:
            return await self._baidu_search(query, max_results)
        except Exception as e:
            return f"[Search Error: {str(e)[:100]}]"

    async def _baidu_search(self, query: str, max_results: int) -> str:
        """Search using httpx + Baidu with browser-like request."""
        import httpx
        import time
        
        encoded = urllib.parse.quote(query)
        url = f"https://www.baidu.com/s?wd={encoded}"

        # 模拟浏览器请求
        timestamp = str(int(time.time() * 1000))
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            # NOTE: do NOT advertise 'br' — httpx can't decode brotli without
            # the optional `brotli` / `brotlicffi` package, and Baidu will
            # happily brotli-encode if we ask for it → `resp.text` becomes
            # garbled bytes and every selector silently misses.
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "max-age=0",
            "DNT": "1",
            "Referer": "https://www.baidu.com/",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Microsoft Edge";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Connection": "keep-alive",
        }
        
        cookies = {
            "BD_UPN": "12314753",
            "BAIDUID": f"ABCDEFG{timestamp}:FG=1",
            "PSTM": timestamp,
        }
        
        async with httpx.AsyncClient(
            timeout=20, 
            follow_redirects=True,
            headers=headers,
            cookies=cookies,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # 检查是否被拦截
        if "百度安全验证" in html or "安全验证" in html:
            return "[Search blocked by Baidu. Try again later or use a VPN.]"

        return self._parse_baidu_html(html, max_results)

    @staticmethod
    def _parse_baidu_html(html: str, max_results: int) -> str:
        """Parse Baidu's current result DOM.

        Since ~2024 Baidu wraps each organic hit in
        ``<div class="result c-container ..." mu="REAL_URL" ...>``. The
        previous regex-chain looked for ``<a class="c-title">`` which only
        exists in the CSS now, never in markup — so it silently returned
        "[No results found]" for every query. We now split the HTML by the
        opening ``result c-container`` position, then extract title + URL +
        a rough snippet per block.
        """
        starts = [m.start() for m in
                  re.finditer(r'<div[^>]*class="result\s+c-container', html)]
        if not starts:
            return "[No results found — Baidu DOM may have changed again]"

        lines: list[str] = []
        for i, pos in enumerate(starts[:max_results]):
            end = starts[i + 1] if i + 1 < len(starts) else min(pos + 8000, len(html))
            body = html[pos:end]

            mu = re.search(r'mu="([^"]+)"', body)
            url = mu.group(1).strip() if mu else ""

            h3 = re.search(r"<h3[^>]*>(.*?)</h3>", body, re.DOTALL)
            title = ""
            if h3:
                title = re.sub(r"<[^>]+>", " ", h3.group(1))
                title = re.sub(r"\s+", " ", title).strip()

            # Strip h3 + scripts + styles + comments, then text-ify for snippet.
            after_h3 = re.sub(r"<h3[^>]*>.*?</h3>", "", body, count=1, flags=re.DOTALL)
            s = re.sub(r"<script[^>]*>.*?</script>", "", after_h3, flags=re.DOTALL)
            s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.DOTALL)
            s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
            s = re.sub(r"<[^>]+>", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            s = (s.replace("&nbsp;", " ").replace("&quot;", '"')
                  .replace("&amp;", "&").replace("&#39;", "'"))

            if title:
                lines.append(f"- {title[:150]}")
            if s:
                lines.append(f"  {s[:280]}")
            if url:
                lines.append(f"  {url[:200]}")
            lines.append("")

        return "\n".join(lines).strip() if lines else "[No results found]"
