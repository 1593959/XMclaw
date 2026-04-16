const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const topbarTitle = document.getElementById('topbar-title');
const modelBadge = document.getElementById('model-badge');
const costDisplay = document.getElementById('cost-display');
const tokenDisplay = document.getElementById('token-display');
const evolutionCount = document.getElementById('evolution-count');
const todoCount = document.getElementById('todo-count');

const stateBadge = document.getElementById('state-badge');
const currentThought = document.getElementById('current-thought');
const activeTool = document.getElementById('active-tool');
const activeFile = document.getElementById('active-file');
const recentTools = document.getElementById('recent-tools');
const selfModLog = document.getElementById('self-mod-log');
const clearToolsBtn = document.getElementById('clear-tools');
const testTarget = document.getElementById('test-target');
const btnTestGenerate = document.getElementById('btn-test-generate');
const btnTestRun = document.getElementById('btn-test-run');
const btnTestRunAll = document.getElementById('btn-test-runall');
const testOutput = document.getElementById('test-output');

const navItems = document.querySelectorAll('.nav-item');
const views = document.querySelectorAll('.view');

const WS_URL = 'ws://127.0.0.1:8765/agent/default';
let ws = null;
let currentMessageEl = null;
let currentView = 'dashboard';

let totalCost = 0;
let totalTokens = 0;
let geneCount = 0;
let skillCount = 0;
let toolHistory = [];
let selfModHistory = [];
let todos = [];
let planMode = false;

const AGENT_ID = 'default';

// View switching
navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        switchView(item.dataset.view);
    });
});

function switchView(view) {
    currentView = view;
    navItems.forEach(n => n.classList.toggle('active', n.dataset.view === view));
    views.forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
    const viewNames = {
        dashboard: '仪表盘',
        workspace: '工作区',
        evolution: '进化',
        memory: '记忆',
        tools: '工具日志',
        agents: '多代理',
        settings: '设置'
    };
    topbarTitle.textContent = viewNames[view] || view;
    if (view === 'workspace') loadWorkspaceFiles();
    if (view === 'evolution') loadEvolutionStatus();
    if (view === 'memory') loadMemorySearch();
    if (view === 'tools') loadToolsLogs();
    if (view === 'agents') loadAgentsView();
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

async function loadDaemonConfig() {
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        const llm = cfg.llm || {};
        const provider = llm.default_provider || 'anthropic';
        settingProvider.value = provider;
        if (provider === 'anthropic') {
            settingApiKey.value = (llm.anthropic || {}).api_key || '';
            settingModel.value = (llm.anthropic || {}).default_model || '';
        } else {
            settingApiKey.value = (llm.openai || {}).api_key || '';
            settingModel.value = (llm.openai || {}).default_model || '';
        }
        modelBadge.textContent = settingModel.value || provider;

        // MCP config
        renderMCPList(cfg.mcp_servers || {});

        localStorage.setItem('xmclaw_settings', JSON.stringify({
            provider,
            apiKey: settingApiKey.value,
            model: settingModel.value
        }));
    } catch {}
}

saveSettingsBtn.addEventListener('click', async () => {
    const provider = settingProvider.value;
    const apiKey = settingApiKey.value;
    const model = settingModel.value;
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        cfg.llm = cfg.llm || {};
        cfg.llm.default_provider = provider;
        if (provider === 'anthropic') {
            cfg.llm.anthropic = cfg.llm.anthropic || {};
            cfg.llm.anthropic.api_key = apiKey;
            cfg.llm.anthropic.default_model = model;
        } else {
            cfg.llm.openai = cfg.llm.openai || {};
            cfg.llm.openai.api_key = apiKey;
            cfg.llm.openai.default_model = model;
        }
        cfg.mcp_servers = collectMCPConfig();
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg)
        });
        localStorage.setItem('xmclaw_settings', JSON.stringify({ provider, apiKey, model }));
        showToast('设置已保存到 Daemon');
        modelBadge.textContent = model || provider;
    } catch (e) {
        showToast('保存失败: ' + e.message);
    }
});

function loadSettings() {
    loadDaemonConfig();
    document.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById('stab-' + tab.dataset.stab).classList.add('active');
        });
    });
    // memory search listeners are set up globally
    initMCPUI();
}

// ===== MCP CONFIG UI =====
let mcpServers = {};

function initMCPUI() {
    document.getElementById('mcp-add')?.addEventListener('click', () => addMCPEntry());
}

