from pathlib import Path

BASE = Path(r'C:\Users\15978\Desktop\XMclaw\web')
css_path = BASE / 'styles.css'
css = css_path.read_text(encoding='utf-8')

additions = """

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
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10px;
}
.welcome-hints {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-top: 8px;
}
.hint {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 12px;
    font-size: 12px;
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.12s;
}
.hint:hover { border-color: var(--accent); color: var(--text); background: var(--accent-subtle); }

/* ===== MARKDOWN RENDERING ===== */
.message-content { line-height: 1.55; }
.message-content p { margin: 0.5em 0; }
.message-content p:first-child { margin-top: 0; }
.message-content p:last-child { margin-bottom: 0; }
.message-content h1, .message-content h2, .message-content h3,
.message-content h4, .message-content h5, .message-content h6 {
    margin: 0.8em 0 0.4em;
    font-weight: 600;
    line-height: 1.3;
}
.message-content h1 { font-size: 18px; }
.message-content h2 { font-size: 16px; }
.message-content h3 { font-size: 15px; }
.message-content ul, .message-content ol {
    margin: 0.4em 0;
    padding-left: 1.4em;
}
.message-content li { margin: 0.2em 0; }
.message-content blockquote {
    margin: 0.6em 0;
    padding-left: 12px;
    border-left: 3px solid var(--accent);
    color: var(--text-dim);
}
.message-content table {
    border-collapse: collapse;
    margin: 0.6em 0;
    font-size: 13px;
}
.message-content th, .message-content td {
    border: 1px solid var(--border);
    padding: 6px 10px;
    text-align: left;
}
.message-content th { background: var(--surface-2); font-weight: 600; }
.message-content code {
    background: rgba(255,255,255,0.06);
    padding: 2px 5px;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.92em;
}
.message-content pre {
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
    overflow-x: auto;
    margin: 0.6em 0;
}
.message-content pre code {
    background: transparent;
    padding: 0;
    border-radius: 0;
    font-size: 12px;
    line-height: 1.5;
}
.message-content a { color: var(--accent); text-decoration: none; }
.message-content a:hover { text-decoration: underline; }

/* ===== VIEWER MODAL ===== */
.viewer-modal-content { max-width: 800px; width: 92vw; max-height: 86vh; }
.viewer-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
    margin-bottom: 10px;
    font-size: 12px;
    color: var(--text-dim);
}
.viewer-meta span {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 3px 8px;
}
.viewer-code {
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
    overflow: auto;
    max-height: 56vh;
    font-size: 12px;
    line-height: 1.5;
    margin: 0;
}
.viewer-code code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }

/* ===== GENE/SKILL LISTS IN EVOLUTION ===== */
.entity-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 8px;
}
.entity-item {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    transition: all 0.12s;
}
.entity-item:hover { border-color: var(--accent); background: var(--accent-subtle); }
.entity-name { font-weight: 500; font-size: 13px; }
.entity-type { font-size: 11px; color: var(--text-dim); background: rgba(255,255,255,0.04); padding: 2px 6px; border-radius: 4px; }
"""