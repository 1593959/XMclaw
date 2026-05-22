import urllib.request, urllib.parse, ssl, re

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

queries = [
    "site:zhihu.com 陪玩店 运营 获客",
    "site:xiaohongshu.com 陪玩店 引流",
    "site:jianshu.com 游戏陪玩 创业",
    "陪玩工作室 抖音 小红书 引流 私域 2024",
]

with open('_search_results.txt', 'w', encoding='utf-8') as out:
    for q in queries:
        out.write(f"\n=== {q} ===\n")
        try:
            url = f'https://www.google.com/search?q={urllib.parse.quote(q)}&num=8'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'zh-CN,zh;q=0.9',
            })
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
                # Try multiple patterns
                results = []
                # Pattern 1: standard search result
                for m in re.finditer(r'<h3[^>]*>(.*?)</h3>.*?<a href="/url\?q=([^&"]+)', html, re.DOTALL):
                    title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    href = urllib.parse.unquote(m.group(2))
                    if title and href.startswith('http') and 'google.com' not in href:
                        results.append((title, href))
                # Pattern 2: alternative
                if not results:
                    for m in re.finditer(r'<a[^>]*href="(/url\?q=[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', html, re.DOTALL):
                        href = urllib.parse.unquote(re.search(r'q=([^&"]+)', m.group(1)).group(1))
                        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                        if title and href.startswith('http') and 'google.com' not in href:
                            results.append((title, href))
                for i, (t, u) in enumerate(results[:5], 1):
                    out.write(f'{i}. {t}\n   {u}\n')
                if not results:
                    out.write('(no results matched)\n')
        except Exception as e:
            out.write(f'Error: {e}\n')
print('done')
