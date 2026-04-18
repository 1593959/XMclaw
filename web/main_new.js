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
let currentMessageEl = null;
let currentView = 'dashboard';
let _rawTextMap = new Map();

// Tool card tracking: call_id -> { el, tool, startTime }
const _toolCards = new Map();

let totalCost = 0;
let totalTokens = 0;
let geneCount = 0;
let skillCount = 0;
let toolHistory = [];
let selfModHistory = [];
let todos = [];
let planMode = false;
let isStreaming = false;

// 控制台日志
let consoleLogs = [];
const MAX_LOGS = 100;

function logConsole(type, msg, data) {
    const time = new Date().toLocaleTimeString();
    const entry = { time, type, msg, data };
    consoleLogs.unshift(entry);
    if (consoleLogs.length > MAX_LOGS) consoleLogs.pop();
    updateConsolePanel();
}

function updateConsolePanel() {
    const panel = document.getElementById('console-logs');
    if (!panel) return;
    panel.innerHTML = consoleLogs.slice(0, 50).map(log => {
        const color = log.type === 'error' ? '#ff6b6b' : 
                      log.type === 'tool' ? '#ffc107' : 
                      log.type === 'state' ? '#4caf50' : '#9ca3af';
        return `<div style="font-size:11px;color:${color};padding:2px 0;border-bottom:1px solid #222">
            <span style="color:#666">${log.time}</span> 
            <strong>[${log.type}]</strong> ${escapeHtml(log.msg || '')}
            ${log.data ? `<span style="color:#666;margin-left:8px">${escapeHtml(JSON.stringify(log.data).substring(0,100))}</span>` : ''}
        </div>`;
    }).join('');
}

function toggleConsolePanel() {
    const panel = document.getElementById('console-panel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

// ── Tool Card Builder ─────────────────────────────────────────────────────────
// Creates a rich tool call card with status animation, args display, and
// execution-time tracking, all driven by backend events (tool_start / tool_result).

function _makeToolCard(toolName, callId, args) {
    const id = callId || `call_${toolName}_${Date.now()}`;
    const argsStr = args && Object.keys(args).length
        ? JSON.stringify(args, null, 2)
        : '';
    const toolIcon = _getToolIcon(toolName);

    const el = document.createElement('div');
    el.className = 'tool-call-card';
    el.id = `tcard-${id}`;
    el.innerHTML = `
        <div class="tool-call-header">
            <span class="tool-icon">${toolIcon}</span>
            <span class="tool-name">${escapeHtml(toolName)}</span>
            <span class="tool-status-dot" id="tdot-${id}"></span>
            <span class="tool-duration" id="tdur-${id}"></span>
            <button class="tool-expand-btn" onclick="_toggleToolCard('${id}')">
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                    <path d="M2 4L6 8L10 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
            </button>
        </div>
        <div class="tool-call-body" id="tbody-${id}" style="display:none">
            ${argsStr ? `<div class="tool-args-label">参数</div><pre class="tool-args">${escapeHtml(argsStr)}</pre>` : ''}
            <div class="tool-result-label" style="display:none" id="trl-${id}">结果</div>
            <div class="tool-result" id="tres-${id}"></div>
        </div>
    `;
    return { el, id };
}

function _getToolIcon(toolName) {
    const icons = {
        file_write: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4fc3f7" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
        file_read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#81c784" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
        file_edit: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff176" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
        bash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#b39ddb" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
        web_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ffb74d" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
        memory_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ce93d8" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
        ask_user: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ef9a9a" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
        default: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#90caf9" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6M9 13h6M9 17h4"/></svg>',
    };
    return icons[toolName] || icons['default'];
}

function _toggleToolCard(id) {
    const body = document.getElementById(`tbody-${id}`);
    if (!body) return;
    body.style.display = body.style.display === 'none' ? 'block' : 'none';
}

function _updateToolCard(id, eventType, data) {
    const dot = document.getElementById(`tdot-${id}`);
    const dur = document.getElementById(`tdur-${id}`);
    const resLabel = document.getElementById(`trl-${id}`);
    const res = document.getElementById(`tres-${id}`);
    const card = document.getElementById(`tcard-${id}`);
    if (!card) return;

    if (eventType === 'start') {
        if (dot) {
            dot.className = 'tool-status-dot running';
            dot.style.animation = 'pulse 1s infinite';
        }
    } else if (eventType === 'result') {
        if (dot) {
            dot.className = 'tool-status-dot done';
            dot.style.animation = '';
        }
        if (dur) {
            const d = data.duration;
            dur.textContent = d != null ? `${d}s` : '';
            dur.style.color = d != null && d > 5 ? '#ef9a9a' : '#4caf50';
        }
        if (res) {
            resLabel.style.display = 'block';
            const text = typeof data.result === 'string'
                ? data.result
                : JSON.stringify(data.result, null, 2);
            res.innerHTML = `<pre class="tool-result-text">${escapeHtml(text.substring(0, 2000))}</pre>`;
        }
        card.classList.add('tool-done');
    } else if (eventType === 'error') {
        if (dot) {
            dot.className = 'tool-status-dot error';
            dot.style.animation = '';
        }
        if (dur) dur.textContent = 'ERROR';
        if (res) {
            resLabel.style.display = 'block';
            res.innerHTML = `<pre class="tool-result-text" style="color:#ef9a9a">${escapeHtml(String(data))}</pre>`;
        }
        card.classList.add('tool-error');
    }
}

// 会话持久化到 localStorage
const SESSIONS_KEY = 'xmclaw_sessions';
const CURRENT_SESSION_KEY = 'xmclaw_current_session';
const STATE_KEY = 'xmclaw_state';

function loadSessions() {
    try {
        const data = localStorage.getItem(SESSIONS_KEY);
        if (data) {
            sessions = JSON.parse(data);
            currentSessionId = localStorage.getItem(CURRENT_SESSION_KEY) || sessions[0]?.id || null;
        }
    } catch (e) {
        console.error('loadSessions error', e);
    }
}

function persistSessions() {
    try {
        localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions));
        if (currentSessionId) {
            localStorage.setItem(CURRENT_SESSION_KEY, currentSessionId);
        }
    } catch (e) {
        console.error('persistSessions error', e);
    }
}

// ── State persistence ────────────────────────────────────────────────────────────
function loadState() {
    try {
        const raw = localStorage.getItem(STATE_KEY);
        if (!raw) return;
        const s = JSON.parse(raw);
        if (s.toolHistory)  toolHistory    = s.toolHistory;
        if (s.selfModHistory) selfModHistory = s.selfModHistory;
        if (s.geneCount)   geneCount     = s.geneCount;
        if (s.skillCount)  skillCount    = s.skillCount;
        if (s.totalTokens) totalTokens   = s.totalTokens;
        if (s.totalCost)  totalCost     = s.totalCost;
        if (s.todos)       todos         = s.todos;
        if (s.tasks)       tasks         = s.tasks;
        // Restore UI displays
        if (geneCount !== undefined || skillCount !== undefined) {
            evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        }
        tokenDisplay.textContent = `${totalTokens.toLocaleString()} tokens`;
        costDisplay.textContent  = `$${totalCost.toFixed(4)}`;
        renderRecentTools();
        renderSelfModLog();
        renderToolLog();
        renderTodos();
        renderTasks();
    } catch (e) {
        console.error('loadState error', e);
    }
}

function persistState() {
    try {
        localStorage.setItem(STATE_KEY, JSON.stringify({
            toolHistory, selfModHistory,
            geneCount, skillCount,
            totalTokens, totalCost,
            todos, tasks,
        }));
    } catch (e) {
        console.error('persistState error', e);
    }
}

// Rebuild _rawTextMap from DOM after restoring a session's HTML.
// This allows copy/re-edit to work after page refresh.
function _rebuildRawTextMap() {
    _rawTextMap.clear();
    document.querySelectorAll('.message-row.agent .message').forEach(el => {
        // Use the textContent as the best available raw representation
        _rawTextMap.set(el, el.textContent || '');
    });
}

let sessions = [];
let currentSessionId = null;

const AGENT_ID = 'default';

// ===== URL ROUTING (delegated to /src/modules/router.js) =====
// Navigation, view switching, and hash routing are handled by the router module.
// Nav item clicks are wired there; keep only non-routing init below.

// Settings
const settingProvider = document.getElementById('setting-provider');
const settingTemp = document.getElementById('setting-temp');
const tempValue = document.getElementById('temp-value');
const saveSettingsBtn = document.getElementById('save-settings');

// Per-provider input references
function getProviderEls(p) {
    return {
        apiKey:  document.getElementById(`${p}-apikey`),
        baseUrl: document.getElementById(`${p}-baseurl`),
        model:   document.getElementById(`${p}-model`),
        preset:  document.getElementById(`${p}-model-preset`),
    };
}

