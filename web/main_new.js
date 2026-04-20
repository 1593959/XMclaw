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

// Plan v2 E6: log of turns committed in this session. The annotation
// sidebar (Shift+R) rehydrates this list and merges with existing
// feedback rows from GET /feedback/recent.
const _turnLog = [];

// Tool card tracking: call_id -> { el, tool, startTime }
const _toolCards = new Map();

// Evolution Live (Phase E0): cycle_id -> { state, startedAt, endedAt, verdict, artifacts[], rejectReason }
const _journalCycles = new Map();
// Order cycles appear on screen — newest first. IDs appended as they're opened.
const _journalOrder = [];

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
    panel.innerHTML = consoleLogs.slice(0, 80).map(log => {
        const color = log.type === 'error' ? '#ff6b6b' :
                      log.type === 'ws'    ? '#60a5fa' :
                      log.type === 'tool' ? '#ffc107' :
                      log.type === 'state' ? '#4caf50' : '#9ca3af';
        const dataStr = log.data ? JSON.stringify(log.data, null, 0) : '';
        const truncated = dataStr.length > 300 ? dataStr.substring(0, 300) + '…' : dataStr;
        return `<div class="console-entry">
            <span class="console-time">${log.time}</span>
            <span class="console-tag" style="color:${color}">[${log.type}]</span>
            <span class="console-msg">${escapeHtml(log.msg || '')}</span>
            ${truncated ? `<pre class="console-data">${escapeHtml(truncated)}</pre>` : ''}
        </div>`;
    }).join('');
}