function renderMCPList(servers) {
    mcpServers = { ...servers };
    const list = document.getElementById('mcp-list');
    if (!list) return;
    list.innerHTML = '';
    Object.entries(servers).forEach(([name, cfg]) => {
        addMCPEntry(name, cfg);
    });
}

function addMCPEntry(name = '', cfg = { command: 'npx', args: [], env: {} }) {
    const list = document.getElementById('mcp-list');
    if (!list) return;
    const id = 'mcp_' + Math.random().toString(36).slice(2, 9);
    const div = document.createElement('div');
    div.className = 'mcp-server-item';
    div.dataset.mcpId = id;
    div.innerHTML = `
        <div style="display:flex;gap:8px;flex:1;align-items:center">
            <input type="text" placeholder="名称" value="${escapeHtml(name)}" class="mcp-name" style="width:120px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:12px">
            <input type="text" placeholder="命令" value="${escapeHtml(cfg.command || '')}" class="mcp-cmd" style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:12px">
            <input type="text" placeholder="参数 (逗号分隔)" value="${escapeHtml((cfg.args || []).join(','))}" class="mcp-args" style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:12px">
        </div>
        <button class="mcp-del" style="background:transparent;border:none;color:#ff6b6b;cursor:pointer;font-size:14px">×</button>
    `;
    div.querySelector('.mcp-del').addEventListener('click', () => div.remove());
    list.appendChild(div);
}

function collectMCPConfig() {
    const servers = {};
    document.querySelectorAll('#mcp-list .mcp-server-item').forEach(item => {
        const name = item.querySelector('.mcp-name')?.value?.trim();
        const cmd = item.querySelector('.mcp-cmd')?.value?.trim();
        const argsStr = item.querySelector('.mcp-args')?.value?.trim() || '';
        if (!name || !cmd) return;
        servers[name] = {
            command: cmd,
            args: argsStr ? argsStr.split(',').map(s => s.trim()) : [],
            env: {}
        };
    });
    return servers;
}

function showToast(msg) {
    const el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = `
        position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
        background: var(--accent); color: #000; padding: 10px 18px;
        border-radius: 8px; font-size: 12px; font-weight: 600; z-index: 1000;
    `;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1800);
}

// Plan Mode
const togglePlanBtn = document.getElementById('toggle-plan');
const planModeBar = document.getElementById('plan-mode-bar');
const cancelPlanBtn = document.getElementById('cancel-plan');

togglePlanBtn.addEventListener('click', () => {
    planMode = !planMode;
    togglePlanBtn.classList.toggle('active', planMode);
    planModeBar.style.display = planMode ? 'flex' : 'none';
});

cancelPlanBtn.addEventListener('click', () => {
    planMode = false;
    togglePlanBtn.classList.remove('active');
    planModeBar.style.display = 'none';
});

// Agent state updates
function setAgentState(state, thought) {
    stateBadge.textContent = state;
    if (thought) currentThought.textContent = thought;
    if (state === 'THINKING') {
        stateBadge.style.background = 'var(--info-bg)';
        stateBadge.style.color = '#2196f3';
    } else if (state === 'TOOL_CALL') {
        stateBadge.style.background = 'var(--warn-bg)';
        stateBadge.style.color = '#ffc107';
    } else if (state === 'SELF_MOD') {
        stateBadge.style.background = 'var(--accent-weak)';
        stateBadge.style.color = 'var(--accent)';
    } else {
        stateBadge.style.background = 'var(--accent-weak)';
        stateBadge.style.color = 'var(--accent)';
    }
}

function addToolCall(tool, args, result) {
    const entry = { tool, args, result, time: new Date().toLocaleTimeString() };
    toolHistory.unshift(entry);
    if (toolHistory.length > 20) toolHistory.pop();
    renderRecentTools();
    renderToolLog();
}

function addSelfMod(file, action) {
    const entry = { file, action, time: new Date().toLocaleTimeString() };
    selfModHistory.unshift(entry);
    if (selfModHistory.length > 20) selfModHistory.pop();
    renderSelfModLog();
    addTimelineEvent('self_mod', `Self-modification: ${action}`, file);
}

function renderRecentTools() {
    if (toolHistory.length === 0) {
        recentTools.innerHTML = '<div class="empty-state">No tools used yet</div>';
        return;
    }
    recentTools.innerHTML = toolHistory.slice(0, 5).map(t => `
        <div class="state-item" style="margin-bottom:10px">
            <div class="state-value code" style="font-size:11px">
                <span style="color:var(--accent)">${escapeHtml(t.tool)}</span>(${formatArgs(t.args)})
            </div>
            <div class="state-label" style="margin-top:3px;font-size:10px">${t.time}</div>
        </div>
    `).join('');
}