settingTemp?.addEventListener('input', () => {
    if (tempValue) tempValue.textContent = settingTemp.value;
});

// Apply preset → free-text input
window.applyModelPreset = function(provider) {
    const els = getProviderEls(provider);
    if (!els.preset || !els.model) return;
    const val = els.preset.value;
    if (val) {
        els.model.value = val;
        els.preset.value = ''; // reset to placeholder after copy
    }
};

async function loadDaemonConfig() {
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        const llm = cfg.llm || {};
        const provider = llm.default_provider || 'anthropic';
        if (settingProvider) settingProvider.value = provider;

        // Populate both provider cards
        for (const p of ['anthropic', 'openai']) {
            const src = llm[p] || {};
            const els = getProviderEls(p);
            if (els.apiKey)  els.apiKey.value  = src.api_key      || '';
            if (els.baseUrl) els.baseUrl.value = src.base_url     || '';
            if (els.model)   els.model.value   = src.default_model || '';
        }

        // Update model badge
        const activeModel = (llm[provider] || {}).default_model || '';
        modelBadge.textContent = activeModel || provider;

        // MCP config
        renderMCPList(cfg.mcp_servers || {});

        // Integration config
        populateIntegrationFields(cfg.integrations || {});

        // ── Evolution tab ────────────────────────────────────────────
        const evo = cfg.evolution || {};
        const evoEnabled = document.getElementById('evo-enabled');
        const evoInterval = document.getElementById('evo-interval');
        const evoDaily = document.getElementById('evo-daily');
        const evoVfm = document.getElementById('evo-vfm');
        if (evoEnabled)  evoEnabled.checked  = evo.enabled    ?? true;
        if (evoInterval) evoInterval.value    = evo.interval   ?? 30;
        if (evoDaily)   evoDaily.value      = evo.daily      ?? 22;
        if (evoVfm)     evoVfm.value        = evo.vfm_threshold ?? 5;

        // ── Memory tab ──────────────────────────────────────────────
        const mem = cfg.memory || {};
        const memVector = document.getElementById('mem-vector');
        const memRetention = document.getElementById('mem-retention');
        const memTokens = document.getElementById('mem-tokens');
        if (memVector)   memVector.value   = mem.vector_path    || '';
        if (memRetention) memRetention.value = mem.retention_days ?? 7;
        if (memTokens)   memTokens.value     = mem.max_tokens     ?? 120000;

        // ── Tools tab ────────────────────────────────────────────────
        const tools = cfg.tools || {};
        const toolBash = document.getElementById('tool-bash');
        const toolSandbox = document.getElementById('tool-sandbox');
        const toolHeadless = document.getElementById('tool-headless');
        if (toolBash)    toolBash.value    = tools.bash_timeout   ?? 300;
        if (toolSandbox) toolSandbox.value = tools.sandbox_timeout ?? 30;
        if (toolHeadless) toolHeadless.checked = tools.headless ?? false;

        // ── Gateway tab ─────────────────────────────────────────────
        const gw = cfg.gateway || {};
        const gwWs = document.getElementById('gw-ws');
        const gwHttp = document.getElementById('gw-http');
        if (gwWs)  gwWs.value  = gw.port ?? 8765;
        if (gwHttp) gwHttp.value = gw.http_port ?? 8080;

        // Persist trimmed snapshot (no secrets) for WS payload
        _syncSettingsCache(provider, llm);
    } catch {}
}

function _syncSettingsCache(provider, llm) {
    const activeModel = (llm[provider] || {}).default_model || '';
    localStorage.setItem('xmclaw_settings', JSON.stringify({
        provider,
        model: activeModel,
        // Do NOT store API keys in localStorage
    }));
}

saveSettingsBtn?.addEventListener('click', async () => {
    const provider = settingProvider?.value || 'anthropic';
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        cfg.llm = cfg.llm || {};
        cfg.llm.default_provider = provider;

        for (const p of ['anthropic', 'openai']) {
            const els = getProviderEls(p);
            cfg.llm[p] = cfg.llm[p] || {};
            if (els.apiKey)  cfg.llm[p].api_key       = els.apiKey.value.trim();
            if (els.baseUrl) cfg.llm[p].base_url       = els.baseUrl.value.trim() || (p === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.openai.com/v1');
            if (els.model)   cfg.llm[p].default_model  = els.model.value.trim();
        }

        cfg.mcp_servers = collectMCPConfig();

        // Collect integration settings
        cfg.integrations = cfg.integrations || {};
        for (const [name, fields] of Object.entries(INTEG_FIELDS)) {
            const obj = {};
            for (const [elId, key, type] of fields) {
                const el = document.getElementById(elId);
                if (!el) continue;
                if (type === 'bool') obj[key] = el.checked;
                else if (type === 'int') obj[key] = parseInt(el.value) || 0;
                else obj[key] = el.value.trim();
            }
            cfg.integrations[name] = obj;
        }

        // ── Evolution tab ─────────────────────────────────────────
        cfg.evolution = cfg.evolution || {};
        cfg.evolution.enabled         = document.getElementById('evo-enabled')?.checked    ?? true;
        cfg.evolution.interval        = parseInt(document.getElementById('evo-interval')?.value) || 30;
        cfg.evolution.daily           = parseInt(document.getElementById('evo-daily')?.value)   || 22;
        cfg.evolution.vfm_threshold  = parseFloat(document.getElementById('evo-vfm')?.value)   || 5;

        // ── Memory tab ────────────────────────────────────────────
        cfg.memory = cfg.memory || {};
        cfg.memory.vector_path    = document.getElementById('mem-vector')?.value   || '';
        cfg.memory.retention_days = parseInt(document.getElementById('mem-retention')?.value) || 7;
        cfg.memory.max_tokens    = parseInt(document.getElementById('mem-tokens')?.value)   || 120000;

        // ── Tools tab ───────────────────────────────────────────
        cfg.tools = cfg.tools || {};
        cfg.tools.bash_timeout     = parseInt(document.getElementById('tool-bash')?.value)    || 300;
        cfg.tools.sandbox_timeout  = parseInt(document.getElementById('tool-sandbox')?.value) || 30;
        cfg.tools.headless         = document.getElementById('tool-headless')?.checked        ?? false;

        // ── Gateway tab ─────────────────────────────────────────
        cfg.gateway = cfg.gateway || {};
        cfg.gateway.port     = parseInt(document.getElementById('gw-ws')?.value)   || 8765;
        cfg.gateway.http_port = parseInt(document.getElementById('gw-http')?.value) || 8080;

        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg)
        });

        const activeModel = cfg.llm[provider]?.default_model || '';
        modelBadge.textContent = activeModel || provider;
        _syncSettingsCache(provider, cfg.llm);
        showToast('设置已保存');
    } catch (e) {
        showToast('保存失败: ' + e.message);
    }
});

function _switchSettingsTab(stab, opts = {}) {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
    const tabEl = document.querySelector(`.settings-tab[data-stab="${stab}"]`);
    const panelEl = document.getElementById('stab-' + stab);
    if (tabEl) tabEl.classList.add('active');
    if (panelEl) panelEl.classList.add('active');
    if (stab === 'integrations') loadIntegrationStatus();
    // Sync URL hash
    if (!opts.fromRouter) {
        history.replaceState(null, '', `#/settings/${stab}`);
    }
}

function loadSettings() {
    loadDaemonConfig();

    // Bind tab clicks
    document.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            _switchSettingsTab(tab.dataset.stab);
        });
    });

    // Expose for external use
    window._switchSettingsTab = _switchSettingsTab;

    // memory search listeners are set up globally
    initMCPUI();
}

// ===== INTEGRATIONS =====

// Map: integrationName → [ [fieldId, configKey], ... ]
const INTEG_FIELDS = {
    slack:    [['integ-slack-enabled','enabled','bool'],['integ-slack-bot-token','bot_token'],['integ-slack-app-token','app_token'],['integ-slack-channel','channel']],
    discord:  [['integ-discord-enabled','enabled','bool'],['integ-discord-bot-token','bot_token'],['integ-discord-channel-id','channel_id']],
    telegram: [['integ-telegram-enabled','enabled','bool'],['integ-telegram-bot-token','bot_token'],['integ-telegram-chat-id','chat_id']],
    github:   [['integ-github-enabled','enabled','bool'],['integ-github-token','token'],['integ-github-repo','repo'],['integ-github-poll','poll_interval','int']],
    notion:   [['integ-notion-enabled','enabled','bool'],['integ-notion-api-key','api_key'],['integ-notion-database-id','database_id']],
};

async function loadIntegrationStatus() {
    try {
        const res = await fetch('/api/integrations');
        const data = await res.json();
        renderIntegrationStatusBar(data.integrations || {});
    } catch {}
}