function toggleConsolePanel() {
    const panel = document.getElementById('console-logs');
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
        tab?.addEventListener('click', () => {
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
    feishu:   [['integ-feishu-enabled','enabled','bool'],['integ-feishu-app-id','app_id'],['integ-feishu-app-secret','app_secret'],['integ-feishu-bot-name','bot_name'],['integ-feishu-default-chat-id','default_chat_id']],
    qq:       [['integ-qq-enabled','enabled','bool'],['integ-qq-mode','mode'],['integ-qq-app-id','app_id'],['integ-qq-app-token','app_token'],['integ-qq-secret','secret'],['integ-qq-ws-url','ws_url'],['integ-qq-channel-id','channel_id']],
    wechat:   [['integ-wechat-enabled','enabled','bool'],['integ-wechat-mode','mode'],['integ-wechat-webhook-url','webhook_url'],['integ-wechat-corp-id','corp_id'],['integ-wechat-agent-id','agent_id'],['integ-wechat-app-secret','app_secret'],['integ-wechat-callback-token','callback_token'],['integ-wechat-callback-aes-key','callback_aes_key']],
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
    const icons = { slack: '💬', discord: '🎮', telegram: '✈️', github: '🐙', notion: '📝', feishu: '🪁', qq: '🐧', wechat: '💼' };
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
    div.querySelector('.mcp-del')?.addEventListener('click', () => div.remove());
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

togglePlanBtn?.addEventListener('click', () => {
    planMode = !planMode;
    togglePlanBtn.classList.toggle('active', planMode);
    planModeBar.style.display = planMode ? 'flex' : 'none';
});

cancelPlanBtn?.addEventListener('click', () => {
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

clearToolsBtn?.addEventListener('click', () => {
    clearRecentTools();
});

function togglePanel(headerEl) {
    const panel = headerEl.closest('.panel');
    if (!panel) return;
    panel.classList.toggle('collapsed');
}

function clearRecentTools() {
    toolHistory = [];
    renderRecentTools();
    renderToolLog();
    persistState();
}

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

btnTestGenerate?.addEventListener('click', () => runTestAction('generate', testTarget.value.trim()));
btnTestRun?.addEventListener('click', () => runTestAction('run', testTarget.value.trim()));
btnTestRunAll?.addEventListener('click', () => runTestAction('run_all'));

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

addTodoBtn?.addEventListener('click', async () => {
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

addTaskBtn?.addEventListener('click', async () => {
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

// ── Real-time Tool Pattern Tracking (Evolution Observation) ────────────────────
const _toolPatterns = {};  // { toolName: { count, threshold } }

function updateToolPatternDisplay(tool, count, threshold) {
    _toolPatterns[tool] = { count, threshold };

    // Update the dedicated observation panel if it exists
    const panel = document.getElementById('evo-pattern-panel');
    if (!panel) return;

    const sortedTools = Object.entries(_toolPatterns)
        .sort((a, b) => b[1].count - a[1].count);

    let html = '<div class="pattern-list">';
    for (const [name, data] of sortedTools) {
        const pct = Math.min(100, Math.round((data.count / data.threshold) * 100));
        const barColor = pct >= 100 ? '#10b981' : pct >= 60 ? '#f59e0b' : '#6366f1';
        const firing = pct >= 100 ? '🔥' : '○';
        html += `<div class="pattern-item">
            <div class="pattern-row">
                <span class="pattern-firing">${firing}</span>
                <span class="pattern-name">${escapeHtml(name)}</span>
                <span class="pattern-count">${data.count}/${data.threshold}</span>
            </div>
            <div class="pattern-bar-bg">
                <div class="pattern-bar-fill" style="width:${pct}%;background:${barColor}"></div>
            </div>
        </div>`;
    }
    if (sortedTools.length === 0) {
        html += '<div class="empty-state" style="padding:12px;text-align:center">工具调用将实时显示在此</div>';
    }
    html += '</div>';
    panel.innerHTML = html;
}

function clearToolPatterns() {
    Object.keys(_toolPatterns).forEach(k => delete _toolPatterns[k]);
    updateToolPatternDisplay('', 0, 3);
}

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
        log.innerHTML = (typeof TOOL_LOG_EMPTY_HTML !== 'undefined')
            ? TOOL_LOG_EMPTY_HTML
            : '<div class="empty-state">No tool executions yet.</div>';
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

// ── Workspace File Tree ──────────────────────────────────────────────────────────
const fileTree = document.getElementById('file-tree');
const workspaceEditor = document.getElementById('workspace-editor');
const editorPath = document.getElementById('editor-path');
const saveFileBtn = document.getElementById('save-file-btn');
const workspaceSearchInput = document.getElementById('workspace-search');
const workspaceSummary = document.getElementById('workspace-summary');

let currentFilePath = null;
let currentFileBaseline = '';   // last-saved content for dirty detection
let allWorkspaceFiles = [];     // full list from API
let visibleWorkspaceFiles = []; // filtered for search
let expandedDirs = new Set();  // tracks which dirs are open
let contextMenuTarget = null; // file/dir the user right-clicked

function _editorIsDirty() {
    return currentFilePath && workspaceEditor && workspaceEditor.value !== currentFileBaseline;
}

function _updateEditorDirtyBadge() {
    if (!editorPath || !currentFilePath) return;
    const prefix = _editorIsDirty() ? '● ' : '';
    editorPath.textContent = prefix + currentFilePath;
}

// File type icons and colors
const FILE_TYPE_STYLE = {
    python:     { icon: '🐍', color: '#4FC3F7', bg: 'rgba(79,195,247,0.1)' },
    javascript: { icon: '📜', color: '#FFD54F', bg: 'rgba(255,213,79,0.1)' },
    typescript: { icon: '📘', color: '#90CAF9', bg: 'rgba(144,202,249,0.1)' },
    json:       { icon: '📋', color: '#A5D6A7', bg: 'rgba(165,214,167,0.1)' },
    markdown:   { icon: '📝', color: '#CE93D8', bg: 'rgba(206,147,216,0.1)' },
    yaml:       { icon: '⚙️',  color: '#80CBC4', bg: 'rgba(128,203,196,0.1)' },
    config:     { icon: '⚙️',  color: '#BCAAA4', bg: 'rgba(188,170,164,0.1)' },
    shell:      { icon: '💻', color: '#B0BEC5', bg: 'rgba(176,190,197,0.1)' },
    html:       { icon: '🌐', color: '#FFAB91', bg: 'rgba(255,171,145,0.1)' },
    css:        { icon: '🎨', color: '#F48FB1', bg: 'rgba(244,143,177,0.1)' },
    image:      { icon: '🖼️', color: '#81C784', bg: 'rgba(129,199,132,0.1)' },
    pdf:        { icon: '📄', color: '#EF9A9A', bg: 'rgba(239,154,154,0.1)' },
    data:       { icon: '📊', color: '#FFF59D', bg: 'rgba(255,245,157,0.1)' },
    database:   { icon: '🗄️', color: '#B39DDB', bg: 'rgba(179,157,219,0.1)' },
    xml:        { icon: '📐', color: '#FFCC80', bg: 'rgba(255,204,128,0.1)' },
    log:        { icon: '📜', color: '#90A4AE', bg: 'rgba(144,164,174,0.1)' },
    env:        { icon: '🔑', color: '#EF5350', bg: 'rgba(239,83,80,0.1)' },
    text:       { icon: '📃', color: '#CFD8DC', bg: 'rgba(207,216,220,0.1)' },
    folder:     { icon: '📁', color: '#FFB74D', bg: 'rgba(255,183,77,0.1)' },
    file:       { icon: '📄', color: '#B0BEC5', bg: 'rgba(176,190,197,0.1)' },
};

function _getStyle(ft) {
    return FILE_TYPE_STYLE[ft] || FILE_TYPE_STYLE.file;
}

// Build a directory tree from flat file list
// Returns: { dirs: [...], files: [...], children: { 'dir/path': { dirs, files } } }
function _buildTree(flatFiles) {
    const rootDirs = [];
    const rootFiles = [];
    const children = {}; // parentPath -> { dirs, files }

    for (const f of flatFiles) {
        const parts = f.path.split('/');
        if (f.type === 'dir') {
            if (parts.length === 1) {
                rootDirs.push(f);
            } else {
                const parent = parts.slice(0, -1).join('/');
                if (!children[parent]) children[parent] = { dirs: [], files: [] };
                children[parent].dirs.push(f);
            }
        } else {
            if (parts.length === 1) {
                rootFiles.push(f);
            } else {
                const parent = parts.slice(0, -1).join('/');
                if (!children[parent]) children[parent] = { dirs: [], files: [] };
                children[parent].files.push(f);
            }
        }
    }
    rootDirs.sort((a, b) => a.name.localeCompare(b.name));
    rootFiles.sort((a, b) => a.name.localeCompare(b.name));
    for (const key of Object.keys(children)) {
        children[key].dirs.sort((a, b) => a.name.localeCompare(b.name));
        children[key].files.sort((a, b) => a.name.localeCompare(b.name));
    }
    return { dirs: rootDirs, files: rootFiles, children };
}

async function loadWorkspaceFiles() {
    fileTree.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/files`);
        const data = await res.json();
        allWorkspaceFiles = data.files || [];
        if (workspaceSummary) {
            const s = data.summary || {};
            workspaceSummary.textContent = `${s.files || 0} 文件 · ${s.dirs || 0} 文件夹`;
            workspaceSummary.style.display = '';
        }
        // Auto-expand root-level directories so the tree isn't empty on first load
        for (const f of allWorkspaceFiles) {
            if (f.type === 'dir' && !f.path.includes('/')) {
                expandedDirs.add(f.path);
            }
        }
        applyWorkspaceSearch();
    } catch {
        fileTree.innerHTML = '<div class="empty-state">加载失败</div>';
    }
}

function applyWorkspaceSearch() {
    const q = (workspaceSearchInput?.value || '').toLowerCase().trim();
    if (!q) {
        visibleWorkspaceFiles = [...allWorkspaceFiles];
    } else {
        visibleWorkspaceFiles = allWorkspaceFiles.filter(f =>
            f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q)
        );
        // When searching, expand all matching parent dirs
        const matchPaths = new Set(visibleWorkspaceFiles.map(f => f.path));
        for (const f of visibleWorkspaceFiles) {
            if (f.type === 'dir') continue;
            const parts = f.path.split('/');
            for (let i = 1; i < parts.length; i++) {
                expandedDirs.add(parts.slice(0, i).join('/'));
            }
        }
    }
    renderFileTree();
}

function renderFileTree() {
    if (allWorkspaceFiles.length === 0) {
        // A single-line "工作区为空" was too terse — after the workspace
        // was re-rooted at agents/<id>/workspace/ (PR #17) the folder is
        // legitimately empty on fresh installs, and the UI gave no hint
        // about why or what to do. This card explains both.
        fileTree.innerHTML = `
            <div class="empty-state-rich">
                <span class="es-icon">📂</span>
                <div class="es-title">工作区为空</div>
                <div class="es-body">
                    代理还没有在这里创建任何文件。
                    让它执行"把 XX 写到 <code>notes.md</code>"之类的任务，
                    文件就会出现在这里；也可以用工具栏的刷新按钮重新加载。
                </div>
            </div>`;
        return;
    }
    if (visibleWorkspaceFiles.length === 0) {
        fileTree.innerHTML = '<div class="empty-state">没有匹配的文件</div>';
        return;
    }

    const tree = _buildTree(visibleWorkspaceFiles);
    const html = _renderNode('', tree, 0);
    fileTree.innerHTML = html;
}

function _renderNode(parentKey, node, depth) {
    let html = '';
    const indentPx = depth * 14;

    // Directories first
    for (const d of node.dirs) {
        const key = d.path;
        const isOpen = expandedDirs.has(key);
        const childData = node.children[key] || { dirs: [], files: [] };
        const subCount = childData.dirs.length + childData.files.length;

        html += `<div class="ft-entry ft-dir ${isOpen ? 'ft-open' : ''}"
                     data-path="${escapeHtml(key)}" data-type="dir"
                     style="padding-left:${indentPx}px"
                     onclick="toggleDir('${escapeHtml(key)}', event)"
                     oncontextmenu="showContextMenu(event, '${escapeHtml(key)}', 'dir')">
                    <span class="ft-arrow">${isOpen ? '▼' : '▶'}</span>
                    <span class="ft-icon">📁</span>
                    <span class="ft-name">${escapeHtml(d.name)}</span>
                    <span class="ft-count">${subCount > 0 ? subCount : ''}</span>
                </div>`;

        if (isOpen) {
            html += `<div class="ft-children" id="ftc-${key.replace(/\//g, '__')}">`;
            html += _renderNode(key, childData, depth + 1);
            html += `</div>`;
        }
    }

    // Files
    for (const f of node.files) {
        const style = _getStyle(f.fileType);
        const isActive = f.path === currentFilePath;
        html += `<div class="ft-entry ft-file ${isActive ? 'ft-active' : ''}"
                     data-path="${escapeHtml(f.path)}" data-type="file"
                     style="padding-left:${indentPx}px"
                     onclick="openWorkspaceFile('${escapeHtml(f.path)}')"
                     oncontextmenu="showContextMenu(event, '${escapeHtml(f.path)}', 'file')">
                    <span class="ft-icon" style="color:${style.color}">${style.icon}</span>
                    <span class="ft-name" title="${escapeHtml(f.path)}">${escapeHtml(f.name)}</span>
                    ${f.sizeLabel ? `<span class="ft-size">${f.sizeLabel}</span>` : ''}
                </div>`;
    }

    return html;
}

window.toggleDir = function(path, event) {
    event.stopPropagation();
    if (expandedDirs.has(path)) {
        expandedDirs.delete(path);
    } else {
        expandedDirs.add(path);
    }
    renderFileTree();
};

window.showContextMenu = function(event, path, type) {
    event.preventDefault();
    event.stopPropagation();
    closeContextMenu();
    contextMenuTarget = { path, type };

    const menu = document.createElement('div');
    menu.id = 'ctx-menu';
    menu.className = 'ctx-menu';

    const isDir = type === 'dir';

    menu.innerHTML = `
        ${!isDir ? `<div class="ctx-item ctx-delete" onclick="ctxDelete()">🗑️ 删除</div>` : ''}
        ${isDir ? `<div class="ctx-item" onclick="ctxNewFile()">📄 新建文件</div>` : ''}
        ${isDir ? `<div class="ctx-item" onclick="ctxNewDir()">📁 新建文件夹</div>` : ''}
        <div class="ctx-item" onclick="ctxRename()">✏️ 重命名</div>
        ${!isDir ? `<div class="ctx-item" onclick="ctxCopyPath()">📋 复制路径</div>` : ''}
    `;

    document.body.appendChild(menu);

    // Position near cursor, clamp to viewport
    let x = event.clientX;
    let y = event.clientY;
    const rect = menu.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (x + rect.width > vw) x = vw - rect.width - 8;
    if (y + rect.height > vh) y = vh - rect.height - 8;
    menu.style.left = x + 'px';
    menu.style.top  = y + 'px';
};

function closeContextMenu() {
    document.getElementById('ctx-menu')?.remove();
    contextMenuTarget = null;
}
document.addEventListener('click', closeContextMenu);

window.ctxDelete = async function() {
    closeContextMenu();
    if (!contextMenuTarget) return;
    const { path, type } = contextMenuTarget;
    if (!confirm(`确定删除${type === 'dir' ? '文件夹' : '文件'} "${path}" 吗？`)) return;
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(await res.text());
        if (currentFilePath === path) {
            currentFilePath = null;
            workspaceEditor.value = '';
            editorPath.textContent = '未选择文件';
            saveFileBtn.style.display = 'none';
        }
        await loadWorkspaceFiles();
        showToast('已删除');
    } catch (e) {
        showToast('删除失败: ' + e.message);
    }
};

window.ctxNewFile = function() {
    closeContextMenu();
    const name = prompt('文件名:', 'untitled.py');
    if (!name || !name.trim()) return;
    const parent = contextMenuTarget?.path || '';
    const relPath = parent ? `${parent}/${name}` : name;
    createWorkspaceEntry(relPath, false);
};

window.ctxNewDir = function() {
    closeContextMenu();
    const name = prompt('文件夹名:', 'new_folder');
    if (!name || !name.trim()) return;
    const parent = contextMenuTarget?.path || '';
    const relPath = parent ? `${parent}/${name}` : name;
    createWorkspaceEntry(relPath, true);
};

window.ctxRename = function() {
    closeContextMenu();
    if (!contextMenuTarget) return;
    const { path } = contextMenuTarget;
    const newName = prompt('新名称:', path.split('/').pop());
    if (!newName || !newName.trim() || newName === path.split('/').pop()) return;
    renameWorkspaceEntry(path, newName);
};

window.ctxCopyPath = function() {
    closeContextMenu();
    if (!contextMenuTarget) return;
    navigator.clipboard.writeText(contextMenuTarget.path).then(() => showToast('路径已复制'));
};

async function createWorkspaceEntry(relPath, isDir) {
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file/create`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: relPath, is_dir: isDir }),
        });
        if (!res.ok) throw new Error(await res.text());
        if (!isDir) {
            openWorkspaceFile(relPath);
        } else {
            expandedDirs.add(relPath);
        }
        await loadWorkspaceFiles();
        showToast(isDir ? '文件夹已创建' : '文件已创建');
    } catch (e) {
        showToast('创建失败: ' + e.message);
    }
}

async function renameWorkspaceEntry(oldPath, newName) {
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: oldPath, new_name: newName }),
        });
        if (!res.ok) throw new Error(await res.text());
        if (currentFilePath === oldPath) {
            currentFilePath = oldPath.split('/').slice(0, -1).concat([newName]).join('/');
        }
        await loadWorkspaceFiles();
        showToast('已重命名');
    } catch (e) {
        showToast('重命名失败: ' + e.message);
    }
}

window.openWorkspaceFile = async function(path) {
    // Guard: if the current file has unsaved edits, confirm before discarding.
    if (_editorIsDirty() && !confirm(`「${currentFilePath}」有未保存的修改，确定要切换吗？`)) {
        return;
    }
    currentFilePath = path;
    editorPath.textContent = path;
    workspaceEditor.value = '加载中...';
    workspaceEditor.readOnly = true;
    saveFileBtn.style.display = 'none';
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        const content = data.content || '';
        workspaceEditor.value = content;
        currentFileBaseline = content;
        workspaceEditor.readOnly = false;
        saveFileBtn.style.display = 'inline-block';
        _updateEditorDirtyBadge();
        renderFileTree(); // update active highlight
    } catch (e) {
        workspaceEditor.value = '加载失败: ' + e.message;
    }
};