function renderSelfModLog() {
    if (selfModHistory.length === 0) {
        selfModLog.innerHTML = '<div class="empty-state">No self-modifications yet</div>';
        return;
    }
    selfModLog.innerHTML = selfModHistory.slice(0, 5).map(m => `
        <div class="state-item" style="margin-bottom:10px">
            <div class="state-value" style="font-size:11px">${escapeHtml(m.action)}</div>
            <div class="state-label" style="margin-top:3px;font-size:10px">${escapeHtml(m.file)} · ${m.time}</div>
        </div>
    `).join('');
}

function formatArgs(args) {
    if (!args) return '';
    try {
        const a = typeof args === 'string' ? JSON.parse(args) : args;
        const keys = Object.keys(a).slice(0, 2);
        return keys.map(k => `${k}=${String(a[k]).slice(0, 20)}`).join(', ');
    } catch {
        return String(args).slice(0, 40);
    }
}

clearToolsBtn.addEventListener('click', () => {
    toolHistory = [];
    renderRecentTools();
    renderToolLog();
});

async function runTestAction(action, target = '') {
    testOutput.style.display = 'block';
    testOutput.textContent = '执行中...';
    try {
        const payload = { action };
        if (target) payload.target = target;
        const res = await fetch('/api/agent/default/tools/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        testOutput.textContent = data.result || data.error || '无输出';
    } catch (e) {
        testOutput.textContent = '请求失败: ' + e.message;
    }
}

btnTestGenerate.addEventListener('click', () => runTestAction('generate', testTarget.value.trim()));
btnTestRun.addEventListener('click', () => runTestAction('run', testTarget.value.trim()));
btnTestRunAll.addEventListener('click', () => runTestAction('run_all'));

// Todos
const todoList = document.getElementById('todo-list');
const addTodoBtn = document.getElementById('add-todo');

async function loadTodos() {
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/todos`);
        todos = await res.json();
        renderTodos();
    } catch {
        todoList.innerHTML = '<div class="empty-state">Failed to load todos</div>';
    }
}

function renderTodos() {
    todoCount.textContent = `${todos.filter(t => !t.done).length} Todos`;
    if (todos.length === 0) {
        todoList.innerHTML = '<div class="empty-state">No todos</div>';
        return;
    }
    todoList.innerHTML = todos.map((t, i) => `
        <div class="todo-item ${t.done ? 'done' : ''}">
            <input type="checkbox" ${t.done ? 'checked' : ''} onchange="toggleTodo(${i})">
            <span>${escapeHtml(t.text)}</span>
            <button onclick="deleteTodo(${i})">×</button>
        </div>
    `).join('');
}

window.toggleTodo = async function(idx) {
    todos[idx].done = !todos[idx].done;
    renderTodos();
    await saveTodos();
};

window.deleteTodo = async function(idx) {
    todos.splice(idx, 1);
    renderTodos();
    await saveTodos();
};

async function saveTodos() {
    try {
        await fetch(`/api/agent/${AGENT_ID}/todos`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(todos)
        });
    } catch {}
}

addTodoBtn.addEventListener('click', async () => {
    const text = prompt('新待办事项：');
    if (!text) return;
    todos.push({ id: Date.now(), text, done: false });
    renderTodos();
    await saveTodos();
});

// Tasks
const taskList = document.getElementById('task-list');
const addTaskBtn = document.getElementById('add-task');
let tasks = [];

async function loadTasks() {
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/tasks`);
        tasks = await res.json();
        renderTasks();
    } catch {
        taskList.innerHTML = '<div class="empty-state">Failed to load tasks</div>';
    }
}

function renderTasks() {
    if (tasks.length === 0) {
        taskList.innerHTML = '<div class="empty-state">No tasks</div>';
        return;
    }
    taskList.innerHTML = tasks.map((t, i) => `
        <div class="task-item" style="padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px">
            <div style="display: flex; align-items: center; gap: 8px">
                <span style="padding: 2px 6px; border-radius: 4px; background: ${statusColor(t.status)}; color: #000; font-size: 10px; font-weight: 600; text-transform: uppercase">${t.status}</span>
                <span style="flex: 1">${escapeHtml(t.title)}</span>
                <button onclick="deleteTask(${i})" style="background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 14px">×</button>
            </div>
        </div>
    `).join('');
}

function statusColor(status) {
    const map = {
        pending: '#f59e0b',
        in_progress: '#3b82f6',
        completed: '#10b981',
        failed: '#ef4444',
    };
    return map[status] || '#6b7280';
}