function renderIntegrationStatusBar(statuses) {
    const bar = document.getElementById('integration-status-bar');
    if (!bar) return;
    const icons = { slack: '💬', discord: '🎮', telegram: '✈️', github: '🐙', notion: '📝' };
    bar.innerHTML = Object.entries(statuses).map(([name, s]) => {
        const dot = s.running ? 'integ-dot-on' : s.enabled ? 'integ-dot-warn' : 'integ-dot-off';
        const label = s.running ? '运行中' : s.enabled ? '未连接' : '未启用';
        return `<div class="integ-status-chip">
            <span>${icons[name] || '🔌'}</span>
            <span>${name}</span>
            <span class="integ-dot ${dot}" title="${label}"></span>
        </div>`;
    }).join('');
}

async function saveIntegrations() {
    try {
        const cfgRes = await fetch('/api/config');
        const cfg = await cfgRes.json();
        cfg.integrations = cfg.integrations || {};
        for (const [name, fields] of Object.entries(INTEG_FIELDS)) {
            const obj = {};
            for (const [elId, key, type] of fields) {
                const el = document.getElementById(elId);
                if (!el) continue;
                if (type === 'bool') obj[key] = el.checked;
                else if (type === 'int') obj[key] = parseInt(el.value) || 0;
                else obj[key] = el.value.trim();
            }
            cfg.integrations[name] = obj;
        }
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
    } catch (e) {
        showToast('集成配置保存失败: ' + e.message);
    }
}

function populateIntegrationFields(integrations) {
    for (const [name, fields] of Object.entries(INTEG_FIELDS)) {
        const cfg = integrations[name] || {};
        for (const [elId, key, type] of fields) {
            const el = document.getElementById(elId);
            if (!el) continue;
            if (type === 'bool') el.checked = !!cfg[key];
            else el.value = cfg[key] ?? '';
        }
    }
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
    el.className = 'toast';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2200);
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
    const labels = { IDLE: '空闲', THINKING: '思考中', PLANNING: '规划中', TOOL_CALL: '工具调用', SELF_MOD: '自修改', WAITING: '等待中' };
    stateBadge.textContent = labels[state] || state;
    if (thought) currentThought.textContent = thought;
    if (state === 'THINKING' || state === 'PLANNING') {
        stateBadge.style.background = 'var(--blue-bg)';
        stateBadge.style.color = 'var(--blue)';
    } else if (state === 'TOOL_CALL') {
        stateBadge.style.background = 'var(--yellow-bg)';
        stateBadge.style.color = 'var(--yellow)';
    } else if (state === 'SELF_MOD') {
        stateBadge.style.background = 'var(--purple-bg)';
        stateBadge.style.color = 'var(--purple)';
    } else if (state === 'WAITING') {
        stateBadge.style.background = 'var(--red-bg)';
        stateBadge.style.color = 'var(--red)';
    } else {
        stateBadge.style.background = 'var(--accent-subtle)';
        stateBadge.style.color = 'var(--accent)';
    }
}

function addToolCall(tool, args, result) {
    const entry = { tool, args, result, time: new Date().toLocaleTimeString() };
    toolHistory.unshift(entry);
    if (toolHistory.length > 20) toolHistory.pop();
    renderRecentTools();
    renderToolLog();
    persistState();
}

