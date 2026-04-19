from pathlib import Path

BASE = Path(r'C:\Users\15978\Desktop\XMclaw\web')
js_path = BASE / 'main.js'
js = js_path.read_text(encoding='utf-8')

# 1. Add new globals after existing globals
old_globals = """let totalCost = 0;
let totalTokens = 0;
let geneCount = 0;
let skillCount = 0;
let toolHistory = [];
let selfModHistory = [];
let todos = [];
let planMode = false;

const AGENT_ID = 'default';"""

new_globals = """let totalCost = 0;
let totalTokens = 0;
let geneCount = 0;
let skillCount = 0;
let toolHistory = [];
let selfModHistory = [];
let todos = [];
let planMode = false;
let sessions = [];
let currentSessionId = null;
let messageBuffer = '';
let isStreaming = false;

const AGENT_ID = 'default';"""

js = js.replace(old_globals, new_globals)

# 2. Replace formatMessage + addMessage + appendChunk + addToolMessage + addToolResultMessage + addReflectionMessage
old_render = """function appendChunk(el, text) {
    let html = el.innerHTML;
    const escaped = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    html += escaped;
    el.innerHTML = formatMessage(html);
}

function formatMessage(html) {
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
    return html;
}

function addMessage(text, role) {
    const row = document.createElement('div');
    row.className = `message-row ${role}`;

    const el = document.createElement('div');
    el.className = `message ${role}`;
    if (role !== 'user') {
        el.innerHTML = formatMessage(text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
    } else {
        el.textContent = text;
    }

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
    return el;
}

function addToolMessage(tool, args) {
    const row = document.createElement('div');
    row.className = 'message-row tool';

    const el = document.createElement('div');
    el.className = 'message tool';
    const argsText = args ? JSON.stringify(args, null, 2) : '{}';
    el.innerHTML = `
        <div class="tool-header">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
            <span class="tool-name">${escapeHtml(tool)}</span>
        </div>
        <pre style="margin:0;background:transparent;padding:0;font-size:11px;border:none">${escapeHtml(argsText)}</pre>
    `;

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}

function addToolResultMessage(tool, result) {
    const row = document.createElement('div');
    row.className = 'message-row tool-result';

    const el = document.createElement('div');
    el.className = 'message tool-result';

    if (typeof result === 'string' && result.startsWith('data:image/')) {
        el.innerHTML = `
            <div class="tool-header">
                <span class="tool-name">${escapeHtml(tool)} result</span>
            </div>
            <img src="${result}" style="max-width:100%;border-radius:8px;margin-top:6px;border:1px solid var(--border)" alt="screenshot">
        `;
    } else {
        const text = String(result).slice(0, 800);
        el.innerHTML = `
            <div class="tool-header">
                <span class="tool-name">${escapeHtml(tool)} result</span>
            </div>
            <pre style="margin:0;background:transparent;padding:0;font-size:11px;border:none;white-space:pre-wrap">${escapeHtml(text)}${String(result).length > 800 ? '...' : ''}</pre>
        `;
    }

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}

function addReflectionMessage(data) {
    const summary = data.summary || 'Reflection';
    const problems = data.problems || [];
    const lessons = data.lessons || [];
    const improvements = data.improvements || [];

    const row = document.createElement('div');
    row.className = 'message-row reflection';

    const el = document.createElement('div');
    el.className = 'message reflection';

    let body = `<div style="font-weight:600;margin-bottom:4px">🧠 ${escapeHtml(summary)}</div>`;
    if (problems.length) {
        body += `<div style="color:#ff6b6b;font-size:11px;margin-top:4px">问题: ${escapeHtml(problems.join('; '))}</div>`;
    }
    if (lessons.length) {
        body += `<div style="color:#ffc107;font-size:11px;margin-top:4px">教训: ${escapeHtml(lessons.join('; '))}</div>`;
    }
    if (improvements.length) {
        body += `<div style="color:#00d4aa;font-size:11px;margin-top:4px">改进: ${escapeHtml(improvements.join('; '))}</div>`;
    }
    el.innerHTML = body;

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}"""

new_render = """function setInput(text) {
    input.value = text;
    input.focus();
    input.dispatchEvent(new Event('input'));
}

function hideWelcome() {
    const w = document.getElementById('welcome-overlay');
    if (w) w.style.display = 'none';
}

function showWelcome() {
    const w = document.getElementById('welcome-overlay');
    if (w) w.style.display = 'flex';
}

function renderMarkdown(text) {
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            gfm: true,
            breaks: true,
            headerIds: false,
            mangle: false
        });
        return marked.parse(text);
    }
    // fallback
    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');