window.deleteTask = async function(idx) {
    tasks.splice(idx, 1);
    renderTasks();
    try {
        await fetch(`/api/agent/${AGENT_ID}/tasks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(tasks)
        });
    } catch {}
};

addTaskBtn.addEventListener('click', async () => {
    const text = prompt('新任务标题：');
    if (!text) return;
    tasks.push({ id: Date.now().toString(), title: text, description: '', status: 'pending', created_at: new Date().toISOString(), updated_at: new Date().toISOString() });
    renderTasks();
    try {
        await fetch(`/api/agent/${AGENT_ID}/tasks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(tasks)
        });
    } catch {}
});

// Timeline
function addTimelineEvent(type, title, desc) {
    const timeline = document.getElementById('evolution-timeline');
    const empty = timeline.querySelector('.empty-state');
    if (empty) empty.remove();

    const iconMap = {
        gene: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4"/><path d="m16.2 7.8 2.9-2.9"/></svg>',
        skill: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
        validation: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
        self_mod: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
        default: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>'
    };

    const iconClass = type === 'validation_fail' ? 'fail' : type === 'adl_warn' ? 'warn' : '';
    const item = document.createElement('div');
    item.className = 'timeline-item';
    item.innerHTML = `
        <div class="timeline-icon ${iconClass}">${iconMap[type] || iconMap.default}</div>
        <div class="timeline-content">
            <div class="timeline-title">${escapeHtml(title)}</div>
            <div class="timeline-desc">${escapeHtml(desc)}</div>
            <div class="timeline-meta">${new Date().toLocaleString()}</div>
        </div>
    `;
    timeline.insertBefore(item, timeline.firstChild);
}

// Tool Log page
function renderToolLog() {
    const log = document.getElementById('tool-log');
    if (!log) return;
    if (toolHistory.length === 0) {
        log.innerHTML = '<div class="empty-state">No tool executions yet.</div>';
        return;
    }
    log.innerHTML = toolHistory.map((t, i) => `
        <div class="tool-log-entry">
            <div class="tool-log-header" onclick="toggleToolLog(${i})">
                <span class="tool-log-name">${escapeHtml(t.tool)}</span>
                <span class="tool-log-time">${t.time}</span>
            </div>
            <div class="tool-log-body" id="tool-log-${i}" style="display:none">
Args:
${escapeHtml(JSON.stringify(t.args, null, 2))}

Result:
${escapeHtml(t.result || '(pending)')}
            </div>
        </div>
    `).join('');
}

window.toggleToolLog = function(idx) {
    const el = document.getElementById(`tool-log-${idx}`);
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
};

// Workspace
const fileTree = document.getElementById('file-tree');
const workspaceEditor = document.getElementById('workspace-editor');
const editorPath = document.getElementById('editor-path');
const saveFileBtn = document.getElementById('save-file-btn');
let currentFilePath = null;
let workspaceFiles = [];

async function loadWorkspaceFiles() {
    fileTree.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/files`);
        const data = await res.json();
        workspaceFiles = data.files || [];
        renderFileTree();
    } catch {
        fileTree.innerHTML = '<div class="empty-state">Failed to load files</div>';
    }
}

function renderFileTree() {
    if (workspaceFiles.length === 0) {
        fileTree.innerHTML = '<div class="empty-state">No files</div>';
        return;
    }
    fileTree.innerHTML = workspaceFiles.map(f => `
        <div class="file-tree-item ${f.path === currentFilePath ? 'active' : ''}" onclick="openWorkspaceFile('${f.path}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            <span style="overflow:hidden;text-overflow:ellipsis">${escapeHtml(f.path)}</span>
        </div>
    `).join('');
}

window.openWorkspaceFile = async function(path) {
    currentFilePath = path;
    renderFileTree();
    editorPath.textContent = path;
    workspaceEditor.value = '加载中...';
    workspaceEditor.readOnly = true;
    saveFileBtn.style.display = 'none';
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        workspaceEditor.value = data.content || '';
        workspaceEditor.readOnly = false;
        saveFileBtn.style.display = 'inline-block';
    } catch {
        workspaceEditor.value = '加载文件失败';
    }
};

saveFileBtn.addEventListener('click', async () => {
    if (!currentFilePath) return;
    try {
        await fetch(`/api/agent/${AGENT_ID}/file?path=${encodeURIComponent(currentFilePath)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: workspaceEditor.value })
        });
        showToast('文件已保存');
    } catch {
        showToast('保存文件失败');
    }
});