function addSelfMod(file, action) {
    const entry = { file, action, time: new Date().toLocaleTimeString() };
    selfModHistory.unshift(entry);
    if (selfModHistory.length > 20) selfModHistory.pop();
    renderSelfModLog();
    addTimelineEvent('self_mod', `Self-modification: ${action}`, file);
    persistState();
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
    persistState();
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
    persistState();
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
        const data = await res.json();
        tasks = Array.isArray(data) ? data : [];
        renderTasks();
    } catch {
        tasks = [];
        renderTasks();
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
    persistState();
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
    persistState();
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

        // Build enhanced HTML with vector + file sections
        let html = '';

        // Vector results (with relevance scores)
        const vectorResults = data.vector_results || data.results || [];
        if (vectorResults.length) {
            html += '<div class="memory-section-label">向量记忆</div>';
            for (const r of vectorResults) {
                const score = r.score != null ? (r.score * 100).toFixed(0) + '%' : '';
                html += `<div class="memory-card">
                    <div class="memory-card-header">
                        <span class="memory-card-type">${escapeHtml(r.source || r.file || 'memory')}</span>
                        ${score ? `<span class="memory-score">相关度 ${score}</span>` : ''}
                    </div>
                    <div class="memory-card-body"><pre style="background:#0a0a0a;padding:10px;border-radius:6px;font-size:12px">${escapeHtml(r.content || r.snippet || '')}</pre></div>
                </div>`;
            }
        }

        // File results
        const fileResults = data.file_results || [];
        if (fileResults.length) {
            html += '<div class="memory-section-label" style="margin-top:16px">文件匹配</div>';
            for (const r of fileResults) {
                html += `<div class="memory-card">
                    <div class="memory-card-header">
                        <span class="memory-card-type">${escapeHtml(r.file || '')}</span>
                    </div>
                    <div class="memory-card-body"><pre style="background:#0a0a0a;padding:10px;border-radius:6px;font-size:12px">${escapeHtml(r.snippet || '')}</pre></div>
                </div>`;
            }
        }

        if (!html) html = '<div class="empty-state">未找到匹配结果</div>';
        memoryResults.innerHTML = html;
    } catch {
        memoryResults.innerHTML = '<div class="empty-state">搜索失败</div>';
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

        // Update dedicated stat elements on the Evolution page
        const evoGenesEl = document.getElementById('evo-genes');
        const evoSkillsEl = document.getElementById('evo-skills');
        const evoCyclesEl = document.getElementById('evo-cycles');
        if (evoGenesEl) evoGenesEl.textContent = geneCount;
        if (evoSkillsEl) evoSkillsEl.textContent = skillCount;
        if (evoCyclesEl) evoCyclesEl.textContent = data.cycle_count || data.cycles || 0;

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


function handleBusEvent(evt) {
    if (!evt) return;
    const etype = evt.event_type || '';
    const payload = evt.payload || {};
    
    if (etype === 'gene:activated') {
        geneCount++;
        evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        addTimelineEvent('gene', `Gene 生成: ${payload.name || payload.gene_id || ''}`, `Score: ${payload.score || '?'}`);
        if (window._devPanel) window._devPanel.addThought(`Gene 生成: ${payload.name || payload.gene_id}`);
    } else if (etype === 'skill:executed') {
        skillCount++;
        evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        addTimelineEvent('skill', `Skill 生成: ${payload.name || payload.skill_id || ''}`, `Score: ${payload.score || '?'}`);
        if (window._devPanel) window._devPanel.addThought(`Skill 生成: ${payload.name || payload.skill_id}`);
    } else if (etype === 'plan:step') {
        // Plan step event
        if (window._devPanel) {
            window._devPanel.addThought(`步骤 ${payload.step || '?'}: ${payload.action || ''}`);
        }
    } else if (etype === 'file:modified') {
        // File modification event
        if (window._devPanel) {
            window._devPanel.addFileChange(payload.file || '', payload.action || 'modify');
            if (payload.diff) {
                window._devPanel.addDiff(payload.file || '', payload.diff);
            }
        }
    } else if (etype === 'thought') {
        // Thinking event
        if (window._devPanel) {
            window._devPanel.addThought(payload.text || '');
        }
    }
}

function flushChunk(el) {
    if (typeof hljs !== 'undefined') {
        el.querySelectorAll('pre code:not([data-highlighted])').forEach(block => {
            try { hljs.highlightElement(block); } catch {}
        });
    }
    // Add copy buttons to code blocks
    el.querySelectorAll('pre:not(.has-copy-btn)').forEach(pre => {
        pre.classList.add('has-copy-btn');
        const btn = document.createElement('button');
        btn.className = 'code-copy-btn';
        btn.textContent = '复制';
        btn.onclick = () => {
            const code = pre.querySelector('code')?.textContent || pre.textContent;
            navigator.clipboard.writeText(code).then(() => {
                btn.textContent = '已复制';
                setTimeout(() => btn.textContent = '复制', 1200);
            });
        };
        pre.style.position = 'relative';
        pre.appendChild(btn);
    });
}

function showWelcome() {
    const w = document.getElementById('welcome');
    if (w) w.style.display = '';
}

function hideWelcome() {
    const w = document.getElementById('welcome');
    if (w) w.style.display = 'none';
}

function appendChunk(el, text) {
    const prev = _rawTextMap.get(el) || '';
    const updated = prev + text;
    _rawTextMap.set(el, updated);
    el.innerHTML = formatMessage(updated);
    // Re-highlight new code blocks
    if (typeof hljs !== 'undefined') {
        el.querySelectorAll('pre code:not([data-highlighted])').forEach(block => {
            try { hljs.highlightElement(block); } catch {}
        });
    }
}

function formatMessage(text) {
    if (typeof marked !== 'undefined') {
        try { return marked.parse(text); } catch {}
    }
    // Fallback
    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    return html;
}

function addMessage(text, role) {
    // Hide welcome screen on first message
    const welcome = document.getElementById('welcome');
    if (welcome) welcome.style.display = 'none';

    const row = document.createElement('div');
    row.className = `message-row ${role}`;

    // Left side: Visual status indicator with mini dev panel trigger
    const statusIndicator = document.createElement('div');
    statusIndicator.className = 'message-status-indicator';
    
    // Role-based icon
    if (role === 'agent') {
        statusIndicator.innerHTML = `
            <div class="status-icon agent-icon" title="AI 助手 - 点击查看详情" onclick="_devPanel && _devPanel.open()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2a10 10 0 1 0 10 10H12V2z"/>
                    <circle cx="12" cy="10" r="3"/>
                </svg>
            </div>
            <div class="tool-progress-line"></div>
        `;
        row.classList.add('has-dev-info');
    } else if (role === 'user') {
        statusIndicator.innerHTML = `
            <div class="status-icon user-icon" title="用户">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                </svg>
            </div>
        `;
    } else if (role === 'tool') {
        statusIndicator.innerHTML = `
            <div class="status-icon tool-icon" title="工具调用">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
                </svg>
            </div>
            <div class="tool-progress-line"></div>
        `;
        row.classList.add('has-dev-info');
    }
    
    // Mini dev panel indicator for agent messages
    if (role === 'agent') {
        const miniIndicator = document.createElement('div');
        miniIndicator.className = 'dev-mini-panel';
        miniIndicator.title = '查看开发详情';
        miniIndicator.onclick = () => { if (window._devPanel) window._devPanel.open(); };
        row.appendChild(miniIndicator);
    }
    
    row.appendChild(statusIndicator);

    const wrapper = document.createElement('div');
    wrapper.className = 'message-wrapper';

    // Action buttons (copy for agent, edit for user)
    const actions = document.createElement('div');
    actions.className = 'message-actions';
    if (role === 'agent') {
        actions.innerHTML = `
            <button class="msg-action-btn" title="复制回复 (Ctrl+Shift+C)" onclick="_copyMessage(this)">📋</button>
        `;
    } else if (role === 'user') {
        actions.innerHTML = `
            <button class="msg-action-btn" title="编辑 (Ctrl+E)" onclick="_editMessage(this)">✏️</button>
            <button class="msg-action-btn" title="复制 (Ctrl+Shift+C)" onclick="_copyMessage(this)">📋</button>
        `;
    }

    const el = document.createElement('div');
    el.className = `message ${role}`;
    if (role !== 'user') {
        _rawTextMap.set(el, text);
        el.innerHTML = formatMessage(text);
        if (typeof hljs !== 'undefined') {
            el.querySelectorAll('pre code').forEach(block => {
                try { hljs.highlightElement(block); } catch {}
            });
        }
    } else {
        el.textContent = text;
        el.dataset.original = text;
    }

    // Message timestamp
    const ts = document.createElement('div');
    ts.className = 'message-time';
    ts.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

    wrapper.appendChild(actions);
    wrapper.appendChild(el);
    wrapper.appendChild(ts);
    row.appendChild(wrapper);
    chat.appendChild(row);
    scrollToBottom();
    return el;
}

// Copy message content
window._copyMessage = function(btn) {
    const row = btn.closest('.message-row');
    const msgEl = row.querySelector('.message');
    const text = _rawTextMap.get(msgEl) || msgEl.textContent;
    navigator.clipboard.writeText(text).then(() => {
        btn.textContent = '✅';
        setTimeout(() => btn.textContent = '📋', 1500);
    }).catch(() => showToast('复制失败'));
};

// _editMessage is redefined in the Shortcuts Bar section below with inline overlay UI

function addToolResultMessage(tool, result) {
    const row = document.createElement('div');
    row.className = 'message-row tool-result';

    const el = document.createElement('div');
    el.className = 'message tool-result';

    const resultStr = String(result);

    if (typeof result === 'string' && result.startsWith('data:image/')) {
        el.innerHTML = `
            <div class="tool-result-header">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                <span class="tool-name">${escapeHtml(tool)}</span>
            </div>
            <img src="${result}" style="max-width:100%;border-radius:8px;margin-top:6px;border:1px solid var(--border)" alt="screenshot">
        `;
    } else {
        const isLong = resultStr.length > 400;
        const preview = escapeHtml(resultStr.slice(0, 400));
        const full = escapeHtml(resultStr);
        const uid = 'tr_' + Date.now();
        el.innerHTML = `
            <div class="tool-result-header">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                <span class="tool-name">${escapeHtml(tool)}</span>
                ${isLong ? `<button class="tool-expand-btn" onclick="toggleToolResult('${uid}')">展开</button>` : ''}
            </div>
            <pre class="tool-result-pre" id="${uid}">${isLong ? preview + '...' : full}</pre>
        `;
        if (isLong) el._fullResult = full;
    }

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
}

window.toggleToolResult = function(uid) {
    const pre = document.getElementById(uid);
    if (!pre) return;
    const el = pre.closest('.message.tool-result');
    const btn = el?.querySelector('.tool-expand-btn');
    if (pre.dataset.expanded === 'true') {
        pre.textContent = el._fullResult?.slice(0, 400) + '...';
        pre.dataset.expanded = 'false';
        if (btn) btn.textContent = '展开';
    } else {
        pre.textContent = el._fullResult || pre.textContent;
        pre.dataset.expanded = 'true';
        if (btn) btn.textContent = '收起';
    }
};

function addReflectionMessage(data, improvement) {
    const summary = data.summary || 'Reflection';
    const problems = data.problems || [];
    const lessons = data.lessons || [];
    const improvements = data.improvements || [];

    const row = document.createElement('div');
    row.className = 'message-row reflection';

    const el = document.createElement('div');
    el.className = 'message reflection';

    let body = `<div class="reflection-summary">🧠 ${escapeHtml(summary)}</div>`;

    if (problems.length) {
        body += `<div class="reflection-section reflection-problems">
            <div class="reflection-section-title">❌ 问题</div>
            ${problems.map(p => `<div class="reflection-item">${escapeHtml(p)}</div>`).join('')}
        </div>`;
    }
    if (lessons.length) {
        body += `<div class="reflection-section reflection-lessons">
            <div class="reflection-section-title">💡 教训</div>
            ${lessons.map(l => `<div class="reflection-item">${escapeHtml(l)}</div>`).join('')}
        </div>`;
    }
    if (improvements.length) {
        body += `<div class="reflection-section reflection-improvements">
            <div class="reflection-section-title">✅ 改进</div>
            ${improvements.map(i => `<div class="reflection-item">${escapeHtml(i)}</div>`).join('')}
        </div>`;
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
    const btn = document.getElementById('scroll-bottom-btn');
    if (btn) {
        const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
        btn.classList.toggle('visible', !atBottom && chat.scrollHeight > chat.clientHeight + 100);
    }
}

window.setInput = function(text) {
    hideWelcome();
    input.value = text;
    input.focus();
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
};

function sendMessage() {
    const text = input.value.trim();
    if (!text) return;

    let finalText = text;
    if (planMode) {
        finalText = `[PLAN MODE] ${text}`;
    }

    // ── Fallback send (used only when websocket.js fails to load) ─────────────
    function _sendSafe(payload) {
        // Open a fresh WebSocket and send; used as last resort
        try {
            const s = new WebSocket('ws://127.0.0.1:8765/agent/default');
            s.onopen = () => {
                s.send(JSON.stringify(payload));
                s.close();
            };
            return true;
        } catch (_) {
            return false;
        }
    }

    // Save to message history for Up/Down navigation
    _userHistory.push(text);
    if (_userHistory.length > 100) _userHistory = _userHistory.slice(-100);
    localStorage.setItem('xmclaw_history', JSON.stringify(_userHistory));
    _historyIndex = -1;

    // Clear draft
    localStorage.removeItem('xmclaw_draft');

    hideWelcome();
    addMessage(text, 'user');
    input.value = '';
    input.style.height = 'auto';
    showTyping();
    currentMessageEl = null;
    isStreaming = false;
    setAgentState('THINKING', '分析请求中...');

    const settings = localStorage.getItem('xmclaw_settings');
    const payload = { role: 'user', content: finalText };
    if (settings) {
        try {
            payload.settings = JSON.parse(settings);
        } catch {}
    }

    const sent = window.wsSend ? window.wsSend(payload) : _sendSafe(payload);
    if (!sent) {
        removeTyping();
        setAgentState('IDLE', '');
        showToast('⚠️ 已离线，消息已加入发送队列');
    }
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

// (init happens at end of file)

// Global error handler
window.addEventListener('error', (e) => {
    console.error('[ERROR]', e.message, 'at', e.filename, ':', e.lineno);
});

// Memory search (tab panel version with relevance scores)
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
            html += '<div class="memory-section-label">向量记忆</div>';
            for (const r of data.vector_results) {
                const score = r.score != null ? (r.score * 100).toFixed(0) + '%' : '';
                html += `<div class="memory-item">
                    <div class="memory-source">${escapeHtml(r.source || 'memory')}${score ? ` <span class="memory-score">相关度 ${score}</span>` : ''}</div>
                    <div class="memory-content">${escapeHtml(r.content || '')}</div>
                </div>`;
            }
        }
        if (data.file_results && data.file_results.length) {
            html += '<div class="memory-section-label" style="margin-top:14px">文件匹配</div>';
            for (const r of data.file_results) {
                html += `<div class="memory-item">
                    <div class="memory-source">${escapeHtml(r.file || '')}</div>
                    <pre style="font-size:12px;color:var(--text-dim);background:#0a0a0a;padding:6px;border-radius:4px;margin-top:4px">${escapeHtml(r.snippet || '')}</pre>
                </div>`;
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
    persistSessions();
    persistState();
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
    _rebuildRawTextMap();
    renderSessionList();
    persistSessions();
    if (chat.children.length === 0) showWelcome(); else hideWelcome();
}

function newSession() {
    saveCurrentSession();
    currentSessionId = generateSessionId();
    chat.innerHTML = '';
    showWelcome();
    renderSessionList();
    persistSessions();
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
// ── Global keyboard shortcuts ──────────────────────────────────────────────

// User message history for Up/Down navigation
let _userHistory = JSON.parse(localStorage.getItem('xmclaw_history') || '[]');
let _historyIndex = -1;

document.addEventListener('keydown', (e) => {
    // Ctrl+Enter: send
    if (e.ctrlKey && e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
    }
    // Ctrl+L: clear chat
    else if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
        e.preventDefault();
        clearChat();
    }
    // Ctrl+N: new session
    else if (e.ctrlKey && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault();
        newSession();
    }
    // Ctrl+Shift+C: copy last agent reply
    else if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.key === 'c')) {
        e.preventDefault();
        const agentMsgs = document.querySelectorAll('.message-row.agent .message');
        if (agentMsgs.length > 0) {
            const last = agentMsgs[agentMsgs.length - 1];
            const text = _rawTextMap.get(last) || last.textContent;
            navigator.clipboard.writeText(text).then(() => showToast('已复制到剪贴板')).catch(() => showToast('复制失败'));
        }
    }
    // Ctrl+E: edit last user message
    else if (e.ctrlKey && (e.key === 'e' || e.key === 'E')) {
        if (document.activeElement === input) return; // don't fire when typing
        const userMsgs = document.querySelectorAll('.message-row.user .message');
        if (userMsgs.length > 0) {
            const last = userMsgs[userMsgs.length - 1];
            last.closest('.message-row').querySelector('.msg-action-btn')?.click();
        }
    }
    // Ctrl+F: search in conversation
    else if (e.ctrlKey && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault();
        const term = prompt('搜索对话内容:');
        if (term) highlightSearch(term);
    }
    // Ctrl+S: save draft
    else if (e.ctrlKey && (e.key === 's' || e.key === 'S')) {
        if (document.activeElement === input) {
            e.preventDefault();
            saveDraft();
            showToast('草稿已保存');
        }
    }
    // Up/Down in input: navigate message history
    else if (e.key === 'ArrowUp' && document.activeElement === input && input.value === '') {
        e.preventDefault();
        if (_historyIndex < _userHistory.length - 1) {
            _historyIndex++;
            input.value = _userHistory[_userHistory.length - 1 - _historyIndex];
        }
    } else if (e.key === 'ArrowDown' && document.activeElement === input && input.value === '') {
        e.preventDefault();
        if (_historyIndex > 0) {
            _historyIndex--;
            input.value = _userHistory[_userHistory.length - 1 - _historyIndex];
        } else {
            _historyIndex = -1;
            input.value = '';
        }
    }
    // Escape: cancel current operation or close overlays
    else if (e.key === 'Escape') {
        const overlay = document.querySelector('.ask-user-overlay');
        if (overlay) overlay.remove();
    }
    // /: focus input
    else if (e.key === '/' && document.activeElement !== input &&
             !e.target.matches('input, textarea, [contenteditable]')) {
        e.preventDefault();
        input.focus();
    }
});

function saveDraft() {
    const text = input.value.trim();
    if (!text) return;
    localStorage.setItem('xmclaw_draft', text);
}

function loadDraft() {
    const draft = localStorage.getItem('xmclaw_draft');
    if (draft) {
        input.value = draft;
        input.dispatchEvent(new Event('input'));
    }
}

function highlightSearch(term) {
    // Remove previous highlights
    document.querySelectorAll('.search-highlight').forEach(el => {
        el.outerHTML = el.textContent;
    });
    if (!term) return;
    const chatEl = document.getElementById('chat');
    const walker = document.createTreeWalker(chatEl, NodeFilter.SHOW_TEXT, null, false);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
        if (!node.textContent.includes(term)) continue;
        const regex = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
        const parent = node.parentNode;
        if (!parent || parent.classList.contains('message-actions') || parent.classList.contains('msg-action-btn')) continue;
        const fragment = document.createDocumentFragment();
        let last = 0;
        for (const match of [...node.textContent.matchAll(regex)]) {
            if (match.index > last) fragment.appendChild(document.createTextNode(node.textContent.slice(last, match.index)));
            const span = document.createElement('mark');
            span.className = 'search-highlight';
            span.style.cssText = 'background:#fbbf24;color:#000;padding:0 2px;border-radius:2px;';
            span.textContent = match[0];
            fragment.appendChild(span);
            last = match.index + match[0].length;
        }
        if (last < node.textContent.length) fragment.appendChild(document.createTextNode(node.textContent.slice(last)));
        if (fragment.childNodes.length > 0) parent.replaceChild(fragment, node);
    }
    // Scroll first match into view
    const first = document.querySelector('.search-highlight');
    first?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

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

// (WebSocket delegation is handled at the bottom of the file — see _wsRenderer / window.wsSetGlobalHandler)

function addToolMessage(tool) {
    const row = document.createElement('div');
    row.className = 'message-row tool';

    const el = document.createElement('div');
    el.className = 'message tool';
    el.innerHTML = `
        <div class="tool-header">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
            <span class="tool-name">${escapeHtml(tool)}</span>
            <span class="tool-status-dot"></span>
            <span style="color:var(--text-faint);font-size:10px">执行中</span>
        </div>
    `;
    el._toolRow = row;

    row.appendChild(el);
    chat.appendChild(row);
    scrollToBottom();
    return el;
}

function showAskUserDialog(question) {
    removeTyping();
    setAgentState('WAITING', 'Waiting for user answer...');

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

    // Detect plan-mode confirmation vs. normal ask_user
    const isPlanQuestion = question.includes('计划已生成') || question.includes('是否执行');
    const title = isPlanQuestion ? '计划确认' : '需要您的输入';

    box.innerHTML = `
        <h3 style="margin:0 0 16px;font-size:16px;color:var(--text)">${title}</h3>
        <p style="margin:0 0 16px;font-size:14px;color:var(--text-dim)">${escapeHtml(question)}</p>
        <textarea id="ask-user-input" rows="3" style="
            width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);
            border-radius:8px;padding:10px;font-size:14px;resize:vertical;box-sizing:border-box;
        "></textarea>
        <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
            <button id="ask-user-cancel" style="
                padding:8px 16px;background:var(--bg);color:var(--text);
                border:1px solid var(--border);border-radius:6px;cursor:pointer;
            ">取消</button>
            <button id="ask-user-submit" style="
                padding:8px 16px;background:var(--accent);color:#000;
                border:none;border-radius:6px;cursor:pointer;font-weight:600;
            ">确认</button>
        </div>
    `;

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const input = document.getElementById('ask-user-input');
    input.focus();

    document.getElementById('ask-user-cancel').onclick = () => {
        overlay.remove();
        setAgentState('IDLE', 'Waiting for input...');
    };

    document.getElementById('ask-user-submit').onclick = () => {
        let answer = input.value;
        overlay.remove();
        // For plan-mode confirmation, inject [PLAN APPROVE] so the agent loop
        // skips the re-ask step and proceeds directly to tool execution.
        if (isPlanQuestion && answer.trim()) {
            answer = `[PLAN APPROVE] ${answer}`;
        }
        (window.wsSend || _sendSafe)({ type: 'ask_user_answer', answer });
    };

    input.onkeydown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('ask-user-submit').click();
        }
    };
}

