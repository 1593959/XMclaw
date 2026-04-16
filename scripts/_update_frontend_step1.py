import re
from pathlib import Path

BASE = Path(r'C:\Users\15978\Desktop\XMclaw\web')

# index.html
html_path = BASE / 'index.html'
html = html_path.read_text(encoding='utf-8')

head_insert = """
    <link rel=\"stylesheet\" href=\"https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css\">
    <script src=\"https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.0/marked.min.js\"></script>
    <script src=\"https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js\"></script>
"""
if 'marked.min.js' not in html:
    html = html.replace('</head>', head_insert + '</head>')

if 'session-list' not in html:
    html = re.sub(
        r'(</nav>)',
        r'''\1
        <div class=\"sidebar-section\">
            <div class=\"sidebar-section-header\">
                <span>会话历史</span>
                <button id=\"btn-new-chat\" class=\"sidebar-section-action\" title=\"新会话\">+</button>
            </div>
            <div class=\"session-list\" id=\"session-list\"></div>
        </div>''',
        html,
        count=1,
    )

welcome_html = """
                <div class=\"welcome-overlay\" id=\"welcome-overlay\">
                    <div class=\"welcome-icon\">
                        <svg width=\"40\" height=\"40\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.5\"><path d=\"M12 2L2 7l10 5 10-5-10-5z\"/><path d=\"M2 17l10 5 10-5\"/><path d=\"M2 12l10 5 10-5\"/></svg>
                    </div>
                    <h2>欢迎使用 XMclaw</h2>
                    <p>本地优先、自主进化的 AI Agent OS</p>
                    <div class=\"welcome-shortcuts\">
                        <div class=\"shortcut\"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> <span>发送消息</span></div>
                        <div class=\"shortcut\"><kbd>Ctrl</kbd> + <kbd>L</kbd> <span>清空聊天</span></div>
                        <div class=\"shortcut\"><kbd>Ctrl</kbd> + <kbd>N</kbd> <span>新会话</span></div>
                        <div class=\"shortcut\"><kbd>/</kbd> <span>聚焦输入框</span></div>
                    </div>
                    <div class=\"welcome-hints\">
                        <div class=\"hint\" onclick=\"setInput('帮我写一段 Python 代码')\">💡 帮我写一段 Python 代码</div>
                        <div class=\"hint\" onclick=\"setInput('分析当前项目结构')\">🔍 分析当前项目结构</div>
                        <div class=\"hint\" onclick=\"setInput('开启计划模式，规划一个功能')\">📋 开启计划模式，规划一个功能</div>
                    </div>
                </div>
"""
if 'welcome-overlay' not in html:
    html = html.replace('<div class=\"chat\" id=\"chat\">', '<div class=\"chat\" id=\"chat\">' + welcome_html)

viewer_modal = """
<!-- Gene/Skill Viewer Modal -->
<div class=\"modal\" id=\"viewer-modal\" style=\"display:none\">
    <div class=\"modal-content viewer-modal-content\">
        <div class=\"modal-header\">
            <h3 id=\"viewer-title\">详情</h3>
            <button class=\"panel-action\" id=\"viewer-close\">✕</button>
        </div>
        <div class=\"modal-body\">
            <div class=\"viewer-meta\" id=\"viewer-meta\"></div>
            <pre class=\"viewer-code\" id=\"viewer-code\"><code></code></pre>
        </div>
        <div class=\"modal-footer\">
            <button class=\"btn-secondary\" id=\"viewer-copy\">复制代码</button>
            <button class=\"btn-primary\" id=\"viewer-ok\">关闭</button>
        </div>
    </div>
</div>
"""
if 'viewer-modal' not in html:
    html = html.replace('</body>', viewer_modal + '</body>')

html_path.write_text(html, encoding='utf-8')
print('index.html updated')