// Memory search
const memorySearch = document.getElementById('memory-query');
const memorySearchBtn = document.getElementById('memory-search-btn');
const memoryResults = document.getElementById('memory-results');

memorySearchBtn?.addEventListener('click', doMemorySearch);
memorySearch?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doMemorySearch();
});

async function doMemorySearch() {
    const q = memorySearch.value.trim();
    if (!q) return;
    memoryResults.innerHTML = '<div class="empty-state">Searching...</div>';
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/memory/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        const results = data.results || [];
        if (results.length === 0) {
            memoryResults.innerHTML = '<div class="empty-state">No results found.</div>';
            return;
        }
        memoryResults.innerHTML = results.map(r => `
            <div class="memory-card">
                <div class="memory-card-header">
                    <span class="memory-card-type">${escapeHtml(r.file)}</span>
                </div>
                <div class="memory-card-body"><pre style="background:#0a0a0a;padding:10px;border-radius:6px;font-size:12px">${escapeHtml(r.snippet)}</pre></div>
            </div>
        `).join('');
    } catch {
        memoryResults.innerHTML = '<div class="empty-state">Search failed.</div>';
    }
}

// Evolution status
async function loadEvolutionStatus() {
    try {
        const res = await fetch('/api/evolution/status');
        const data = await res.json();
        geneCount = data.gene_count || 0;
        skillCount = data.skill_count || 0;
        evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        const evoContent = document.getElementById('evolution-content');
        if (evoContent) {
            let html = `<div class="evo-cards">
                <div class="evo-card"><div class="evo-num">${geneCount}</div><div class="evo-label">Genes</div></div>
                <div class="evo-card"><div class="evo-num">${skillCount}</div><div class="evo-label">Skills</div></div>
                <div class="evo-card"><div class="evo-num">${data.scheduler_running ? '运行中' : '已停止'}</div><div class="evo-label">调度器</div></div>
            </div>`;

            // Genes list
            if (data.genes && data.genes.length) {
                html += '<h4 style="margin:14px 0 6px;font-size:13px;color:var(--text-dim)">Genes</h4>';
                html += '<div class="entity-list">';
                for (const g of data.genes) {
                    html += `<div class="entity-item" onclick="loadEntity('gene','${g.name}')">
                        <span class="entity-name">${escapeHtml(g.name)}</span>
                        <span class="entity-type">Gene</span>
                    </div>`;
                }
                html += '</div>';
            }

            // Skills list
            if (data.skills && data.skills.length) {
                html += '<h4 style="margin:14px 0 6px;font-size:13px;color:var(--text-dim)">Skills</h4>';
                html += '<div class="entity-list">';
                for (const s of data.skills) {
                    html += `<div class="entity-item" onclick="loadEntity('skill','${s.name}')">
                        <span class="entity-name">${escapeHtml(s.name)}</span>
                        <span class="entity-type">Skill</span>
                    </div>`;
                }
                html += '</div>';
            }

            if (data.logs && data.logs.length) {
                html += '<div class="evo-logs"><h4>最近日志</h4>';
                for (const log of data.logs.slice(0, 5)) {
                    html += `<div class="evo-log-item"><strong>${escapeHtml(log.name)}</strong><pre>${escapeHtml(log.content.slice(-800))}</pre></div>`;
                }
                html += '</div>';
            }
            evoContent.innerHTML = html;
        }
    } catch (e) {
        console.error('loadEvolutionStatus error', e);
    }
}

