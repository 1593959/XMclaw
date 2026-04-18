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
            "Accept-Encoding": "gzip, deflate, br",
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
        
        # 解析结果 - 尝试多种选择器
        lines = []
        
        # 方法1: 标准 result div
        pattern1 = r'<div[^>]*class="result[^"]*"[^>]*>.*?<h3[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<p[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</p>'
        results = re.findall(pattern1, html, re.DOTALL)
        
        if not results:
            # 方法2: 更宽松的选择器
            pattern2 = r'<h3[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?</h3>.*?<p[^>]*>(.*?)</p>'
            results = re.findall(pattern2, html, re.DOTALL)
        
        if not results:
            # 方法3: 直接找链接和标题
            pattern3 = r'<a[^>]+class="[^"]*c-title[^"]*"[^>]*>(.*?)</a>'
            results = re.findall(pattern3, html, re.DOTALL)
            if results:
                for i, title_raw in enumerate(results[:max_results]):
                    title = re.sub(r'<[^>]+>', '', title_raw).strip()
                    if title:
                        lines.append(f"- {title[:150]}")
                return "\n".join(lines).strip()
        
        for link, title_raw, snippet_raw in results[:max_results]:
            title = re.sub(r'<[^>]+>', '', title_raw).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet_raw).strip()
            link = link.strip()
            
            if title:
                lines.append(f"- {title[:150]}")
            if snippet:
                lines.append(f"  {snippet[:300]}")
            if link:
                lines.append(f"  {link[:200]}")
            lines.append("")

        return "\n".join(lines).strip() if lines else "[No results found]"