// ===== FILE UPLOAD =====
const fileUpload = document.getElementById('file-upload');
if (fileUpload) {
    fileUpload.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files);
        if (files.length === 0) return;
        
        for (const file of files) {
            const reader = new FileReader();
            reader.onload = (evt) => {
                const dataUrl = evt.target.result;
                // 显示上传的文件消息
                const row = document.createElement('div');
                row.className = 'message-row user';
                row.innerHTML = `
                    <div class="message user">
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                            <span style="font-size:11px;color:var(--text-dim)">上传文件</span>
                        </div>
                        ${file.type.startsWith('image/') 
                            ? `<img src="${dataUrl}" style="max-width:200px;border-radius:6px;border:1px solid var(--border)" alt="${escapeHtml(file.name)}">`
                            : `<div style="padding:8px 12px;background:var(--surface);border-radius:6px;font-size:12px"><strong>${escapeHtml(file.name)}</strong> (${(file.size/1024).toFixed(1)} KB)</div>`
                        }
                    </div>
                `;
                chat.appendChild(row);
                scrollToBottom();
                
                // 发送到服务器
                _sendSafe({
                    type: 'file_upload',
                    name: file.name,
                    mime: file.type,
                    data: dataUrl
                });
            };
            reader.readAsDataURL(file);
        }
        // 清空 input 以便再次选择同一文件
        fileUpload.value = '';
    });
}