async function loadEntity(type, name) {
    try {
        const res = await fetch(`/api/evolution/entity/${type}/${name}`);
        const data = await res.json();
        if (data.content) {
            openViewer(`${name} (${type})`, [type, name], data.content);
        } else {
            showToast('加载失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        console.error('loadEntity error', e);
        showToast('加载失败');
    }
}

// WebSocket
function connect() {
    console.log('[DEBUG] connect() called, WS_URL =', WS_URL);
    ws = new WebSocket(WS_URL);
    console.log('[DEBUG] WebSocket created, readyState =', ws.readyState);

    ws.onopen = () => {
        statusDot.classList.add('connected');
        statusText.textContent = '已连接';
        statusText.style.color = 'var(--accent)';
        loadTodos();
        loadTasks();
        loadEvolutionStatus();
    };

    ws.onclose = () => {
        statusDot.classList.remove('connected');
        statusText.textContent = '重新连接中...';
        statusText.style.color = 'var(--text-dim)';
        setTimeout(connect, 2000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'chunk') {
            removeTyping();
            hideWelcome();
            if (!currentMessageEl) {
                currentMessageEl = addMessage('', 'agent');
            }
            appendChunk(currentMessageEl, data.content);
            scrollToBottom();
        } else if (data.type === 'tool_call') {
            removeTyping();
            setAgentState('TOOL_CALL', `Using ${data.tool}...`);
            activeTool.textContent = `${data.tool}(${formatArgs(data.args)})`;
            addToolCall(data.tool, data.args, data.result);
            addToolMessage(data.tool, data.args);
            scrollToBottom();
        } else if (data.type === 'tool_result') {
            if (toolHistory.length > 0) {
                toolHistory[0].result = data.result;
                renderRecentTools();
                renderToolLog();
            }
            addToolResultMessage(toolHistory[0]?.tool || 'tool', data.result);
            activeTool.textContent = '—';
            setAgentState('THINKING', '处理结果中...');
        } else if (data.type === 'file_op') {
            setAgentState('SELF_MOD', `Modified ${data.file || 'file'}`);
            activeFile.textContent = `${data.action || 'write'}: ${data.file || '-'}`;
            addSelfMod(data.file, data.action);
            if (currentView !== 'dashboard') {
                showToast(`Self-mod: ${data.action} ${data.file}`);
            }
        } else if (data.type === 'state') {
            setAgentState(data.state, data.thought);
        } else if (data.type === 'done') {
            removeTyping();
            if (currentMessageEl) flushChunk(currentMessageEl);
            currentMessageEl = null;
            isStreaming = false;
            setAgentState('IDLE', '等待输入...');
            activeTool.textContent = '—';
            activeFile.textContent = '—';
            saveCurrentSession();
        } else if (data.type === 'error') {
            removeTyping();
            addMessage(data.content, 'error');
            currentMessageEl = null;
            setAgentState('IDLE', '发生错误');
        } else if (data.type === 'cost') {
            totalTokens += data.tokens || 0;
            totalCost += data.cost || 0;
            tokenDisplay.textContent = `${totalTokens.toLocaleString()} tokens`;
            costDisplay.textContent = `$${totalCost.toFixed(4)}`;
        } else if (data.type === 'evolution') {
            if (data.gene) geneCount++;
            if (data.skill) skillCount++;
            evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
            addTimelineEvent(data.subtype || 'gene', data.title, data.desc);
        } else if (data.type === 'reflection') {
            addReflectionMessage(data.data || {}, data.improvement || {});
        } else if (data.type === 'ask_user') {
            showAskUserDialog(data.question);
        }
    };
}

function showAskUserDialog(question) {
    removeTyping();
    setAgentState('WAITING', '等待用户回复...');

    const overlay = document.createElement('div');
    overlay.id = 'ask-user-overlay';
    overlay.style.cssText = `
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0,0,0,0.7); z-index: 2000;
        display: flex; align-items: center; justify-content: center;
    `;

    const box = document.createElement('div');
    box.style.cssText = `
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 12px; padding: 24px; width: 90%; max-width: 480px;
        box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    `;

    box.innerHTML = `
        <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px; color: var(--accent)">XMclaw is asking...</div>
        <div style="font-size: 13px; margin-bottom: 16px; line-height: 1.5">${escapeHtml(question)}</div>
        <textarea id="ask-user-input" rows="3" style="width: 100%; resize: vertical; margin-bottom: 12px; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 13px"></textarea>
        <div style="display: flex; gap: 10px; justify-content: flex-end">
            <button id="ask-user-cancel" style="padding: 8px 16px; border-radius: 6px; border: 1px solid var(--border); background: transparent; color: var(--text); font-size: 12px; cursor: pointer">Cancel</button>
            <button id="ask-user-submit" style="padding: 8px 16px; border-radius: 6px; border: none; background: var(--accent); color: #000; font-size: 12px; font-weight: 600; cursor: pointer">Reply</button>
        </div>
    `;

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const answerInput = document.getElementById('ask-user-input');
    answerInput.focus();

    document.getElementById('ask-user-cancel').addEventListener('click', () => {
        overlay.remove();
        setAgentState('IDLE', '等待输入...');
    });

    document.getElementById('ask-user-submit').addEventListener('click', () => {
        const answer = answerInput.value.trim();
        if (!answer) return;
        overlay.remove();
        addMessage(answer, 'user');
        showTyping();
        setAgentState('THINKING', '继续处理中...');
        ws.send(JSON.stringify({ role: 'user', content: `[RESUME] ${answer}` }));
    });

    answerInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('ask-user-submit').click();
        }
    });
}

