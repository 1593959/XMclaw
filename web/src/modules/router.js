/** @module router - Hash-based URL routing */
const VALID_VIEWS = new Set([
    'dashboard', 'workspace', 'evolution', 'memory', 'tools', 'agents', 'settings',
]);
const SETTINGS_TABS = new Set(['llm', 'evolution', 'memory', 'tools', 'gateway', 'mcp', 'integrations']);

/** Current active view name. */
let _currentView = 'dashboard';

/** Callback registered by main: (viewName) => void */
let _onViewChange = null;

/** Register the view-change callback. */
export function onViewChange(fn) { _onViewChange = fn; }

/** Read the primary view from URL hash. */
export function getViewFromHash() {
    const hash = window.location.hash.replace(/^#\/?/, '') || 'dashboard';
    return hash.split('/')[0];
}

/** Navigate to a view, updating URL and triggering the callback. */
export function navigate(view, replace = false) {
    const url = `#/${view}`;
    if (replace) history.replaceState(null, '', url);
    else history.pushState(null, '', url);
    _setView(view, { fromRouter: false });
}

/** Switch to a view programmatically (no URL change). */
export function switchView(view) {
    if (!VALID_VIEWS.has(view)) view = 'dashboard';
    _setView(view, { fromRouter: false });
}

/** Internal: actually switch the view. */
function _setView(view, opts = {}) {
    _currentView = view;

    // Highlight nav
    document.querySelectorAll('.nav-item').forEach(n =>
        n.classList.toggle('active', n.dataset.view === view));

    // Show/hide view panels
    document.querySelectorAll('.view').forEach(v =>
        v.classList.toggle('active', v.id === `view-${view}`));

    // Update topbar title
    const titles = {
        dashboard: '仪表盘', workspace: '工作区', evolution: '进化',
        memory: '记忆', tools: '工具日志', agents: '多代理', settings: '设置',
    };
    const tb = document.getElementById('topbar-title');
    if (tb) tb.textContent = titles[view] || view;

    // Trigger lazy-loaders
    if (view === 'workspace')     { if (typeof loadWorkspaceFiles === 'function') loadWorkspaceFiles(); }
    if (view === 'evolution')     { if (typeof loadEvolutionStatus === 'function') loadEvolutionStatus(); }
    if (view === 'memory')        { if (typeof loadMemorySearch === 'function') loadMemorySearch(); }
    if (view === 'tools')         { if (typeof loadToolsLogs === 'function') loadToolsLogs(); }
    if (view === 'agents')        { if (typeof loadAgentsView === 'function') loadAgentsView(); }

    // Update URL only if not triggered by browser back/forward
    if (!opts.fromRouter) {
        history.replaceState(null, '', `#/${view}`);
    }

    if (typeof _onViewChange === 'function') _onViewChange(view);
}

export function getCurrentView() { return _currentView; }

// ── Settings sub-tab routing ──────────────────────────────────────────────────
/** Switch to a settings sub-tab, syncing the URL hash. */
export function switchSettingsTab(tab, opts = {}) {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
    const tabEl = document.querySelector(`.settings-tab[data-stab="${tab}"]`);
    const panelEl = document.getElementById('stab-' + tab);
    if (tabEl) tabEl.classList.add('active');
    if (panelEl) panelEl.classList.add('active');
    if (tab === 'integrations' && typeof loadIntegrationStatus === 'function') loadIntegrationStatus();
    if (!opts.fromRouter) history.replaceState(null, '', `#/settings/${tab}`);
}

// ── Bootstrap ──────────────────────────────────────────────────────────────────
export function initRouter() {
    // Wire nav-item clicks
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            navigate(item.dataset.view);
        });
    });

    // Browser back/forward
    window.addEventListener('popstate', () => {
        const hash = window.location.hash.replace(/^#\/?/, '') || 'dashboard';
        const parts = hash.split('/');
        if (parts[0] === 'settings' && parts[1] && SETTINGS_TABS.has(parts[1])) {
            switchSettingsTab(parts[1], { fromRouter: true });
        } else {
            _setView(parts[0] || 'dashboard', { fromRouter: true });
        }
    });

    // Initial route
    const init = getViewFromHash();
    if (init === 'settings' && window.location.hash.includes('/settings/')) {
        const tab = window.location.hash.split('/')[2];
        if (tab && SETTINGS_TABS.has(tab)) switchSettingsTab(tab, { fromRouter: true });
        else _setView('settings', { fromRouter: true });
    } else {
        _setView(init, { fromRouter: true });
    }

    // Expose globally for backward compat
    window._navigate = navigate;
    window.switchView = switchView;
    window._switchSettingsTab = switchSettingsTab;
    window.initRouter = initRouter;
}
