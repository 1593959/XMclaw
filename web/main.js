const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const topbarTitle = document.getElementById('topbar-title');
const modelBadge = document.getElementById('model-badge');
const tokenCount = document.getElementById('token-count');

const navItems = document.querySelectorAll('.nav-item');
const views = document.querySelectorAll('.view');

const WS_URL = 'ws://127.0.0.1:8765/agent/default';
let ws = null;
let currentMessageEl = null;
let currentView = 'chat';

// View switching
navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const view = item.dataset.view;
        switchView(view);
    });
});

function switchView(view) {
    currentView = view;
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === view));
    views.forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
    topbarTitle.textContent = view.charAt(0).toUpperCase() + view.slice(1);
}

// Settings
const settingProvider = document.getElementById('setting-provider');
const settingApiKey = document.getElementById('setting-apikey');
const settingModel = document.getElementById('setting-model');
const settingTemp = document.getElementById('setting-temp');
const tempValue = document.getElementById('temp-value');
const saveSettingsBtn = document.getElementById('save-settings');

settingTemp.addEventListener('input', () => {
    tempValue.textContent = settingTemp.value;
});

saveSettingsBtn.addEventListener('click', () => {
    const settings = {
        provider: settingProvider.value,
        apiKey: settingApiKey.value,
        model: settingModel.value,
        temperature: parseFloat(settingTemp.value)
    };
    localStorage.setItem('xmclaw_settings', JSON.stringify(settings));
    showToast('Settings saved');
    modelBadge.textContent = settings.model || settings.provider;
});

function loadSettings() {
    const raw = localStorage.getItem('xmclaw_settings');
    if (!raw) return;
    try {
        const s = JSON.parse(raw);
        if (s.provider) settingProvider.value = s.provider;
        if (s.apiKey) settingApiKey.value = s.apiKey;
        if (s.model) settingModel.value = s.model;
        if (s.temperature !== undefined) {
            settingTemp.value = s.temperature;
            tempValue.textContent = s.temperature;
        }
        modelBadge.textContent = s.model || s.provider || 'default';
    } catch {}
}

function showToast(msg) {
    const el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = `
        position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
        background: var(--accent); color: #000; padding: 10px 20px;
        border-radius: 8px; font-size: 13px; font-weight: 500; z-index: 1000;
    `;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2000);
}

// WebSocket
function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        statusDot.classList.add('connected');
        statusText.textContent = 'Connected';
        statusText.style.color = 'var(--accent)';
    };

    ws.onclose = () => {
        statusDot.classList.remove('connected');
        statusText.textContent = 'Reconnecting...';
        statusText.style.color = 'var(--text-dim)';
        setTimeout(connect, 2000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'chunk') {
            removeTyping();
            if (!currentMessageEl) {
                currentMessageEl = addMessage('', 'agent');
            }
            appendChunk(currentMessageEl, data.content);
            scrollToBottom();
        } else if (data.type === 'tool_call') {
            removeTyping();
            addToolMessage(data.tool, data.args);
            scrollToBottom();
        } else if (data.type === 'done') {
            removeTyping();
            currentMessageEl = null;
        } else if (data.type === 'error') {
            removeTyping();
            addMessage(data.content, 'error');
            currentMessageEl = null;
        } else if (data.type === 'cost') {
            tokenCount.textContent = `${data.tokens || '-'} tokens · $${data.cost || '-'}`;
        }
    };
}

function appendChunk(el, text) {
    // Simple markdown-like formatting for code blocks
    let html = el.innerHTML;
    const escaped = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    html += escaped;
    el.innerHTML = formatMessage(html);
}

function formatMessage(html) {
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Code blocks
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
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
            <span class="tool-name">${escapeHtml(tool)}</span>
        </div>
        <pre style="margin:0;background:transparent;padding:0;font-size:12px">${escapeHtml(argsText)}</pre>
    `;

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}

function escapeHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function showTyping() {
    const row = document.createElement('div');
    row.className = 'message-row agent';
    row.id = 'typing-row';

    const el = document.createElement('div');
    el.className = 'typing';
    el.innerHTML = '<span></span><span></span><span></span>';

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}

function removeTyping() {
    const el = document.getElementById('typing-row');
    if (el) el.remove();
}

function scrollToBottom() {
    chat.scrollTop = chat.scrollHeight;
}

function sendMessage() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    addMessage(text, 'user');
    input.value = '';
    input.style.height = 'auto';
    showTyping();
    currentMessageEl = null;

    const settings = localStorage.getItem('xmclaw_settings');
    const payload = { role: 'user', content: text };
    if (settings) {
        try {
            payload.settings = JSON.parse(settings);
        } catch {}
    }

    ws.send(JSON.stringify(payload));
}

sendBtn.addEventListener('click', sendMessage);
input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
});

loadSettings();
connect();