function appendChunk(el, text) {
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

function addReflectionMessage(data, improvement) {
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

    if (improvement && improvement.status !== 'nothing_to_improve') {
        const actions = improvement.actions || [];
        for (const action of actions) {
            if (action.type === 'gene' && action.status === 'created') {
                body += `<div style="color:#a78bfa;font-size:11px;margin-top:6px">✨ 已自动生成 Gene: <strong>${escapeHtml(action.name)}</strong> <button class="viewer-btn" onclick="loadEntity('gene','${action.gene_id}')">查看</button></div>`;
            } else if (action.type === 'skill' && action.status === 'created') {
                body += `<div style="color:#a78bfa;font-size:11px;margin-top:6px">✨ 已自动生成 Skill: <strong>${escapeHtml(action.name)}</strong> <button class="viewer-btn" onclick="loadEntity('skill','${action.skill_id}')">查看</button></div>`;
            } else if (action.type === 'patch' && action.status === 'proposed') {
                body += `<div style="color:#f59e0b;font-size:11px;margin-top:6px">📋 核心代码修改提案: <strong>${escapeHtml(action.proposal_id)}</strong> <button class="viewer-btn" onclick="showToast('请在 shared/proposals 目录中审核补丁')">查看路径</button></div>`;
            } else if (action.type === 'gene' || action.type === 'skill') {
                body += `<div style="color:#9ca3af;font-size:11px;margin-top:6px">⚠️ ${escapeHtml(action.type)} 生成失败: ${escapeHtml(action.status)}</div>`;
            }
        }
    }

    el.innerHTML = body;

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

    let finalText = text;
    if (planMode) {
        finalText = `[PLAN MODE] ${text}`;
    }

    addMessage(text, 'user');
    input.value = '';
    input.style.height = 'auto';
    showTyping();
    currentMessageEl = null;
    setAgentState('THINKING', '分析请求中...');

    const settings = localStorage.getItem('xmclaw_settings');
    const payload = { role: 'user', content: finalText };
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
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

document.getElementById('workspace-refresh')?.addEventListener('click', loadWorkspaceFiles);

loadSettings();
connect();

// Global error handler
window.addEventListener('error', (e) => {
    console.error('[ERROR]', e.message, 'at', e.filename, ':', e.lineno);
});

// Memory search
async function loadMemorySearch() {
    const q = document.getElementById('memory-query')?.value?.trim();
    const resultsEl = document.getElementById('memory-results');
    if (!resultsEl) return;
    if (!q) {
        resultsEl.innerHTML = '<div class="empty-state">输入关键词搜索记忆</div>';
        return;
    }
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/memory/search?q=` + encodeURIComponent(q));
        const data = await res.json();
        let html = '';
        if (data.vector_results && data.vector_results.length) {
            html += '<h4>向量记忆</h4>';
            for (const r of data.vector_results) {
                html += `<div class="memory-item"><div class="memory-source">${escapeHtml(r.source)}</div><div class="memory-content">${escapeHtml(r.content)}</div></div>`;
            }
        }
        if (data.file_results && data.file_results.length) {
            html += '<h4>文件匹配</h4>';
            for (const r of data.file_results) {
                html += `<div class="memory-item"><div class="memory-source">${escapeHtml(r.file)}</div><pre>${escapeHtml(r.snippet)}</pre></div>`;
            }
        }
        if (!html) html = '<div class="empty-state">未找到匹配结果</div>';
        resultsEl.innerHTML = html;
    } catch (e) {
        resultsEl.innerHTML = '<div class="empty-state">搜索失败</div>';
    }
}

// Tools logs
async function loadToolsLogs() {
    const el = document.getElementById('tool-log');
    if (!el) return;
    try {
        const res = await fetch('/api/tools/logs');
        const data = await res.json();
        if (!data.logs || !data.logs.length) {
            el.innerHTML = '<div class="empty-state">暂无工具日志</div>';
            return;
        }
        let html = '';
        for (const log of data.logs) {
            html += `<div class="tool-log-item"><strong>${escapeHtml(log.name)}</strong><pre>${escapeHtml(log.content)}</pre></div>`;
        }
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<div class="empty-state">加载失败</div>';
    }
}


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

// ===== MULTI-AGENT =====
async function loadAgentsView() {
    try {
        const [agentsRes, teamsRes] = await Promise.all([
            fetch('/api/agents'),
            fetch('/api/teams')
        ]);
        const agentsData = await agentsRes.json();
        const teamsData = await teamsRes.json();

        const agentsList = document.getElementById('agents-list');
        const teamsList = document.getElementById('teams-list');

        // Render agents
        const agents = agentsData.agents || [];
        if (agents.length === 0) {
            agentsList.innerHTML = '<div class="empty">暂无代理</div>';
        } else {
            agentsList.innerHTML = agents.map(a => `
                <div class="agent-card">
                    <div class="agent-header">
                        <span class="agent-name">${escapeHtml(a.agent_id)}</span>
                        <span class="agent-status ${a.status}">${a.status === 'idle' ? '空闲' : a.status === 'busy' ? '忙碌' : '离线'}</span>
                    </div>
                    <div style="font-size:11px;color:var(--text-dim)">计划模式: ${a.plan_mode ? '开启' : '关闭'} | 最大轮数: ${a.max_turns}</div>
                    <div class="agent-actions">
                        <button onclick="delegateToAgent('${a.agent_id}')">委派任务</button>
                    </div>
                </div>
            `).join('');
        }

        // Render teams
        const teams = teamsData.teams || {};
        const teamNames = Object.keys(teams);
        if (teamNames.length === 0) {
            teamsList.innerHTML = '<div class="empty">暂无团队</div>';
        } else {
            teamsList.innerHTML = teamNames.map(name => {
                const members = teams[name] || [];
                return `
                <div class="team-card">
                    <div class="team-header">
                        <span class="team-name">${escapeHtml(name)}</span>
                        <button onclick="deleteTeam('${name}')" style="background:transparent;border:none;color:#ff6b6b;cursor:pointer;font-size:11px">删除</button>
                    </div>
                    <div class="team-agents">成员: ${members.length ? members.map(m => escapeHtml(m)).join(', ') : '无'}</div>
                    <div class="team-actions">
                        <button onclick="addAgentToTeam('${name}')">+ 添加代理</button>
                        <button onclick="removeAgentFromTeam('${name}')">- 移除代理</button>
                        <button onclick="delegateToTeam('${name}')">委派任务</button>
                    </div>
                </div>`;
            }).join('');
        }
    } catch (e) {
        console.error('loadAgentsView error', e);
    }
}

document.getElementById('create-team-btn')?.addEventListener('click', async () => {
    const name = prompt('请输入团队名称:');
    if (!name) return;
    try {
        const res = await fetch('/api/teams', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        if (res.ok) {
            showToast('团队创建成功');
            loadAgentsView();
        } else {
            const data = await res.json();
            showToast('创建失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('创建失败');
    }
});

async function deleteTeam(name) {
    if (!confirm(`确定删除团队 "${name}" 吗?`)) return;
    try {
        const res = await fetch(`/api/teams/${name}`, { method: 'DELETE' });
        if (res.ok) {
            showToast('团队已删除');
            loadAgentsView();
        } else {
            const data = await res.json();
            showToast('删除失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('删除失败');
    }
}

async function addAgentToTeam(teamName) {
    const agentId = prompt('请输入要添加的代理 ID:');
    if (!agentId) return;
    try {
        const res = await fetch(`/api/teams/${teamName}/agents/${agentId}`, { method: 'POST' });
        if (res.ok) {
            showToast('代理已添加');
            loadAgentsView();
        } else {
            const data = await res.json();
            showToast('添加失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('添加失败');
    }
}

async function removeAgentFromTeam(teamName) {
    const agentId = prompt('请输入要移除的代理 ID:');
    if (!agentId) return;
    try {
        const res = await fetch(`/api/teams/${teamName}/agents/${agentId}`, { method: 'DELETE' });
        if (res.ok) {
            showToast('代理已移除');
            loadAgentsView();
        } else {
            const data = await res.json();
            showToast('移除失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('移除失败');
    }
}

async function delegateToAgent(agentId) {
    const task = prompt(`请输入要委派给 ${agentId} 的任务:`);
    if (!task) return;
    try {
        const res = await fetch(`/api/agents/${agentId}/delegate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`任务已委派给 ${agentId}`);
            alert('结果:\n' + JSON.stringify(data.result, null, 2));
        } else {
            showToast('委派失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('委派失败');
    }
}

async function delegateToTeam(teamName) {
    const task = prompt(`请输入要委派给团队 ${teamName} 的任务:`);
    if (!task) return;
    try {
        const res = await fetch(`/api/teams/${teamName}/delegate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`任务已委派给团队 ${teamName}`);
            alert('结果:\n' + JSON.stringify(data.results, null, 2));
        } else {
            showToast('委派失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        showToast('委派失败');
    }
}

// ===== INIT =====
newSession();