async function _saveCurrentWorkspaceFile() {
    if (!currentFilePath) return;
    if (workspaceEditor.readOnly) return; // nothing loaded
    const content = workspaceEditor.value;
    try {
        const res = await fetch(`/api/agent/${AGENT_ID}/file?path=${encodeURIComponent(currentFilePath)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            throw new Error(`HTTP ${res.status}${body ? ': ' + body.slice(0, 120) : ''}`);
        }
        currentFileBaseline = content;
        _updateEditorDirtyBadge();
        showToast('文件已保存');
    } catch (e) {
        showToast('保存失败: ' + (e.message || e));
    }
}

saveFileBtn?.addEventListener('click', _saveCurrentWorkspaceFile);

// Live dirty indicator — update on every keystroke so the ● disappears
// after a save and reappears as soon as the user types again.
workspaceEditor?.addEventListener('input', _updateEditorDirtyBadge);

// Ctrl+S / Cmd+S to save when the editor has focus.
workspaceEditor?.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        _saveCurrentWorkspaceFile();
    }
});

// Warn before the user closes the tab with unsaved workspace edits.
window.addEventListener('beforeunload', (e) => {
    if (_editorIsDirty()) {
        e.preventDefault();
        e.returnValue = ''; // required by some browsers to trigger the prompt
    }
});

workspaceSearchInput?.addEventListener('input', applyWorkspaceSearch);
document.getElementById('workspace-refresh')?.addEventListener('click', loadWorkspaceFiles);

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

            // Genes list — prefer human-readable .name (from sidecar JSON);
            // fall back to the hex ID when metadata is missing, but still show
            // the ID as a subtitle so operators can grep logs.
            if (data.genes && data.genes.length) {
                html += '<h4 style="margin:14px 0 6px;font-size:13px;color:var(--text-dim)">Genes</h4>';
                html += '<div class="entity-list">';
                for (const g of data.genes) {
                    const entityId = g.id || g.name;
                    const display = g.name && g.name !== entityId ? g.name : entityId;
                    const badge = g.category ? `<span class="entity-badge">${escapeHtml(g.category)}</span>` : '';
                    const version = g.version ? `<span class="entity-version">${escapeHtml(g.version)}</span>` : '';
                    const desc = g.description && g.description !== 'Gene'
                        ? `<div class="entity-desc">${escapeHtml(g.description)}</div>` : '';
                    const subId = display !== entityId
                        ? `<div class="entity-id">${escapeHtml(entityId)}</div>` : '';
                    html += `<div class="entity-item" onclick="loadEntity('gene','${escapeHtml(entityId)}')">
                        <div class="entity-row">
                            <span class="entity-name">${escapeHtml(display)}</span>
                            ${badge}${version}
                            <span class="entity-type">Gene</span>
                        </div>
                        ${desc}${subId}
                    </div>`;
                }
                html += '</div>';
            }

            // Skills list — same treatment as genes. Without this the page
            // was a wall of 60+ raw hex IDs like ``skill_01ae10a3`` with no
            // way to tell them apart at a glance.
            if (data.skills && data.skills.length) {
                html += '<h4 style="margin:14px 0 6px;font-size:13px;color:var(--text-dim)">Skills</h4>';
                html += '<div class="entity-list">';
                for (const s of data.skills) {
                    const entityId = s.id || s.name;
                    const display = s.name && s.name !== entityId ? s.name : entityId;
                    const badge = s.category ? `<span class="entity-badge">${escapeHtml(s.category)}</span>` : '';
                    const version = s.version ? `<span class="entity-version">${escapeHtml(s.version)}</span>` : '';
                    const desc = s.description && s.description !== 'Skill'
                        ? `<div class="entity-desc">${escapeHtml(s.description)}</div>` : '';
                    const subId = display !== entityId
                        ? `<div class="entity-id">${escapeHtml(entityId)}</div>` : '';
                    html += `<div class="entity-item" onclick="loadEntity('skill','${escapeHtml(entityId)}')">
                        <div class="entity-row">
                            <span class="entity-name">${escapeHtml(display)}</span>
                            ${badge}${version}
                            <span class="entity-type">Skill</span>
                        </div>
                        ${desc}${subId}
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


// ── Evolution Live journal panel (Phase E0 / PR-E0-4) ───────────────────────
// The daemon emits per-type wire events (see WS_EVENT_MAP in event_bus.py).
// We collect them into _journalCycles keyed by payload.cycle_id and render a
// deterministic timeline so the user can watch a cycle flow through states.

const _JOURNAL_STATE_ORDER = [
    'cycle_started', 'reflecting', 'forging', 'validating', 'cycle_ended',
];
const _JOURNAL_STATE_LABEL = {
    cycle_started: '已开始',
    reflecting:    '反思中',
    forging:       '合成中',
    validating:    '校验中',
    cycle_ended:   '已结束',
};
const _JOURNAL_VERDICT_LABEL = {
    passed:   '✅ 通过',
    rejected: '⛔ 拒绝',
    skipped:  '⏭ 跳过',
    pending:  '… 进行中',
};

function _ensureJournalCycle(cycleId) {
    if (!cycleId) return null;
    let cycle = _journalCycles.get(cycleId);
    if (!cycle) {
        cycle = {
            cycleId,
            state: 'cycle_started',
            verdict: 'pending',
            startedAt: Date.now(),
            endedAt: null,
            trigger: null,
            artifacts: [],
            rejectReason: null,
        };
        _journalCycles.set(cycleId, cycle);
        _journalOrder.unshift(cycleId);
        // Cap rendered cycles to keep DOM light
        while (_journalOrder.length > 20) {
            const stale = _journalOrder.pop();
            _journalCycles.delete(stale);
        }
    }
    return cycle;
}

function handleJournalEvent(wireType, payload) {
    payload = payload || {};
    const cycleId = payload.cycle_id;
    if (!cycleId) return;
    const cycle = _ensureJournalCycle(cycleId);

    switch (wireType) {
        case 'evolution_cycle_started':
            cycle.state = 'cycle_started';
            cycle.trigger = payload.trigger || cycle.trigger;
            break;
        case 'evolution_reflecting':
            cycle.state = 'reflecting';
            break;
        case 'evolution_forging':
            cycle.state = 'forging';
            break;
        case 'evolution_validating':
            cycle.state = 'validating';
            break;
        case 'evolution_artifact_shadow':
            cycle.artifacts.push({ id: payload.artifact_id, kind: payload.kind, status: 'shadow' });
            break;
        case 'evolution_artifact_promoted':
            _markArtifactStatus(cycle, payload.artifact_id, 'promoted');
            break;
        case 'evolution_artifact_retired':
            _markArtifactStatus(cycle, payload.artifact_id, 'retired');
            break;
        case 'evolution_rollback':
            cycle.rejectReason = payload.reason || 'rollback';
            break;
        case 'evolution_rejected':
            cycle.verdict = 'rejected';
            cycle.rejectReason = payload.reason || cycle.rejectReason;
            break;
        case 'evolution_cycle_ended':
            cycle.state = 'cycle_ended';
            cycle.verdict = payload.verdict || cycle.verdict || 'passed';
            cycle.endedAt = Date.now();
            break;
        default:
            return;
    }
    renderJournalPanel();
}

function _markArtifactStatus(cycle, artifactId, status) {
    if (!artifactId) return;
    const existing = cycle.artifacts.find(a => a.id === artifactId);
    if (existing) {
        existing.status = status;
    } else {
        cycle.artifacts.push({ id: artifactId, kind: null, status });
    }
}

function renderJournalPanel() {
    const body = document.getElementById('evo-journal-body');
    if (!body) return;
    if (_journalOrder.length === 0) {
        body.innerHTML = '<div class="empty-state" style="padding:12px;text-align:center">暂无进化周期</div>';
        return;
    }
    const html = _journalOrder.map(cid => {
        const c = _journalCycles.get(cid);
        if (!c) return '';
        const steps = _JOURNAL_STATE_ORDER.map(s => {
            const reached = _JOURNAL_STATE_ORDER.indexOf(s) <= _JOURNAL_STATE_ORDER.indexOf(c.state);
            const isCurrent = s === c.state && c.verdict === 'pending';
            const cls = reached ? (isCurrent ? 'step active' : 'step done') : 'step';
            return `<span class="${cls}">${_JOURNAL_STATE_LABEL[s]}</span>`;
        }).join('<span class="sep">›</span>');

        const verdictLabel = _JOURNAL_VERDICT_LABEL[c.verdict] || c.verdict;
        const verdictClass = `verdict verdict-${c.verdict}`;
        const artifactsHtml = c.artifacts.length
            ? c.artifacts.map(a => `<span class="artifact artifact-${a.status}" title="${a.kind || ''}">${_escapeHtml(a.id || '')} · ${a.status}</span>`).join('')
            : '<span class="artifact-empty">无产物</span>';
        const rejectHtml = c.rejectReason
            ? `<div class="reject-reason">原因：${_escapeHtml(c.rejectReason)}</div>`
            : '';
        const triggerHtml = c.trigger ? `<span class="trigger">触发：${_escapeHtml(c.trigger)}</span>` : '';

        return `
            <div class="journal-cycle">
                <div class="journal-head">
                    <span class="cycle-id" title="${_escapeHtml(c.cycleId)}">${_escapeHtml(c.cycleId.slice(0, 12))}</span>
                    ${triggerHtml}
                    <span class="${verdictClass}">${verdictLabel}</span>
                </div>
                <div class="journal-steps">${steps}</div>
                <div class="journal-artifacts">${artifactsHtml}</div>
                ${rejectHtml}
            </div>
        `;
    }).join('');
    body.innerHTML = html;
}

function _escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
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
    } else if (etype === 'reflection:complete') {
        // Reflection completed — add reflection card to chat and timeline
        addReflectionMessage(payload.reflection || {}, payload.improvement || {});
        addTimelineEvent('reflection', '反思完成', payload.reflection?.summary || '');

    } else if (etype === 'tool:called') {
        // Real-time tool pattern tracking — update the evolution observation panel
        if (payload.count !== undefined) {
            updateToolPatternDisplay(payload.tool || '', payload.count, payload.threshold);
        }
        if (payload.action === 'pattern_threshold_reached') {
            showToast(`⚡ 模式触发：${payload.tool} 使用 ≥${payload.count} 次，即将生成技能！`);
            addTimelineEvent('pattern', `模式触发: ${payload.tool}`, `${payload.count}次调用`);
        }

    } else if (etype === 'pattern:threshold_reached') {
        // Tool pattern threshold reached — real-time evolution trigger
        showToast(`🔥 进化触发：${payload.tool} 模式已达到阈值！`);
        addTimelineEvent('evolution', `模式触发进化`, `${payload.tool} × ${payload.count}次`);

    } else if (etype === 'evolution:trigger') {
        // Evolution engine just started running
        const trigger = payload.trigger || 'manual';
        showToast(`🔄 进化引擎启动（触发：${trigger}）`);
        addTimelineEvent('evolution', '进化引擎启动', `触发: ${trigger}`);
        // Update evolution view if visible
        if (currentView === 'evolution') typeof loadEvolutionStatus === 'function' && loadEvolutionStatus();

    } else if (etype === 'evolution:notify') {
        // Evolution cycle completed with results
        const actions = payload.actions || [];
        const status = payload.status || 'done';
        if (actions.length > 0) {
            for (const action of actions) {
                const typeLabel = action.type === 'gene' ? 'Gene' : action.type === 'skill' ? 'Skill' : action.type;
                showToast(`✨ ${typeLabel} 已生成：${action.name || action.id}`);
                addTimelineEvent(action.type || 'evolution', `${typeLabel} 生成`, action.name || action.id);
            }
            geneCount += actions.filter(a => a.type === 'gene').length;
            skillCount += actions.filter(a => a.type === 'skill').length;
            evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
            persistState();
        } else if (status === 'no_insights') {
            addTimelineEvent('evolution', '进化无洞察', '暂无待学习模式');
        }

    } else if (etype === 'gene:generated') {
        geneCount++;
        evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
        showToast(`🧬 Gene 已生成：${payload.name || payload.gene_id}`);
        addTimelineEvent('gene', 'Gene 生成', payload.name || payload.gene_id);
        persistState();

    } else if (etype === 'skill:generated' || etype === 'skill:executed') {
        // skill:executed also fires when a new skill is hot-reloaded
        const isHotReload = payload.action === 'hot_reloaded';
        if (!isHotReload) {
            skillCount++;
            evolutionCount.textContent = `${geneCount} Genes · ${skillCount} Skills`;
            persistState();
        }
        showToast(`${isHotReload ? '🔄' : '⚡'} Skill ${isHotReload ? '热加载' : '已生成'}：${payload.skill_name || payload.skill_id || payload.name}`);
        addTimelineEvent('skill', `Skill ${isHotReload ? '热加载' : '生成'}`, payload.skill_name || payload.skill_id || payload.name || '');
        if (currentView === 'evolution') typeof loadEvolutionStatus === 'function' && loadEvolutionStatus();

    } else if (etype === 'memory:updated') {
        // Real-time memory update — refresh memory panel if visible
        const preview = payload.preview || '';
        addTimelineEvent('memory', '记忆已保存', preview.substring(0, 60));
        if (currentView === 'evolution' || currentView === 'memory') {
            typeof loadEvolutionStatus === 'function' && loadEvolutionStatus();
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

// ── Plan v2 E6: message-level 👍/👎 feedback ───────────────────────────────
// Buttons render inline with the copy action and POST to the feedback API,
// which upserts into user_feedback and publishes USER_FEEDBACK_RECORDED so
// reflection sees the verdict on the next cycle.
function _attachFeedbackButtons(msgEl, turnId) {
    if (!msgEl || !turnId) return;
    const row = msgEl.closest('.message-row');
    if (!row) return;
    const actions = row.querySelector('.message-actions');
    if (!actions || actions.querySelector('.fb-btn')) return; // idempotent
    msgEl.dataset.turnId = turnId;
    const up = document.createElement('button');
    up.className = 'msg-action-btn fb-btn fb-up';
    up.title = '这个回复有帮助 (👍)';
    up.textContent = '👍';
    up.onclick = () => _submitFeedback(turnId, 'up', up);
    const down = document.createElement('button');
    down.className = 'msg-action-btn fb-btn fb-down';
    down.title = '这个回复不对 (👎)，可附一句备注';
    down.textContent = '👎';
    down.onclick = () => _submitFeedback(turnId, 'down', down);
    actions.appendChild(up);
    actions.appendChild(down);
}

// ── Plan v2 E6 PR-E6-2: Turn annotation sidebar ──────────────────────────
// Shift+R opens a right-side drawer listing every turn the client has seen
// this session, with inline thumb + note editors. Existing feedback is
// pulled from /api/agent/{id}/feedback/recent and merged in.
async function _openAnnotationSidebar() {
    // Build or focus existing drawer
    let drawer = document.getElementById('turn-annotation-drawer');
    if (drawer) { drawer.classList.add('open'); return; }

    drawer = document.createElement('aside');
    drawer.id = 'turn-annotation-drawer';
    drawer.className = 'turn-annotation-drawer open';
    drawer.innerHTML = `
        <div class="tad-header">
            <span class="tad-title">📝 批注本轮会话 (Shift+R)</span>
            <button class="tad-close" title="关闭 (Esc)">✕</button>
        </div>
        <div class="tad-hint">给每轮加一条备注，反思会在下轮读到它们。</div>
        <div class="tad-list" id="tad-list">
            <div class="tad-empty">加载中…</div>
        </div>
    `;
    document.body.appendChild(drawer);
    drawer.querySelector('.tad-close').onclick = _closeAnnotationSidebar;
    document.addEventListener('keydown', _annotationSidebarEscHandler);
    await _renderAnnotationList();
}

function _closeAnnotationSidebar() {
    const drawer = document.getElementById('turn-annotation-drawer');
    if (drawer) drawer.remove();
    document.removeEventListener('keydown', _annotationSidebarEscHandler);
}

function _annotationSidebarEscHandler(e) {
    if (e.key === 'Escape') _closeAnnotationSidebar();
}

async function _renderAnnotationList() {
    const list = document.getElementById('tad-list');
    if (!list) return;
    // Pull persisted feedback so reloaded sessions still see their votes.
    let existing = {};
    try {
        const r = await fetch(`/api/agent/${encodeURIComponent(AGENT_ID)}/feedback/recent?limit=200`);
        if (r.ok) {
            const body = await r.json();
            for (const row of (body.feedback || [])) existing[row.turn_id] = row;
        }
    } catch {}
    // Merge _turnLog entries + any feedback rows that aren't in the log
    // (e.g. restored from a prior session). Newest first.
    const seen = new Set();
    const items = [];
    for (let i = _turnLog.length - 1; i >= 0; i--) {
        const t = _turnLog[i];
        seen.add(t.turn_id);
        items.push({...t, feedback: existing[t.turn_id] || null});
    }
    for (const tid of Object.keys(existing)) {
        if (seen.has(tid)) continue;
        items.push({turn_id: tid, user: '', assistant: '(旧会话)', ts: 0, feedback: existing[tid]});
    }
    if (items.length === 0) {
        list.innerHTML = '<div class="tad-empty">本会话还没有已提交的轮次。先聊两句再回来～</div>';
        return;
    }
    list.innerHTML = items.map((it, idx) => {
        const thumb = it.feedback?.thumb || '';
        const note = it.feedback?.note || '';
        const safeUid = escapeHtml(it.turn_id);
        return `
            <div class="tad-item" data-turn-id="${safeUid}">
                <div class="tad-item-head">
                    <span class="tad-item-num">#${items.length - idx}</span>
                    <span class="tad-thumb-group">
                        <button class="tad-thumb ${thumb==='up'?'active':''}" data-val="up" title="👍">👍</button>
                        <button class="tad-thumb ${thumb==='down'?'active down':''}" data-val="down" title="👎">👎</button>
                    </span>
                </div>
                ${it.user ? `<div class="tad-preview tad-user">🧑 ${escapeHtml(it.user)}${it.user.length >= 80 ? '…' : ''}</div>` : ''}
                ${it.assistant ? `<div class="tad-preview tad-asst">🤖 ${escapeHtml(it.assistant)}${it.assistant.length >= 120 ? '…' : ''}</div>` : ''}
                <textarea class="tad-note" placeholder="批注（可选）…">${escapeHtml(note)}</textarea>
                <div class="tad-item-actions">
                    <button class="tad-save">保存</button>
                </div>
            </div>
        `;
    }).join('');
    // Wire handlers
    list.querySelectorAll('.tad-item').forEach(item => {
        const tid = item.dataset.turnId;
        item.querySelectorAll('.tad-thumb').forEach(btn => {
            btn.onclick = () => {
                item.querySelectorAll('.tad-thumb').forEach(b => b.classList.remove('active', 'down'));
                btn.classList.add('active');
                if (btn.dataset.val === 'down') btn.classList.add('down');
            };
        });
        item.querySelector('.tad-save').onclick = () => _saveAnnotationRow(tid, item);
    });
}

async function _saveAnnotationRow(turnId, itemEl) {
    const activeThumb = itemEl.querySelector('.tad-thumb.active');
    if (!activeThumb) {
        showToast('先选一个 👍 或 👎');
        return;
    }
    const thumb = activeThumb.dataset.val;
    const note = itemEl.querySelector('.tad-note').value.trim();
    try {
        const body = {thumb};
        if (note) body.note = note;
        const r = await fetch(
            `/api/agent/${encodeURIComponent(AGENT_ID)}/turns/${encodeURIComponent(turnId)}/feedback`,
            {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}
        );
        if (!r.ok) {
            const err = await r.json().catch(() => ({error: r.statusText}));
            showToast(`保存失败: ${err.error || r.status}`);
            return;
        }
        showToast('已保存批注');
        // Also sync the inline bubble buttons so they reflect the new verdict.
        const bubble = document.querySelector(`.message[data-turn-id="${turnId}"]`);
        if (bubble) {
            const row = bubble.closest('.message-row');
            row?.querySelectorAll('.fb-btn').forEach(b => b.classList.remove('fb-active'));
            row?.querySelector(thumb === 'up' ? '.fb-up' : '.fb-down')?.classList.add('fb-active');
        }
    } catch (e) {
        console.error('saveAnnotationRow error', e);
        showToast('请求失败');
    }
}

// ── Plan v2 E6 PR-E6-3: SOUL/PROFILE/AGENTS md editor ────────────────────
// Shift+M opens a drawer that lets the user overwrite the three identity
// files — changes hot-reload on the daemon's live agent instance, so the
// next reflection/turn reads the new content without a restart.
const _MD_KINDS = [
    {key: 'soul', label: 'SOUL.md', hint: '身份 / 立场 / 不变的准则'},
    {key: 'profile', label: 'PROFILE.md', hint: '能力 / 风格 / 擅长什么'},
    {key: 'agents', label: 'AGENTS.md', hint: '其他可协作 agent 的摘要'},
];

async function _openMdEditor(initialKind = 'soul') {
    let drawer = document.getElementById('md-editor-drawer');
    if (drawer) { drawer.remove(); }
    drawer = document.createElement('aside');
    drawer.id = 'md-editor-drawer';
    drawer.className = 'md-editor-drawer open';
    const tabsHtml = _MD_KINDS.map(k => `
        <button class="mde-tab ${k.key===initialKind?'active':''}" data-kind="${k.key}">
            ${k.label}
        </button>
    `).join('');
    drawer.innerHTML = `
        <div class="mde-header">
            <span class="mde-title">🧬 编辑 Agent 身份 (Shift+M)</span>
            <button class="mde-close" title="关闭 (Esc)">✕</button>
        </div>
        <div class="mde-tabs">${tabsHtml}</div>
        <div class="mde-hint" id="mde-hint"></div>
        <textarea class="mde-textarea" id="mde-textarea" placeholder="加载中…"></textarea>
        <div class="mde-footer">
            <span class="mde-status" id="mde-status"></span>
            <button class="mde-save" id="mde-save">保存并热加载</button>
        </div>
    `;
    document.body.appendChild(drawer);
    drawer.querySelector('.mde-close').onclick = _closeMdEditor;
    document.addEventListener('keydown', _mdEditorEscHandler);
    drawer.querySelectorAll('.mde-tab').forEach(t => {
        t.onclick = () => {
            drawer.querySelectorAll('.mde-tab').forEach(b => b.classList.remove('active'));
            t.classList.add('active');
            _loadMdKind(t.dataset.kind);
        };
    });
    document.getElementById('mde-save').onclick = _saveMdCurrent;
    await _loadMdKind(initialKind);
}

function _closeMdEditor() {
    const d = document.getElementById('md-editor-drawer');
    if (d) d.remove();
    document.removeEventListener('keydown', _mdEditorEscHandler);
}

function _mdEditorEscHandler(e) {
    if (e.key === 'Escape' && document.activeElement?.tagName !== 'TEXTAREA') {
        _closeMdEditor();
    }
}

async function _loadMdKind(kind) {
    const ta = document.getElementById('mde-textarea');
    const hint = document.getElementById('mde-hint');
    const status = document.getElementById('mde-status');
    const info = _MD_KINDS.find(k => k.key === kind);
    if (hint) hint.textContent = info ? info.hint : '';
    if (status) status.textContent = '';
    ta.dataset.kind = kind;
    ta.value = '';
    try {
        const r = await fetch(`/api/agent/${encodeURIComponent(AGENT_ID)}/md/${kind}`);
        if (!r.ok) {
            ta.value = '';
            status.textContent = `加载失败 (${r.status})`;
            return;
        }
        const body = await r.json();
        ta.value = body.content || '';
        if (!body.exists) status.textContent = '（文件尚未存在，保存后会新建）';
    } catch (e) {
        status.textContent = '加载失败';
    }
}

async function _saveMdCurrent() {
    const ta = document.getElementById('mde-textarea');
    const kind = ta?.dataset.kind;
    if (!kind) return;
    const status = document.getElementById('mde-status');
    status.textContent = '保存中…';
    try {
        const r = await fetch(
            `/api/agent/${encodeURIComponent(AGENT_ID)}/md/${kind}`,
            {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: ta.value}),
            }
        );
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
            status.textContent = `保存失败: ${body.error || r.status}`;
            return;
        }
        status.textContent = `已保存 ${body.bytes || 0} 字节，已热加载到当前 agent`;
        showToast(`已保存 ${kind}.md`);
    } catch (e) {
        status.textContent = '请求失败';
    }
}

async function _submitFeedback(turnId, thumb, btn) {
    let note = null;
    if (thumb === 'down') {
        note = window.prompt('（可选）告诉 Agent 哪里不对，帮助下一轮反思：', '');
        if (note === null) return; // user cancelled
        note = note.trim() || null;
    }
    try {
        const body = { thumb };
        if (note) body.note = note;
        const r = await fetch(
            `/api/agent/${encodeURIComponent(AGENT_ID)}/turns/${encodeURIComponent(turnId)}/feedback`,
            {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            }
        );
        if (!r.ok) {
            const err = await r.json().catch(() => ({error: r.statusText}));
            showToast(`反馈失败: ${err.error || r.status}`);
            return;
        }
        const row = btn.closest('.message-row');
        if (row) {
            // Highlight the chosen thumb; fade the other so the verdict is clear.
            row.querySelectorAll('.fb-btn').forEach(b => b.classList.remove('fb-active'));
            btn.classList.add('fb-active');
        }
        showToast(thumb === 'up' ? '已记录 👍' : '已记录 👎');
    } catch (e) {
        console.error('submitFeedback error', e);
        showToast('反馈请求失败');
    }
}

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

    // ── Fallback send (one-shot socket, used only if the long-lived _ws isn't open) ──
    function _sendSafe(payload) {
        try {
            const s = new WebSocket('ws://127.0.0.1:8766/agent/default');
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
    saveCurrentSession();  // save immediately so refresh doesn't lose the message
    if (!sent) {
        removeTyping();
        setAgentState('IDLE', '');
        showToast('⚠️ 已离线，消息已加入发送队列');
    }
}

sendBtn?.addEventListener('click', sendMessage);
input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

input?.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

document.getElementById('workspace-refresh')?.addEventListener('click', loadWorkspaceFiles);

// (init happens at end of file)

// Global error handler
window.addEventListener('error', (e) => {
    console.error('[ERROR]', e.message, 'at', e.filename, ':', e.lineno);
});

// Rich empty-state HTML for views that otherwise render as "blank with one
// gray line" — same copy as index.html's initial state so the view looks
// identical before first search and after clearing a query.
const MEMORY_EMPTY_HTML = `
    <div class="empty-state-rich">
        <span class="es-icon">🧠</span>
        <div class="es-title">长期记忆检索</div>
        <div class="es-body">
            代理每轮对话后会把关键结论、反思结果、事实性知识写入向量记忆。
            在上方输入关键词就能按语义搜索历史记忆；刚装好时还没有内容，
            聊几轮之后再回来试试。
        </div>
    </div>`;

const TOOL_LOG_EMPTY_HTML = `
    <div class="empty-state-rich">
        <span class="es-icon">🔧</span>
        <div class="es-title">工具调用日志</div>
        <div class="es-body">
            这里会实时记录代理调用每个工具的输入、输出、耗时和成功/失败。
            上方"自动测试"面板可以给工具生成单元测试；
            开一轮对话让代理用几个工具,日志就会出现。
        </div>
    </div>`;

// Memory search (tab panel version with relevance scores)
async function loadMemorySearch() {
    const q = document.getElementById('memory-query')?.value?.trim();
    const resultsEl = document.getElementById('memory-results');
    if (!resultsEl) return;
    if (!q) {
        resultsEl.innerHTML = MEMORY_EMPTY_HTML;
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

// Render the 工具 view from the *current session's* in-memory tool history.
//
// This used to fetch /api/tools/logs (the daemon's cross-session log file)
// and paint it into #tool-log, which meant opening the 工具 tab clobbered
// the per-session log with every tool call ever made on this machine —
// i.e. a brand-new conversation would show web_fetch/bash calls from
// sessions the user had deleted hours ago. Render from toolHistory
// (populated by tool_call / tool_result WebSocket events) instead so
// the view is scoped to the conversation the user is actually looking at.
async function loadToolsLogs() {
    renderToolLog();
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
    // Snapshot the in-memory tool log so switching back later restores the
    // exact tool calls the agent made in *this* session (see Bug E fix).
    const toolSnapshot = toolHistory.slice(0, 50);
    const existing = sessions.find(s => s.id === currentSessionId);
    if (existing) {
        existing.html = html;
        existing.title = getSessionTitle();
        existing.updated = Date.now();
        existing.toolHistory = toolSnapshot;
    } else {
        sessions.unshift({ id: currentSessionId, title: getSessionTitle(), html: html, updated: Date.now(), toolHistory: toolSnapshot });
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
            <span class="session-title">${escapeHtml(s.title)}</span>
            <button class="session-delete" data-del-id="${s.id}" title="删除此会话" aria-label="删除此会话">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
            </button>
        </div>
    `).join('');
    list.querySelectorAll('.session-item').forEach(item => {
        item?.addEventListener('click', (e) => {
            // Ignore clicks on the delete button — it has its own handler.
            if (e.target.closest('.session-delete')) return;
            switchSession(item.dataset.id);
        });
    });
    list.querySelectorAll('.session-delete').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteSession(btn.dataset.delId);
        });
    });
}

function deleteSession(id) {
    const s = sessions.find(x => x.id === id);
    if (!s) return;
    if (!confirm(`确定删除会话「${s.title}」？\n此操作无法撤销。`)) return;
    sessions = sessions.filter(x => x.id !== id);
    // If we deleted the active session, fall back to the newest remaining
    // one (or a fresh empty session if the list is now empty).
    if (id === currentSessionId) {
        if (sessions.length > 0) {
            switchSession(sessions[0].id);
        } else {
            currentSessionId = generateSessionId();
            chat.innerHTML = '';
            showWelcome();
        }
    }
    renderSessionList();
    persistSessions();
}
window.deleteSession = deleteSession;

function switchSession(id) {
    saveCurrentSession();
    const s = sessions.find(x => x.id === id);
    if (!s) return;
    currentSessionId = id;
    chat.innerHTML = s.html || '';
    _rebuildRawTextMap();
    // Each session owns its own tool history so the 工具 view never leaks
    // calls from one conversation into another. Restore (or reset) here.
    toolHistory = Array.isArray(s.toolHistory) ? s.toolHistory.slice() : [];
    renderRecentTools();
    renderToolLog();
    renderSessionList();
    persistSessions();
    if (chat.children.length === 0) showWelcome(); else hideWelcome();
}

function newSession() {
    saveCurrentSession();
    currentSessionId = generateSessionId();
    chat.innerHTML = '';
    // A fresh session must start with a clean tool log — otherwise the
    // previous session's bash/web_fetch calls appear to belong to this one.
    toolHistory = [];
    renderRecentTools();
    renderToolLog();
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
    document.getElementById('viewer-meta').innerHTML = meta.map(m => `<span class="viewer-meta-pill">${escapeHtml(m)}</span>`).join('');
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
    // Shift+R: open turn annotation sidebar (Plan v2 E6 PR-E6-2).
    // Guard against typing in input/textarea so it doesn't hijack letters.
    else if (e.shiftKey && (e.key === 'R' || e.key === 'r')
             && !e.ctrlKey && !e.metaKey && !e.altKey
             && !['INPUT','TEXTAREA'].includes(document.activeElement?.tagName)) {
        e.preventDefault();
        _openAnnotationSidebar();
    }
    // Shift+M: open SOUL/PROFILE/AGENTS md editor (Plan v2 E6 PR-E6-3).
    else if (e.shiftKey && (e.key === 'M' || e.key === 'm')
             && !e.ctrlKey && !e.metaKey && !e.altKey
             && !['INPUT','TEXTAREA'].includes(document.activeElement?.tagName)) {
        e.preventDefault();
        _openMdEditor();
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

document.getElementById('btn-new-chat')?.addEventListener('click', function() {
    console.log('[XMclaw] newSession clicked, currentSessionId:', currentSessionId);
    try { newSession(); }
    catch(e) { console.error('[XMclaw] newSession error:', e); }
});
document.getElementById('toggle-console-panel')?.addEventListener('click', toggleConsolePanel);
document.getElementById('clear-patterns')?.addEventListener('click', clearToolPatterns);

// ===== ARCHITECTURE & FLOWS =====
async function loadArchitectureFlows() {
    const tabs = document.getElementById('arch-flow-tabs');
    const content = document.getElementById('arch-content');
    if (!tabs || !content) return;

    const flows = [
        {
            id: 'startup',
            label: '🚀 启动',
            icon: '⚡',
            desc: 'Daemon 启动流程：配置检查 → 组件初始化 → 服务就绪',
            color: '#f59e0b',
            steps: [
                { node: 'xmclaw start', type: 'entry', desc: 'CLI 命令入口', color: '#6366f1' },
                { node: 'lifecycle.py', type: 'process', desc: '检查 PID / 启动子进程', color: '#3b82f6' },
                { node: 'config.json', type: 'data', desc: '首次运行：运行配置向导', color: '#10b981' },
                { node: 'python -m xmclaw.daemon.server', type: 'process', desc: '启动 Python 进程', color: '#3b82f6' },
                { node: 'lifespan()', type: 'process', desc: 'FastAPI 生命周期', color: '#8b5cf6' },
                { node: 'orchestrator.initialize()', type: 'process', desc: '初始化编排器', color: '#ec4899' },
                { node: 'tools.load_all()', type: 'process', desc: '加载工具注册表', color: '#f97316' },
                { node: 'memory.initialize()', type: 'process', desc: '初始化记忆层', color: '#06b6d4' },
                { node: 'install_event_handlers()', type: 'process', desc: '安装事件总线处理器', color: '#84cc16' },
                { node: 'evo_scheduler.start()', type: 'process', desc: '启动进化调度器', color: '#14b8a6' },
                { node: 'integration_manager.start()', type: 'process', desc: '启动外部集成', color: '#a855f7' },
                { node: 'uvicorn.run()', type: 'terminal', desc: '监听 8766 端口', color: '#ef4444' },
                { node: '前端 WebSocket 连接', type: 'exit', desc: 'ws://127.0.0.1:8766/agent/default', color: '#6366f1' },
            ]
        },
        {
            id: 'conversation',
            label: '💬 对话',
            icon: '💬',
            desc: '用户发送消息 → Agent 思考 → 工具调用 → 流式响应 → 记忆保存',
            color: '#6366f1',
            steps: [
                { node: '用户输入 → 发送', type: 'entry', desc: 'input.value → wsSend()', color: '#6366f1' },
                { node: 'WebSocket.send()', type: 'process', desc: 'JSON: {type:"message",content:"..."}', color: '#3b82f6' },
                { node: 'server.py: agent_websocket()', type: 'process', desc: '接收消息 → orchestrator.run_agent()', color: '#8b5cf6' },
                { node: 'AgentLoop.run()', type: 'process', desc: '构建 Prompt → LLM.stream()', color: '#ec4899' },
                { node: 'LLMRouter', type: 'decision', desc: '路由到 Anthropic/OpenAI/插件', color: '#f59e0b' },
                { node: 'streaming chunks', type: 'process', desc: 'yield chunk → ws.send_text()', color: '#06b6d4' },
                { node: 'tool_call detected', type: 'decision', desc: '是工具调用?', color: '#f97316' },
                { node: 'tools.execute()', type: 'process', desc: '执行 Bash / File / Web 等工具', color: '#f97316' },
                { node: 'tool_result', type: 'process', desc: '结果注入 messages → 继续 LLM', color: '#84cc16' },
                { node: 'EventBus 通知', type: 'process', desc: '发布 tool:called / agent:message 等事件', color: '#14b8a6' },
                { node: 'yield done', type: 'process', desc: '对话结束', color: '#a855f7' },
                { node: 'memory.save_turn()', type: 'process', desc: '保存到 SQLite + JSONL + VectorDB', color: '#06b6d4' },
                { node: '前端渲染消息', type: 'exit', desc: '_wsRenderer() → addMessage()', color: '#6366f1' },
            ]
        },
        {
            id: 'reflection',
            label: '🧠 反思',
            icon: '🧠',
            desc: '对话结束后自动分析：问题识别 → 教训总结 → 自动改进 → Gene/Skill 生成',
            color: '#8b5cf6',
            steps: [
                { node: 'yield done', type: 'entry', desc: '对话轮次结束', color: '#8b5cf6' },
                { node: '_schedule_reflection()', type: 'process', desc: 'asyncio.create_task(_bg()) 后台执行', color: '#6366f1' },
                { node: 'ReflectionEngine.reflect()', type: 'process', desc: '分析 _turn_history 历史', color: '#ec4899' },
                { node: 'LLM 生成反思', type: 'process', desc: 'prompt → 问题/教训/改进建议', color: '#f59e0b' },
                { node: 'EventBus.publish()', type: 'process', desc: 'REFLECTION_COMPLETE 事件', color: '#14b8a6' },
                { node: 'WebSocket 广播', type: 'process', desc: '{type:"event",event:{...}} → 所有客户端', color: '#06b6d4' },
                { node: 'handleBusEvent()', type: 'process', desc: 'etype === reflection:complete', color: '#84cc16' },
                { node: 'addReflectionMessage()', type: 'exit', desc: '渲染反思卡片到对话区', color: '#10b981' },
                { node: 'addTimelineEvent()', type: 'exit', desc: '时间线记录反思完成', color: '#10b981' },
            ]
        },
        {
            id: 'evolution',
            label: '🔄 进化',
            icon: '🔄',
            desc: 'APScheduler 定时触发：观察 → 学习 → 生成 Gene/Skill → VFM 评分 → 热加载',
            color: '#ec4899',
            steps: [
                { node: 'APScheduler interval', type: 'entry', desc: '默认每 30 分钟触发一次', color: '#ec4899' },
                { node: 'EvolutionEngine.run_cycle()', type: 'process', desc: '执行完整进化周期', color: '#6366f1' },
                { node: 'Observe: _get_recent_sessions()', type: 'process', desc: '获取最近 200 个会话', color: '#3b82f6' },
                { node: 'Learn: _extract_insights()', type: 'process', desc: '工具使用频率 / 重复请求 / 问题检测', color: '#8b5cf6' },
                { node: 'Decide: _decide_evolution()', type: 'decision', desc: 'pattern→Skill / problem→Gene', color: '#f59e0b' },
                { node: 'GeneForge.forge()', type: 'process', desc: 'LLM 生成 Gene JSON + 代码', color: '#f97316' },
                { node: 'SkillForge.forge()', type: 'process', desc: 'LLM 生成 Skill JSON + 代码', color: '#f97316' },
                { node: 'VFMScorer.score()', type: 'decision', desc: 'VFM 评分 ≥ 阈值才保留', color: '#14b8a6' },
                { node: 'EvolutionValidator.validate()', type: 'decision', desc: '语法检查 / 测试验证', color: '#14b8a6' },
                { node: 'ToolRegistry._load_generated_skills()', type: 'process', desc: '热加载：新 Skill 立即可用', color: '#06b6d4' },
                { node: 'EventBus.publish()', type: 'process', desc: 'gene:generated / skill:generated', color: '#84cc16' },
                { node: '前端更新计数', type: 'exit', desc: 'geneCount++ / skillCount++', color: '#10b981' },
            ]
        },
        {
            id: 'memory',
            label: '🧩 记忆',
            icon: '🧩',
            desc: '三层记忆架构：会话层(JSONL) + 向量层(ChromaDB) + 结构层(SQLite)',
            color: '#06b6d4',
            steps: [
                { node: 'SQLiteStore', type: 'data', desc: 'agent 配置 / insights / 进化记录', color: '#6366f1' },
                { node: 'SessionManager', type: 'data', desc: 'JSONL: 会话历史 / 工具调用记录', color: '#3b82f6' },
                { node: 'VectorStore', type: 'data', desc: 'ChromaDB: 向量嵌入 / 语义搜索', color: '#8b5cf6' },
                { node: 'load_context()', type: 'process', desc: '对话前：加载历史 + 搜索记忆', color: '#14b8a6' },
                { node: 'save_turn()', type: 'process', desc: '对话后：写入三层存储', color: '#14b8a6' },
                { node: 'vector.add()', type: 'process', desc: 'turn 内容 → 向量嵌入', color: '#06b6d4' },
                { node: 'search()', type: 'process', desc: 'top_k 相关记忆 → Prompt 上下文', color: '#06b6d4' },
            ]
        },
        {
            id: 'eventbus',
            label: '📡 事件总线',
            icon: '📡',
            desc: '全系统异步发布订阅：所有组件通过事件总线解耦通信',
            color: '#14b8a6',
            steps: [
                { node: 'EventBus 单例', type: 'data', desc: '全局事件总线 / 历史 500 条', color: '#6366f1' },
                { node: 'bus.subscribe("*")', type: 'process', desc: '所有 WS 客户端注册 wildcard', color: '#3b82f6' },
                { node: 'bus.publish(Event)', type: 'process', desc: 'agent / tools / evolution 发布事件', color: '#8b5cf6' },
                { node: 'rate_limit', type: 'process', desc: '单类型每秒 ≤200 条防护', color: '#f59e0b' },
                { node: 'handlers 异步执行', type: 'process', desc: '同步/异步 handler 全部 await', color: '#ec4899' },
                { node: 'EventType 枚举', type: 'data', desc: '20+ 种事件类型', color: '#14b8a6' },
                { node: 'WS 广播: _forward_event()', type: 'process', desc: '所有事件 → 所有 WebSocket 客户端', color: '#06b6d4' },
                { node: '前端 handleBusEvent()', type: 'exit', desc: '路由到对应 UI 更新逻辑', color: '#10b981' },
            ]
        },
        {
            id: 'multiagent',
            label: '🤖 多代理',
            icon: '🤖',
            desc: '多 Agent 协作：团队创建 → 并行/串行执行 → 结果合并',
            color: '#f97316',
            steps: [
                { node: 'AgentOrchestrator', type: 'process', desc: '管理所有 Agent 实例', color: '#6366f1' },
                { node: 'create_team()', type: 'process', desc: '创建 Agent 团队 / 共享/独立记忆', color: '#3b82f6' },
                { node: 'run_agent()', type: 'process', desc: '运行单个 Agent → yield chunks', color: '#8b5cf6' },
                { node: 'delegate()', type: 'process', desc: '父 Agent 委派任务给子 Agent', color: '#ec4899' },
                { node: 'run_team()', type: 'process', desc: 'parallel=True → asyncio.gather()', color: '#f97316' },
                { node: 'merge_results()', type: 'process', desc: 'concat / first / vote 策略', color: '#f59e0b' },
                { node: 'EventBus 事件', type: 'process', desc: 'agent:start / task:assigned 等', color: '#14b8a6' },
            ]
        },
        {
            id: 'integrations',
            label: '🔌 集成',
            icon: '🔌',
            desc: '外部平台集成：Telegram / Discord / Slack / GitHub / Notion',
            color: '#a855f7',
            steps: [
                { node: 'IntegrationManager', type: 'process', desc: '管理所有外部集成实例', color: '#6366f1' },
                { node: 'config.json', type: 'data', desc: '各平台 token / channel 等配置', color: '#3b82f6' },
                { node: 'integ.connect()', type: 'process', desc: '建立 WebSocket / Webhook 连接', color: '#8b5cf6' },
                { node: 'on_message()', type: 'process', desc: '接收外部消息 → orchestrator.run_agent()', color: '#ec4899' },
                { node: 'AgentLoop.run()', type: 'process', desc: '处理外部平台消息', color: '#f97316' },
                { node: 'yield chunk', type: 'process', desc: '生成响应流', color: '#f59e0b' },
                { node: 'integ.send()', type: 'process', desc: '响应发回外部平台', color: '#14b8a6' },
            ]
        },
        {
            id: 'tools',
            label: '🔧 工具',
            icon: '🔧',
            desc: '工具注册 → 参数解析 → 执行 → 结果返回 → LLM 继续',
            color: '#f97316',
            steps: [
                { node: 'ToolRegistry', type: 'data', desc: '所有工具的注册表', color: '#6366f1' },
                { node: 'load_all()', type: 'process', desc: '加载内置 + MCP + generated 工具', color: '#3b82f6' },
                { node: '_get_tools_for_llm()', type: 'process', desc: '构建 JSON Schema 工具定义', color: '#8b5cf6' },
                { node: 'LLM tool_call', type: 'decision', desc: '返回 tool_use 块?', color: '#f59e0b' },
                { node: 'tools.execute(name, args)', type: 'process', desc: '路由到具体工具类执行', color: '#f97316' },
                { node: 'Bash / File / Web 等', type: 'process', desc: '具体工具执行', color: '#ec4899' },
                { node: 'result → messages', type: 'process', desc: '结果作为 user 消息注入', color: '#14b8a6' },
                { node: 'LLM 继续生成', type: 'process', desc: '基于工具结果继续响应', color: '#06b6d4' },
            ]
        },
        {
            id: 'cognition',
            label: '🧠 认知推理',
            icon: '🧠',
            desc: '五阶段认知流水线：分析→收集→规划→技能→反思（实时可见）',
            color: '#8b5cf6',
            steps: [
                { node: '用户输入', type: 'entry', desc: '进入 AgentLoop.run()', color: '#6366f1' },
                { node: 'Stage 1: 任务分类', type: 'process', desc: 'TaskClassifier.classify() → LLM 判断类型/复杂度', color: '#ec4899' },
                { node: 'Stage 2: 信息收集', type: 'process', desc: 'InfoGatherer.gather() → 记忆/经验/网络 并行搜索', color: '#f59e0b' },
                { node: 'Stage 3: 任务规划', type: 'decision', desc: 'complexity=high → TaskPlanner 生成步骤', color: '#14b8a6' },
                { node: 'Stage 4: 技能匹配', type: 'process', desc: 'SkillMatcher → 评分 → 高置信度自动执行', color: '#f97316' },
                { node: 'Stage 5: 构建 Prompt', type: 'process', desc: '注入分类/记忆/计划/技能结果 → messages[]', color: '#3b82f6' },
                { node: 'Main Loop: Think-Act', type: 'process', desc: 'LLM stream → tool_call → execute → result', color: '#8b5cf6' },
                { node: 'Stage 5: 反思总结', type: 'process', desc: '同步可见 ReflectionEngine.reflect() → 渲染到前端', color: '#06b6d4' },
                { node: 'yield done → 前端', type: 'exit', desc: '返回给用户', color: '#10b981' },
                { node: 'Background Evolution', type: 'process', desc: '_schedule_evolution_only() → 模式触发进化', color: '#84cc16' },
            ]
        },
    ];

    const nodeColors = {
        entry:  { bg: '#6366f1', border: '#818cf8', text: '#fff' },
        exit:   { bg: '#10b981', border: '#34d399', text: '#fff' },
        data:   { bg: '#0f172a', border: '#334155', text: '#94a3b8' },
        process:{ bg: '#1e1b4b', border: '#4c1d95', text: '#c4b5fd' },
        decision:{ bg: '#78350f', border: '#92400e', text: '#fde68a' },
        terminal:{ bg: '#450a0a', border: '#7f1d1d', text: '#fca5a5' },
    };

    let activeFlow = 'startup';

    function renderFlow(flow) {
        const colors = nodeColors;
        const steps = flow.steps;
        const cols = 3;
        const rows = Math.ceil(steps.length / cols);

        let html = `<div class="arch-flow-header">
            <div class="arch-flow-icon" style="background:${flow.color}22;border-color:${flow.color}44">
                <span style="font-size:28px">${flow.icon}</span>
            </div>
            <div>
                <h3 class="arch-flow-title">${flow.label.replace(/^.+\s/, '')}</h3>
                <p class="arch-flow-desc">${flow.desc}</p>
            </div>
        </div>
        <div class="arch-flow-grid" style="--cols:${cols}">`;

        steps.forEach((step, i) => {
            const c = colors[step.type] || colors.process;
            const isLast = i === steps.length - 1;
            const isFirst = i === 0;
            const col = (i % cols) + 1;
            const row = Math.floor(i / cols) + 1;
            const isRowEnd = (i % cols) === cols - 1 || i === steps.length - 1;

            html += `<div class="arch-node" style="
                --node-bg:${c.bg};--node-border:${c.border};--node-text:${c.text};
                grid-column:${col};grid-row:${row};
            ">
                <div class="arch-node-badge">${isFirst ? '▶' : isLast ? '■' : step.type === 'decision' ? '◇' : '●'}</div>
                <div class="arch-node-name">${escapeHtml(step.node)}</div>
                <div class="arch-node-desc">${escapeHtml(step.desc)}</div>
                ${!isRowEnd && !isLast ? `<div class="arch-arrow">→</div>` : ''}
            </div>`;
        });

        html += '</div>';

        // Detail panel
        html += `<div class="arch-detail">
            <div class="arch-detail-header">📋 详细说明</div>
            <div class="arch-steps-list">`;
        steps.forEach((step, i) => {
            const num = String(i + 1).padStart(2, '0');
            html += `<div class="arch-step-item">
                <div class="arch-step-num">${num}</div>
                <div>
                    <div class="arch-step-node">${escapeHtml(step.node)}</div>
                    <div class="arch-step-desc">${escapeHtml(step.desc)}</div>
                </div>
            </div>`;
        });
        html += '</div></div>';

        content.innerHTML = html;
    }

    // Render tabs
    tabs.innerHTML = flows.map(f =>
        `<button class="arch-tab-btn ${f.id === activeFlow ? 'active' : ''}"
            data-flow="${f.id}" style="--tab-color:${f.color}">${f.icon} ${f.label}</button>`
    ).join('');

    tabs.querySelectorAll('.arch-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            activeFlow = btn.dataset.flow;
            tabs.querySelectorAll('.arch-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const flow = flows.find(f => f.id === activeFlow);
            if (flow) renderFlow(flow);
        });
    });

    // Render initial flow
    const initFlow = flows.find(f => f.id === activeFlow);
    if (initFlow) renderFlow(initFlow);
}

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
    fileUpload?.addEventListener('change', async (e) => {
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
    voiceBtn?.addEventListener('click', async () => {
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
    voiceBtn?.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        voiceMode = voiceMode === 'send' ? 'asr' : 'send';
        const modeText = voiceMode === 'send' ? '直接发送' : '转文字';
        showToast(`语音模式切换为: ${modeText}`);
    });
}

// ===== INIT =====
// Save session before page unloads (refresh/close)
window.addEventListener('beforeunload', () => {
    saveCurrentSession();
    persistSessions();
});

loadSessions();
loadSettings();
loadState();
renderSessionList();
if (!currentSessionId || sessions.length === 0) {
    newSession();
} else {
    // Restore chat window HTML for the current session
    const saved = sessions.find(s => s.id === currentSessionId);
    if (saved && saved.html) {
        chat.innerHTML = saved.html;
        hideWelcome();
    } else {
        // current session ID not found in sessions (stale ID) — fall back to first session
        const fallback = sessions[0];
        if (fallback) {
            currentSessionId = fallback.id;
            localStorage.setItem('xmclaw_current_session', currentSessionId);
            if (fallback.html) {
                chat.innerHTML = fallback.html;
                hideWelcome();
            } else {
                showWelcome();
            }
        } else {
            newSession();
        }
    }
    // Scope toolHistory to the *current* session, not the global state blob.
    // Without this, a page reload leaves whatever session happened to save
    // STATE_KEY last as the tool log for every session — i.e. the exact
    // cross-session leak Bug E is about.
    const activeSession = sessions.find(s => s.id === currentSessionId);
    toolHistory = (activeSession && Array.isArray(activeSession.toolHistory))
        ? activeSession.toolHistory.slice()
        : [];
    renderRecentTools();
    renderToolLog();
    _rebuildRawTextMap();  // restore raw text map for copy/re-edit
}
loadDraft();  // restore saved draft on refresh

// ── Wire up core modules ──────────────────────────────────────────────────────
// ── Router: nav clicks, popstate, URL hash sync ─────────────────────────────────
(function initRouter() {
    const VALID_VIEWS = new Set(['dashboard','workspace','evolution','memory','tools','agents','architecture','settings']);
    const SETTINGS_TABS = new Set(['llm','evolution','memory','tools','gateway','mcp','integrations']);
    let _currentView = 'dashboard';

    function getViewFromHash() {
        const hash = window.location.hash.replace(/^#\/?/, '') || 'dashboard';
        return hash.split('/')[0];
    }
    function navigate(view) {
        history.pushState(null, '', `#/${view}`);
        _setView(view);
    }
    function switchView(view) {
        if (!VALID_VIEWS.has(view)) view = 'dashboard';
        _setView(view);
    }
    function _setView(view) {
        _currentView = view;
        document.querySelectorAll('.nav-item').forEach(n =>
            n.classList.toggle('active', n.dataset.view === view));
        document.querySelectorAll('.view').forEach(v =>
            v.classList.toggle('active', v.id === `view-${view}`));
        const titles = { dashboard:'仪表盘', workspace:'工作区', evolution:'进化', memory:'记忆', tools:'工具日志', agents:'多代理', architecture:'架构', settings:'设置' };
        const tb = document.getElementById('topbar-title');
        if (tb) tb.textContent = titles[view] || view;
        if (view === 'workspace')    typeof loadWorkspaceFiles === 'function' && loadWorkspaceFiles();
        if (view === 'evolution')   typeof loadEvolutionStatus === 'function' && loadEvolutionStatus();
        if (view === 'memory')      typeof loadMemorySearch === 'function' && loadMemorySearch();
        if (view === 'tools')       typeof loadToolsLogs === 'function' && loadToolsLogs();
        if (view === 'agents')      typeof loadAgentsView === 'function' && loadAgentsView();
        if (view === 'architecture') typeof loadArchitectureFlows === 'function' && loadArchitectureFlows();
        history.replaceState(null, '', `#/${view}`);
    }
    function switchSettingsTab(tab) {
        document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
        const tabEl = document.querySelector(`.settings-tab[data-stab="${tab}"]`);
        const panelEl = document.getElementById('stab-' + tab);
        if (tabEl) tabEl.classList.add('active');
        if (panelEl) panelEl.classList.add('active');
        if (tab === 'integrations' && typeof loadIntegrationStatus === 'function') loadIntegrationStatus();
        history.replaceState(null, '', `#/settings/${tab}`);
    }
    // Wire nav-item clicks
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', e => {
            e.preventDefault();
            navigate(item.dataset.view);
        });
    });
    // Browser back/forward
    window.addEventListener('popstate', () => {
        const hash = window.location.hash.replace(/^#\/?/, '') || 'dashboard';
        const parts = hash.split('/');
        if (parts[0] === 'settings' && parts[1] && SETTINGS_TABS.has(parts[1])) {
            switchSettingsTab(parts[1]);
        } else {
            _setView(parts[0] || 'dashboard');
        }
    });
    // Initial route
    const init = getViewFromHash();
    if (init === 'settings' && window.location.hash.includes('/settings/')) {
        const tab = window.location.hash.split('/')[2];
        if (tab && SETTINGS_TABS.has(tab)) switchSettingsTab(tab);
        else _setView('settings');
    } else {
        _setView(init);
    }
    // Expose globally
    window._navigate = navigate;
    window.switchView = switchView;
    window._switchSettingsTab = switchSettingsTab;
    window.initRouter = initRouter;  // keep for compat
})();

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
        btn.title = _chatLayoutWide
            ? '切换布局 — 当前为全宽对话，点击回到分栏（对话+侧栏）'
            : '切换布局 — 当前为分栏（对话+侧栏），点击切换到全宽对话';
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

