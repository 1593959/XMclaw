const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');

const WS_URL = 'ws://127.0.0.1:8765/agent/default';
let ws = null;
let currentMessageEl = null;

function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        statusEl.textContent = 'Connected';
        statusEl.classList.add('connected');
    };

    ws.onclose = () => {
        statusEl.textContent = 'Disconnected';
        statusEl.classList.remove('connected');
        setTimeout(connect, 2000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'chunk') {
            if (!currentMessageEl) {
                currentMessageEl = addMessage('', 'agent');
            }
            currentMessageEl.textContent += data.content;
            scrollToBottom();
        } else if (data.type === 'done') {
            removeTyping();
            currentMessageEl = null;
        } else if (data.type === 'error') {
            removeTyping();
            addMessage(data.content, 'error');
            currentMessageEl = null;
        }
    };
}

function addMessage(text, role) {
    const el = document.createElement('div');
    el.className = `message ${role}`;
    el.textContent = text;
    chat.appendChild(el);
    scrollToBottom();
    return el;
}

function showTyping() {
    const el = document.createElement('div');
    el.className = 'typing';
    el.id = 'typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    chat.appendChild(el);
    scrollToBottom();
}

function removeTyping() {
    const el = document.getElementById('typing');
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

    ws.send(JSON.stringify({ role: 'user', content: text }));
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
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

connect();