// ===== VOICE INPUT =====
const voiceBtn = document.getElementById('voice-btn');
let mediaRecorder = null;
let audioChunks = [];
let voiceMode = 'send'; // 'send' = 直接发送, 'asr' = 转文字

if (voiceBtn) {
    voiceBtn.addEventListener('click', async () => {
        if (!mediaRecorder || mediaRecorder.state === 'inactive') {
            // 录音中...显示模式选择
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];
                
                mediaRecorder.ondataavailable = (e) => {
                    audioChunks.push(e.data);
                };
                
                mediaRecorder.onstop = async () => {
                    const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                    const reader = new FileReader();
                    reader.onload = async (e) => {
                        if (voiceMode === 'send') {
                            // 模式1: 直接发送语音
                            if (window.wsSend && window.wsIsConnected()) {
                                // 显示语音消息
                                const row = document.createElement('div');
                                row.className = 'message-row user';
                                row.innerHTML = `
                                    <div class="message user">
                                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
                                            <span style="font-size:11px;color:var(--text-dim)">语音消息</span>
                                        </div>
                                        <audio controls src="${e.target.result}" style="max-width:200px"></audio>
                                    </div>
                                `;
                                chat.appendChild(row);
                                scrollToBottom();

                                window.wsSend({
                                    type: 'voice_input',
                                    audio: e.target.result,
                                    format: 'webm'
                                });
                                showToast('语音已发送');
                            } else {
                                showToast('⚠️ 未连接，请先建立连接');
                            }
                        } else {
                            // 模式2: 语音转文字
                            showToast('正在识别语音...');
                            try {
                                const response = await fetch('http://127.0.0.1:8765/asr', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ audio: e.target.result })
                                });
                                const result = await response.json();
                                if (result.text) {
                                    input.value = result.text;
                                    input.focus();
                                    showToast('已转换为文字，可编辑后发送');
                                } else {
                                    showToast('语音识别失败');
                                }
                            } catch (err) {
                                showToast('语音识别失败: ' + err.message);
                            }
                        }
                    };
                    reader.readAsDataURL(audioBlob);
                    
                    // 停止所有轨道
                    stream.getTracks().forEach(track => track.stop());
                };
                
                mediaRecorder.start();
                voiceBtn.style.color = 'var(--error)';
                voiceBtn.title = '点击切换模式: 当前=直接发送 | 右击=转文字';
                
                // 显示当前模式
                const modeText = voiceMode === 'send' ? '直接发送' : '转文字';
                showToast(`录音中 (${modeText}模式) - 点击停止`);
            } catch (err) {
                showToast('无法访问麦克风: ' + err.message);
            }
        } else {
            // 停止录音
            mediaRecorder.stop();
            voiceBtn.style.color = '';
            voiceBtn.title = '语音输入';
        }
    });
    
    // 右键切换模式
    voiceBtn.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        voiceMode = voiceMode === 'send' ? 'asr' : 'send';
        const modeText = voiceMode === 'send' ? '直接发送' : '转文字';
        showToast(`语音模式切换为: ${modeText}`);
    });
}

// ===== INIT =====
loadSessions();
loadSettings();
loadState();
renderSessionList();
if (!currentSessionId || sessions.length === 0) newSession();
else _rebuildRawTextMap();  // restore raw text map for copy/re-edit
loadDraft();  // restore saved draft on refresh

// ── Wire up core modules ──────────────────────────────────────────────────────
// Router: nav clicks, popstate, URL hash sync
if (window.initRouter) window.initRouter();

// WebSocket: inject the main_new.js message renderer as the global handler
// so all WS messages are processed by the existing switch statement below.
if (window.wsSetGlobalHandler) {
    window.wsSetGlobalHandler(_wsRenderer);
    window.wsOnConnect(_wsOnConnect);
    window.wsOnDisconnect(_wsOnDisconnect);
    window.wsOnError(_wsOnError);
    window.wsConnect();
}

// Placeholder so existing callers (e.g. voice input) don't break.
function connect() { if (window.wsConnect) window.wsConnect(); }

// ── Shortcuts Bar ──────────────────────────────────────────────────────────────
window.togglePlanMode = function() {
    planMode = !planMode;
    const btn = document.getElementById('sc-plan-btn');
    const bar = document.getElementById('plan-mode-bar');
    const toggleBtn = document.getElementById('toggle-plan');
    if (btn) btn.classList.toggle('active', planMode);
    if (bar) bar.style.display = planMode ? 'flex' : 'none';
    if (toggleBtn) toggleBtn.classList.toggle('active', planMode);
};

window.toggleLLMTooltip = function() {
    const tip = document.getElementById('quick-llm-tooltip');
    if (!tip) return;
    const isOpen = tip.classList.toggle('open');
    if (isOpen) {
        // Pre-fill from current settings
        const provEl = document.getElementById('ql-provider');
        const modelEl = document.getElementById('ql-model');
        const tempEl = document.getElementById('ql-temp');
        const tempVal = document.getElementById('ql-temp-val');
        if (provEl) provEl.value = settingProvider?.value || 'anthropic';
        if (modelEl) modelEl.value = modelBadge?.textContent || '';
        if (tempEl) {
            tempEl.value = settingTemp?.value || '0.7';
            if (tempVal) tempVal.textContent = tempEl.value;
        }
        // Close on outside click
        const closeHandler = (e) => {
            if (!tip.contains(e.target) && e.target.id !== 'sc-llm-btn') {
                tip.classList.remove('open');
                document.removeEventListener('click', closeHandler);
            }
        };
        setTimeout(() => document.addEventListener('click', closeHandler), 0);
    }
};

window.closeLLMTooltip = function() {
    document.getElementById('quick-llm-tooltip')?.classList.remove('open');
};

window.applyLLMQuickConfig = async function() {
    const provider = document.getElementById('ql-provider')?.value || 'anthropic';
    const model = document.getElementById('ql-model')?.value || '';
    const temp = document.getElementById('ql-temp')?.value || '0.7';
    closeLLMTooltip();

    // Update model badge
    if (modelBadge) modelBadge.textContent = model || provider;
    // Update shortcuts label
    const label = document.getElementById('sc-model-label');
    if (label) label.textContent = model ? model.slice(0, 8) : provider;

    // Persist to localStorage for next session
    localStorage.setItem('xmclaw_quick_llm', JSON.stringify({ provider, model, temp }));

    // Trigger settings save in background (non-blocking)
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        cfg.llm = cfg.llm || {};
        cfg.llm.default_provider = provider;
        cfg.llm[provider] = cfg.llm[provider] || {};
        if (model) cfg.llm[provider].default_model = model;
        if (settingTemp) settingTemp.value = temp;
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        showToast('LLM 配置已应用');
    } catch (e) {
        showToast('配置应用失败: ' + e.message);
    }
};

window.toggleEvolution = async function() {
    const btn = document.getElementById('sc-evo-btn');
    const enabled = !btn?.classList.contains('active');
    btn?.classList.toggle('active', enabled);
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        cfg.evolution = cfg.evolution || {};
        cfg.evolution.enabled = enabled;
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        showToast(`进化功能已${enabled ? '开启' : '关闭'}`);
    } catch {}
};

window.toggleChannelPanel = function() {
    switchView('settings');
    // Auto-switch to integrations tab
    setTimeout(() => _switchSettingsTab('integrations'), 50);
};