// ── Five-Stage Pipeline helper ──────────────────────────────────────────────
function _renderStageData(data) {
    if (!data) return '';
    const parts = [];

    if (data.type) {
        const typeMap = { qa:'问答', code:'代码', search:'搜索', plan:'规划',
            creative:'创意', learning:'学习', file_op:'文件操作',
            system:'系统', general:'通用' };
        parts.push(`<span class="sdata-tag type">${typeMap[data.type] || data.type}</span>`);
    }
    if (data.complexity) {
        const color = { low:'#10b981', medium:'#f59e0b', high:'#ef4444' }[data.complexity] || '#9ca3af';
        parts.push(`<span class="sdata-tag" style="background:${color}33;color:${color}">${data.complexity}</span>`);
    }
    if (data.capabilities && data.capabilities.length) {
        parts.push(...data.capabilities.map(c => `<span class="sdata-tag cap">${c}</span>`));
    }
    if (data.memories && data.memories.length) {
        parts.push(`<div class="sdata-section">相关记忆 (${data.memories.length})</div>`);
    }
    if (data.insights && data.insights.length) {
        parts.push(`<div class="sdata-section">经验 (${data.insights.length})</div>`);
    }
    if (data.web_results && data.web_results.length) {
        parts.push(`<div class="sdata-section">网页结果 (${data.web_results.length})</div>`);
    }
    if (data.steps && data.steps.length) {
        const ol = document.createElement('ol');
        ol.className = 'sdata-steps';
        data.steps.slice(0, 8).forEach(s => {
            const li = document.createElement('li');
            li.textContent = `${s.step}. ${s.action}${s.tool ? ` → ${s.tool}` : ''}`;
            ol.appendChild(li);
        });
        return parts.join('') + ol.outerHTML;
    }
    if (data.summary) {
        parts.push(`<div class="sdata-summary">${data.summary}</div>`);
    }
    return parts.join('');
}

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
    } else if (data.type === 'stage') {
        // ── Five-Stage Cognition Pipeline events ───────────────────────────────
        const stage = data.stage || '';
        const label = data.label || stage;
        const desc = data.desc || '';

        // Update agent state strip
        setAgentState(stage.toUpperCase(), desc);

        // Remove typing indicator when entering a new stage
        removeTyping();

        if (stage.endsWith('_done')) {
            // Render a stage completion card
            const stageCard = document.createElement('div');
            stageCard.className = 'stage-card';
            stageCard.innerHTML = `
                <div class="stage-card-header">
                    <span class="stage-icon">${label.split(' ')[0] || '✅'}</span>
                    <span class="stage-label">${label.split(' ').slice(1).join(' ')}</span>
                </div>
                <div class="stage-card-desc">${desc}</div>
                ${data.data ? `<div class="stage-card-data">${_renderStageData(data.data)}</div>` : ''}
            `;
            chat.appendChild(stageCard);
            chat.scrollTop = chat.scrollHeight;

            // Add to timeline
            addTimelineEvent('pipeline', label, desc);

            // Special handling for specific stages
            if (stage === 'reflect_done') {
                addReflectionMessage(data.data || {}, {});
            }
        } else {
            // Active stage: show indicator at top
            const indicator = document.getElementById('stage-indicator');
            if (indicator) {
                indicator.textContent = label;
                indicator.style.display = 'flex';
            }
        }
    } else if (data.type === 'turn_committed') {
        // Plan v2 E6: tag the active assistant bubble with its turn_id so
        // the user can attach 👍/👎 feedback. Arrives just before 'done'.
        if (data.turn_id && currentMessageEl) {
            _attachFeedbackButtons(currentMessageEl, data.turn_id);
            // Log this turn so the annotation sidebar (Shift+R) can list it.
            const row = currentMessageEl.closest('.message-row');
            const prevUser = row ? row.previousElementSibling : null;
            const userText = prevUser && prevUser.classList.contains('user')
                ? (prevUser.querySelector('.message')?.textContent || '').slice(0, 80)
                : '';
            const asstText = (_rawTextMap.get(currentMessageEl) || '').slice(0, 120);
            _turnLog.push({
                turn_id: data.turn_id,
                user: userText,
                assistant: asstText,
                ts: Date.now(),
            });
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
    } else if (typeof data.type === 'string' && data.type.startsWith('evolution_')) {
        // Phase E0: journal state-machine + legacy evolution wire events
        handleJournalEvent(data.type, data.payload || {});
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
const WS_URL = 'ws://127.0.0.1:8766/agent/default';
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

// ── WebSocket: register renderer and connect ──────────────────────────────────
// (Moved here from the INIT section above so it runs AFTER window.ws* are set)
window.wsSetGlobalHandler(_wsRenderer);
window.wsOnConnect(_wsOnConnect);
window.wsOnDisconnect(_wsOnDisconnect);
window.wsOnError(_wsOnError);
window.wsConnect();

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
            tab?.addEventListener('click', () => {
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
