"""Update frontend with Markdown, code highlighting, gene/skill viewer, session history, welcome page, shortcuts."""
import re
from pathlib import Path

BASE = Path(r"C:\Users\15978\Desktop\XMclaw\web")

# ========== index.html ==========
html_path = BASE / "index.html"
html = html_path.read_text(encoding="utf-8")

# Add marked + highlight.js in <head>
head_insert = """
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.0/marked.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
"""
if "marked.min.js" not in html:
    html = html.replace('</head>', head_insert + '</head>')

# Add session history section after </nav>
if "session-list" not in html:
    html = re.sub(
        r'(</nav>)',
        r'''\1
        <div class="sidebar-section">
            <div class="sidebar-section-header">
                <span>会话历史</span>
                <button id="btn-new-chat" class="sidebar-section-action" title="新会话">+</button>
            </div>
            <div class="session-list" id="session-list"></div>
        </div>''',
        html,
        count=1,
    )

# Add welcome overlay in chat
welcome_html = """
                <div class="welcome-overlay" id="welcome-overlay">
                    <div class="welcome-icon">
                        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
                    </div>
                    <h2>欢迎使用 XMclaw</h2>
                    <p>本地优先、自主进化的 AI Agent OS</p>
                    <div class="welcome-shortcuts">
                        <div class="shortcut"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> <span>发送消息</span></div>
                        <div class="shortcut"><kbd>Ctrl</kbd> + <kbd>L</kbd> <span>清空聊天</span></div>
                        <div class="shortcut"><kbd>Ctrl</kbd> + <kbd>N</kbd> <span>新会话</span></div>
                        <div class="shortcut"><kbd>/</kbd> <span>聚焦输入框</span></div>
                    </div>
                    <div class="welcome-hints">
                        <div class="hint" onclick="setInput('帮我写一段 Python 代码')">💡 帮我写一段 Python 代码</div>
                        <div class="hint" onclick="setInput('分析当前项目结构')">🔍 分析当前项目结构</div>
                        <div class="hint" onclick="setInput('开启计划模式，规划一个功能')">📋 开启计划模式，规划一个功能</div>
                    </div>
                </div>
"""
if "welcome-overlay" not in html:
    html = html.replace('<div class="chat" id="chat">', '<div class="chat" id="chat">' + welcome_html)

# Add gene/skill viewer modal
viewer_modal = """
<!-- Gene/Skill Viewer Modal -->
<div class="modal" id="viewer-modal" style="display:none">
    <div class="modal-content viewer-modal-content">
        <div class="modal-header">
            <h3 id="viewer-title">详情</h3>
            <button class="panel-action" id="viewer-close">✕</button>
        </div>
        <div class="modal-body">
            <div class="viewer-meta" id="viewer-meta"></div>
            <pre class="viewer-code" id="viewer-code"><code></code></pre>
        </div>
        <div class="modal-footer">
            <button class="btn-secondary" id="viewer-copy">复制代码</button>
            <button class="btn-primary" id="viewer-ok">关闭</button>
        </div>
    </div>
</div>
"""
if "viewer-modal" not in html:
    html = html.replace('</body>', viewer_modal + '</body>')

html_path.write_text(html, encoding="utf-8")
print("index.html updated")

# ========== styles.css additions ==========
css_path = BASE / "styles.css"
css = css_path.read_text(encoding="utf-8")

css_additions = """

/* ===== SESSION HISTORY ===== */
.sidebar-section {
    border-top: 1px solid var(--border);
    padding: 10px 0;
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
}
.sidebar-section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 14px 8px;
    font-size: 10px;
    font-weight: 700;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.sidebar-section-action {
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: 4px;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.12s;
}
.sidebar-section-action:hover { border-color: var(--accent); color: var(--accent); }
.session-list {
    flex: 1;
    overflow-y: auto;
    padding: 0 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.session-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 9px;
    border-radius: var(--radius-xs);
    font-size: 12px;
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.1s;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.session-item:hover { background: rgba(255,255,255,0.04); color: var(--text); }
.session-item.active { background: var(--accent-subtle); color: var(--accent); }
.session-item svg { flex-shrink: 0; opacity: 0.7; }

/* ===== WELCOME OVERLAY ===== */
.welcome-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 24px;
    gap: 14px;
    z-index: 5;
}
.welcome-icon {
    width: 64px;
    height: 64px;
    border-radius: 16px;
    background: linear-gradient(135deg, var(--accent) 0%, var(--purple) 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    box-shadow: 0 0 24px var(--accent-glow);
}
.welcome-overlay h2 { font-size: 22px; font-weight: 600; }
.welcome-overlay > p { color: var(--text-dim); font-size: 13px; }
.welcome-shortcuts {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px 14px;
    margin-top: 4px;
}
.shortcut { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-dim); }
.shortcut kbd {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 5px;
    font-family