// Track whether chat is in wide (full-width) or split layout
let _chatLayoutWide = false;
window.toggleChatLayout = function() {
    const left = document.querySelector('.dashboard-left');
    const right = document.querySelector('.dashboard-right');
    _chatLayoutWide = !_chatLayoutWide;
    if (left) {
        if (_chatLayoutWide) {
            left.style.flex = '1';
            if (right) right.style.display = 'none';
        } else {
            left.style.flex = '';
            if (right) right.style.display = '';
        }
    }
    const btn = document.getElementById('sc-view-btn');
    if (btn) {
        btn.classList.toggle('active', _chatLayoutWide);
        btn.title = _chatLayoutWide ? '切换到分栏布局' : '切换到全宽布局';
    }
};

// Load quick LLM from localStorage on init
(function loadQuickLLM() {
    try {
        const saved = JSON.parse(localStorage.getItem('xmclaw_quick_llm') || '{}');
        if (saved.model) {
            const label = document.getElementById('sc-model-label');
            if (label) label.textContent = saved.model.slice(0, 8);
        }
    } catch {}
})();

// ── Enhanced inline edit overlay ─────────────────────────────────────────────
window._editMessage = function(btn) {
    const row = btn.closest('.message-row');
    const msgEl = row?.querySelector('.message');
    if (!msgEl) return;
    const original = msgEl.dataset.original || msgEl.textContent;

    const overlay = document.createElement('div');
    overlay.className = 'msg-edit-overlay';
    overlay.innerHTML = `
        <div class="msg-edit-box">
            <div class="msg-edit-header">编辑消息</div>
            <textarea class="msg-edit-textarea" id="edit-textarea">${escapeHtml(original)}</textarea>
            <div class="msg-edit-footer">
                <button class="cancel" id="edit-cancel">取消</button>
                <button class="submit" id="edit-submit">发送修改</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
    const ta = document.getElementById('edit-textarea');
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);

    document.getElementById('edit-cancel').onclick = () => overlay.remove();
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

    document.getElementById('edit-submit').onclick = () => {
        const newText = ta.value.trim();
        overlay.remove();
        if (!newText || newText === original.trim()) return;
        // Mark old row as superseded
        row.style.opacity = '0.4';
        // Send edited message
        input.value = newText;
        sendMessage();
    };

    ta.onkeydown = (e) => {
        if (e.key === 'Escape') overlay.remove();
        if (e.key === 'Enter' && e.ctrlKey) document.getElementById('edit-submit').click();
    };
};

// ── WS message renderer (registered as global handler above) ───────────────────
/**
 * Single-entry WS message renderer. Dispatches to typed handlers using
 * the same logic as the original ws.onmessage switch statement.
 * Registered via window.wsSetGlobalHandler so ws.js calls this for
 * every non-built-in WS message.
 */
function _wsRenderer(data) {
    // pong: no-op (already filtered in ws.js)
    if (data.type === 'pong') return;
    logConsole('ws', `收到消息: ${data.type}`, data);

    if (data.type === 'chunk') {
        removeTyping();
        isStreaming = true;
        if (!currentMessageEl) currentMessageEl = addMessage('', 'agent');
        appendChunk(currentMessageEl, data.content);
        
        // Live plan detection: update dev panel with current text
        if (window._devPanel) {
            const rawText = _rawTextMap.get(currentMessageEl) || '';
            // Detect plan patterns
            if (rawText.includes('计划') || rawText.includes('步骤') || rawText.includes('要做') || /\d+[.)：、]/.test(rawText)) {
                window._devPanel.setPlan(rawText);
                // Also add as thought
                if (rawText.length > 20) {
                    window._devPanel.addThought(rawText.slice(-200));
                }
            }
        }
        scrollToBottom();
    } else if (data.type === 'tool_start') {
        removeTyping();
        const toolName = data.tool || 'tool';
        const callId = data.call_id || `call_${toolName}_${Date.now()}`;
        setAgentState('TOOL_CALL', `正在使用 ${toolName}...`);
        activeTool.textContent = toolName;
        const { el, id } = _makeToolCard(toolName, callId, data.args || {});
        const row = document.createElement('div');
        row.className = 'message-row tool';
        row.appendChild(el);
        chat.appendChild(row);
        scrollToBottom();
        toolHistory.unshift({ tool: toolName, args: data.args, result: null, time: Date.now(), call_id: callId });
        if (toolHistory.length > 50) toolHistory.pop();
        renderRecentTools();
        renderToolLog();
        _toolCards.set(callId, { el, tool: toolName });
        _updateToolCard(id, 'start', {});
    } else if (data.type === 'tool_call') {
        removeTyping();
        const toolName = data.tool || 'tool';
        setAgentState('TOOL_CALL', `正在使用 ${toolName}...`);
        activeTool.textContent = toolName;
        toolHistory.unshift({ tool: toolName, args: data.args, result: null, time: Date.now() });
        if (toolHistory.length > 50) toolHistory.pop();
        renderRecentTools();
        renderToolLog();
        addToolMessage(toolName);
        scrollToBottom();
    } else if (data.type === 'tool_result') {
        if (toolHistory.length > 0) {
            toolHistory[0].result = data.result;
            renderRecentTools();
            renderToolLog();
        }
        const callId = data.call_id || '';
        if (callId && _toolCards.has(callId)) {
            _updateToolCard(callId, 'result', data);
            activeTool.textContent = '—';
            setAgentState('THINKING', '处理结果中...');
        } else {
            addToolResultMessage(data.tool, data.result);
            activeTool.textContent = '—';
            setAgentState('THINKING', 'Processing result...');
        }
    } else if (data.type === 'file_op') {
        setAgentState('SELF_MOD', `Modified ${data.file || 'file'}`);
        activeFile.textContent = `${data.action || 'write'}: ${data.file || '-'}`;
        addSelfMod(data.file, data.action);
        // Forward to dev panel file diff tracker
        if (window._devPanel) {
            window._devPanel.addFileChange(data.file || '', data.action || 'write');
        }
        if (currentView !== 'dashboard') showToast(`Self-mod: ${data.action} ${data.file}`);
    } else if (data.type === 'state') {
        setAgentState(data.state, data.thought);
        // When entering PLANNING state, open the dev panel to plan tab
        if (data.state === 'PLANNING' && window._devPanel) {
            window._devPanel.switchTab('plan');
            window._devPanel.open();
        }
    } else if (data.type === 'done') {
        removeTyping();
        if (currentMessageEl) flushChunk(currentMessageEl);
        currentMessageEl = null;
        isStreaming = false;
        setAgentState('IDLE', 'Waiting for input...');
        activeTool.textContent = '—';
        activeFile.textContent = '—';
        saveCurrentSession();
        persistSessions();
    } else if (data.type === 'error') {
        removeTyping();
        addMessage(data.content, 'error');
        currentMessageEl = null;
        setAgentState('IDLE', 'Error occurred');
    } else if (data.type === 'cost') {
        totalTokens += data.tokens || 0;
        totalCost += data.cost || 0;
        tokenDisplay.textContent = `${totalTokens.toLocaleString()} tokens`;
        costDisplay.textContent = `$${totalCost.toFixed(4)}`;
        persistState();
    } else if (data.type === 'evolution') {
        if (data.gene) geneCount++;
        if (data.skill) skillCount++;
        evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        addTimelineEvent(data.subtype || 'gene', data.title, data.desc);
        persistState();
    } else if (data.type === 'reflection') {
        addReflectionMessage(data.data || {}, data.improvement || {});
        addTimelineEvent('reflection', '反思完成', data.data?.summary || '');
    } else if (data.type === 'ask_user') {
        showAskUserDialog(data.question);
    } else if (data.type === 'event') {
        handleBusEvent(data.event);
    } else if (data.type === 'transcription') {
        input.value = data.text || '';
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }
}

// ── WS lifecycle hooks (registered above) ──────────────────────────────────────
function _wsOnConnect() {
    statusDot.classList.add('connected');
    statusText.textContent = 'Connected';
    statusText.style.color = 'var(--accent)';
    loadTodos();
    loadTasks();
    loadEvolutionStatus();
    if (window.wsQueuedCount && window.wsQueuedCount() > 0) {
        showToast(`⚠️ ${window.wsQueuedCount()} 条消息因离线未能发送`);
    }
}

function _wsOnDisconnect(delay) {
    statusDot.classList.remove('connected');
    const delaySec = Math.round(delay / 1000);
    statusText.textContent = `Reconnecting in ${delaySec}s...`;
    statusText.style.color = 'var(--text-dim)';
}

function _wsOnError(e) {
    console.error('ws_error', e);
    logConsole('ws_error', 'WebSocket error', {});
    statusText.textContent = 'Connection error';
    statusText.style.color = '#ef9a9a';
}

// ===== WEBSOCKET CORE (inline) =====
const WS_URL = 'ws://127.0.0.1:8765/agent/default';
var _ws = null;
var _wsReconnectDelay = 1000;
var _wsMaxReconnectDelay = 5000;  // Max 5 seconds
var _wsIntentionalClose = false;
var _wsQueued = [];
var _wsReconnectTimer = null;

function _wsCreate() {
    if (_ws && _ws.readyState === WebSocket.OPEN) return;
    if (_ws && _ws.readyState === WebSocket.CONNECTING) return;
    _wsIntentionalClose = false;
    
    try {
        _ws = new WebSocket(WS_URL);
    } catch(e) {
        console.error('[WS] Create failed:', e);
        _scheduleReconnect();
        return;
    }
    
    _ws.onopen = () => {
        console.log('[WS] Connected');
        _wsReconnectDelay = 1000;  // Reset delay on success
        if (_onConnect) _onConnect();
        // Flush queued messages
        while (_wsQueued.length > 0) {
            _ws.send(_wsQueued.shift());
        }
    };
    
    _ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'pong') return;
            if (_globalHandler) _globalHandler(data);
        } catch(e) {
            console.error('[WS] Parse error:', e);
        }
    };
    
    _ws.onclose = () => {
        console.log('[WS] Disconnected');
        if (!_wsIntentionalClose) {
            if (_onDisconnect) _onDisconnect(_wsReconnectDelay);
            _scheduleReconnect();
        }
    };
    
    _ws.onerror = (error) => {
        console.error('[WS] Error:', error);
        if (_onError) _onError(error);
    };
}

function _scheduleReconnect() {
    if (_wsReconnectTimer) clearTimeout(_wsReconnectTimer);
    _wsReconnectTimer = setTimeout(() => {
        console.log(`[WS] Reconnecting in ${_wsReconnectDelay}ms...`);
        _wsCreate();
        _wsReconnectDelay = Math.min(_wsReconnectDelay * 1.5, _wsMaxReconnectDelay);
    }, _wsReconnectDelay);
}

// Global functions
function wsConnect() { _wsCreate(); }

function wsSend(payload) {
    const msg = JSON.stringify(payload);
    if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(msg);
        return true;
    }
    _wsQueued.push(msg);
    return false;
}

function wsQueuedCount() { return _wsQueued.length; }

function wsSetGlobalHandler(handler) { _globalHandler = handler; }
function wsOnConnect(cb) { _onConnect = cb; }
function wsOnDisconnect(cb) { _onDisconnect = cb; }
function wsOnError(cb) { _onError = cb; }

// Export
window.wsConnect = wsConnect;
window.wsSend = wsSend;
window.wsQueuedCount = wsQueuedCount;
window.wsSetGlobalHandler = wsSetGlobalHandler;
window.wsOnConnect = wsOnConnect;
window.wsOnDisconnect = wsOnDisconnect;
window.wsOnError = wsOnError;

// ===== DEV PANEL: Operation Visualization =====
class DevPanel {
    constructor() {
        this.panel = document.getElementById('dev-panel');
        this.planContent = document.getElementById('dev-plan-content');
        this.filesList = document.getElementById('dev-files-list');
        this.diffContent = document.getElementById('dev-diff-content');
        this.thoughtsContent = document.getElementById('dev-thoughts-content');
        
        this.fileChanges = [];
        this.diffs = [];
        this.thoughts = [];
        
        this.init();
    }
    
    init() {
        // Tab switching
        document.querySelectorAll('.dev-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const tabName = tab.dataset.tab;
                this.switchTab(tabName);
            });
        });
        
        // Close button
        document.getElementById('dev-panel-close')?.addEventListener('click', () => {
            this.close();
        });
    }
    
    open() {
        if (this.panel) {
            this.panel.style.display = 'flex';
        }
    }
    
    close() {
        if (this.panel) {
            this.panel.style.display = 'none';
        }
    }
    
    switchTab(tabName) {
        // Update tab buttons
        document.querySelectorAll('.dev-tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.tab === tabName);
        });
        
        // Update tab content
        document.querySelectorAll('.dev-tab-content').forEach(content => {
            content.classList.toggle('active', content.id === `dev-tab-${tabName}`);
        });
    }
    
    setPlan(planText) {
        if (!this.planContent) return;
        
        // Parse plan into steps
        const steps = this.parsePlan(planText);
        
        if (steps.length > 0) {
            this.planContent.innerHTML = steps.map((step, i) => `
                <div class="dev-plan-item">
                    <div class="dev-plan-step ${i === 0 ? 'current' : ''}">${i + 1}</div>
                    <div class="dev-plan-text">
                        ${this.escapeHtml(step)}
                        <div class="dev-plan-status">${i === 0 ? '执行中...' : '待执行'}</div>
                    </div>
                </div>
            `).join('');
        }
    }
    
    parsePlan(text) {
        if (!text) return [];
        
        // Try to extract numbered steps
        const lines = text.split('\n');
        const steps = [];
        
        for (const line of lines) {
            const trimmed = line.trim();
            // Match patterns like "1. xxx", "1) xxx", "步骤1: xxx", "Step 1: xxx"
            const match = trimmed.match(/^[\d一二三四五六七八九十]+[.、:：)）]\s*(.+)/);
            if (match) {
                steps.push(match[1]);
            } else if (trimmed.length > 10 && trimmed.length < 200 && !trimmed.startsWith('#')) {
                // Heuristic: long lines without numbering could be plan items
                if (steps.length > 0 || trimmed.includes('计划') || trimmed.includes('步骤')) {
                    steps.push(trimmed);
                }
            }
        }
        
        // If no structured steps found, use the whole text
        if (steps.length === 0 && text.length > 50) {
            return [text];
        }
        
        return steps.slice(0, 10); // Max 10 steps
    }
    
    addFileChange(file, action) {
        if (!this.filesList) return;
        
        this.fileChanges.push({
            file,
            action,
            time: new Date().toLocaleTimeString()
        });
        
        this.renderFiles();
    }
    
    renderFiles() {
        if (this.fileChanges.length === 0) {
            this.filesList.innerHTML = '<div class="dev-empty">暂无文件变化...</div>';
            return;
        }
        
        this.filesList.innerHTML = this.fileChanges.map(change => `
            <div class="dev-file-item">
                <span class="dev-file-action ${change.action === 'create' ? 'create' : change.action === 'delete' ? 'delete' : 'modify'}">${change.action}</span>
                <span class="dev-file-path">${this.escapeHtml(change.file)}</span>
                <span class="dev-file-time">${change.time}</span>
            </div>
        `).join('');
    }
    
    addDiff(file, diffText) {
        if (!this.diffContent) return;
        
        this.diffs.push({
            file,
            diff: diffText,
            time: new Date().toLocaleTimeString()
        });
        
        this.renderDiffs();
    }
    
    renderDiffs() {
        if (this.diffs.length === 0) {
            this.diffContent.innerHTML = '<div class="dev-empty">暂无代码变更...</div>';
            return;
        }
        
        this.diffContent.innerHTML = this.diffs.map(d => `
            <div class="dev-diff-file">
                <div class="dev-diff-header">${this.escapeHtml(d.file)}</div>
                <div class="dev-diff-body">
                    ${this.renderDiffLines(d.diff)}
                </div>
            </div>
        `).join('');
    }
    
    renderDiffLines(diff) {
        if (!diff) return '<div class="dev-diff-line context"><span class="dev-diff-line-code">No diff available</span></div>';
        
        const lines = diff.split('\n');
        let lineNum = 1;
        
        return lines.map(line => {
            let cls = 'context';
            if (line.startsWith('+')) cls = 'add';
            else if (line.startsWith('-')) cls = 'remove';
            
            return `
                <div class="dev-diff-line ${cls}">
                    <span class="dev-diff-line-num">${lineNum++}</span>
                    <span class="dev-diff-line-code">${this.escapeHtml(line)}</span>
                </div>
            `;
        }).join('');
    }
    
    addThought(thought) {
        if (!this.thoughtsContent) return;
        
        this.thoughts.push({
            thought,
            time: new Date().toLocaleTimeString()
        });
        
        this.renderThoughts();
    }
    
    renderThoughts() {
        if (this.thoughts.length === 0) {
            this.thoughtsContent.innerHTML = '<div class="dev-empty">暂无思考记录...</div>';
            return;
        }
        
        this.thoughtsContent.innerHTML = this.thoughts.map(t => `
            <div class="dev-thought-item">
                <div class="dev-thought-time">${t.time}</div>
                <div class="dev-thought-text">${this.escapeHtml(t.thought)}</div>
            </div>
        `).join('');
    }
    
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize DevPanel when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        window._devPanel = new DevPanel();
    });
} else {
    window._devPanel = new DevPanel();
}
