from pathlib import Path

js_path = Path(r'C:\Users\15978\Desktop\XMclaw\web\main.js')
js = js_path.read_text(encoding='utf-8')

# 3. Modify ws.onmessage chunk handling
old_chunk = """        if (data.type === 'chunk') {
            removeTyping();
            if (!currentMessageEl) {
                currentMessageEl = addMessage('', 'agent');
            }
            appendChunk(currentMessageEl, data.content);
            scrollToBottom();
        }"""
new_chunk = """        if (data.type === 'chunk') {
            removeTyping();
            hideWelcome();
            if (!currentMessageEl) {
                currentMessageEl = addMessage('', 'agent');
            }
            appendChunk(currentMessageEl, data.content);
            scrollToBottom();
        }"""
js = js.replace(old_chunk, new_chunk)

# 4. Modify sendMessage
old_send = """function sendMessage() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    if (text === '[PLAN MODE]') {
        planMode = true;
        input.value = '';
        addMessage('已开启计划模式。下一条消息将生成执行计划。', 'system');
        return;
    }

    if (text.toLowerCase() === 'exit plan mode' || text === '退出计划模式') {
        planMode = false;
        input.value = '';
        addMessage('已退出计划模式。', 'system');
        return;
    }

    addMessage(text, 'user');
    input.value = '';
    input.style.height = 'auto';
    showTyping();
    currentMessageEl = null;

    const payload = planMode ? `[PLAN MODE] ${text}` : text;
    ws.send(JSON.stringify({ type: 'message', content: payload }));
}"""
new_send = """function sendMessage() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    if (text === '[PLAN MODE]') {
        planMode = true;
        input.value = '';
        addMessage('已开启计划模式。下一条消息将生成执行计划。', 'system');
        return;
    }

    if (text.toLowerCase() === 'exit plan mode' || text === '退出计划模式') {
        planMode = false;
        input.value = '';
        addMessage('已退出计划模式。', 'system');
        return;
    }

    hideWelcome();
    addMessage(text, 'user');
    input.value = '';
    input.style.height = 'auto';
    showTyping();
    currentMessageEl = null;
    messageBuffer = '';
    isStreaming = true;

    const payload = planMode ? `[PLAN MODE] ${text}` : text;
    ws.send(JSON.stringify({ type: 'message', content: payload }));
    saveCurrentSession();
}"""
js = js.replace(old_send, new_send)

# 5. Modify done handler to flush and save session
old_done = """        } else if (data.type === 'done') {
            removeTyping();
            currentMessageEl = null;
            setAgentState('IDLE', '等待输入...');
            activeTool.textContent = '—';
            activeFile.textContent = '—';
        }"""
new_done = """        } else if (data.type === 'done') {
            removeTyping();
            if (currentMessageEl) flushChunk(currentMessageEl);
            currentMessageEl = null;
            isStreaming = false;
            setAgentState('IDLE', '等待输入...');
            activeTool.textContent = '—';
            activeFile.textContent = '—';
            saveCurrentSession();
        }"""
js = js.replace(old_done, new_done)

# 6. Append new functions at the end
appendix = """

// ===== SESSION MANAGEMENT =====
function generateSessionId() {
    return 'sess_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 7);
}

function getSessionTitle() {
    const firstUser = chat.querySelector('.message-row.user .message');
    if (firstUser) {
        const text = firstUser.textContent.trim().slice(0, 24);
        return text || '新会话';
    }
    return '新会话';
}

function saveCurrentSession() {
    if (!currentSessionId) {
        currentSessionId = generateSessionId();
    }
    const html = chat.innerHTML;
    const existing = sessions.find(s => s.id === currentSessionId);
    if (existing) {
        existing.html = html;
        existing.title = getSessionTitle();
        existing.updated = Date.now();
    } else {
        sessions.unshift({ id: currentSessionId, title: getSessionTitle(), html: html, updated: Date.now() });
    }
    renderSessionList();
}

function renderSessionList() {
    const list = document.getElementById('session-list');
    if (!list) return;
    list.innerHTML = sessions.map(s => `
        <div class="session-item ${s.id === currentSessionId ? 'active' : ''}" data-id="${s.id}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <span style="overflow:hidden;text-overflow:ellipsis">${escapeHtml(s.title)}</span>
        </div>
    `).join('');
    list.querySelectorAll('.session-item').forEach(item => {
        item.addEventListener('click', () => switchSession(item.dataset.id));
    });
}

function switchSession(id) {
    saveCurrentSession();
    const s = sessions.find(x => x.id === id);
    if (!s) return;
    currentSessionId = id;
    chat.innerHTML = s.html || '';
    renderSessionList();
    if (chat.children.length === 0) showWelcome(); else hideWelcome();
}

function newSession() {
    saveCurrentSession();
    currentSessionId = generateSessionId();
    chat.innerHTML = '';
    showWelcome();
    renderSessionList();
    input.focus();
}

function clearChat() {
    chat.innerHTML = '';
    showWelcome();
    saveCurrentSession();
}

// ===== GENE/SKILL VIEWER =====
function openViewer(title, meta, code) {
    const modal = document.getElementById('viewer-modal');
    document.getElementById('viewer-title').textContent = title;
    document.getElementById('viewer-meta').innerHTML = meta.map(m => `<span>${escapeHtml(m)}</span>`).join('');
    const codeEl = document.querySelector('#viewer-code code');
    codeEl.textContent = code;
    codeEl.className = '';
    modal.style.display = 'flex';
    if (typeof hljs !== 'undefined') hljs.highlightElement(codeEl);
}

function closeViewer() {
    document.getElementById('viewer-modal').style.display = 'none';
}

document.getElementById('viewer-close')?.addEventListener('click', closeViewer);
document.getElementById('viewer-ok')?.addEventListener('click', closeViewer);
document.getElementById('viewer-copy')?.addEventListener('click', () => {
    const code = document.querySelector('#viewer-code code').textContent;
    navigator.clipboard.writeText(code).then(() => showToast('已复制到剪贴板'));
});

// ===== SHORTCUTS =====
document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
    } else if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
        e.preventDefault();
        clearChat();
    } else if (e.ctrlKey && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault();
        newSession();
    } else if (e.key === '/' && document.activeElement !== input) {
        e.preventDefault();
        input.focus();
    }
});

document.getElementById('btn-new-chat')?.addEventListener('click', newSession);

// ===== INIT =====
newSession();
"""

js += appendix
js_path.write_text(js, encoding='utf-8')
print('main.js updated')
