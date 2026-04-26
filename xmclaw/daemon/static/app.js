// XMclaw — app entry
//
// Phase 1 wires:
//   * pairing-token fetch (lib/auth.js)
//   * WS client at /agent/v2/{sid} (lib/ws.js)
//   * chat reducer that maps BehavioralEvent envelopes into chat slice
//     mutations (lib/chat_reducer.js)
//   * real Chat page replacing the Phase 0 placeholder for /chat
//
// Other sidebar pages (Agents / Skills / Memory / …) still render a
// placeholder — those are Phase 2-4 work. By keeping the layout the same,
// the user can already navigate and see the new design system everywhere
// while only Chat is live.

const { h, render } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import {
  app as store,
  persistActiveSid,
  persistSidList,
  newSid,
} from "./store.js";
import { installRouter } from "./router.js";
import { fetchPairingToken } from "./lib/auth.js";
import { createWsClient } from "./lib/ws.js";
import {
  applyEvent,
  applySessionLifecycle,
  appendOptimisticUser,
} from "./lib/chat_reducer.js";

import { Button } from "./components/atoms/button.js";
import { Badge } from "./components/atoms/badge.js";
import { Icon } from "./components/atoms/icon.js";
import { Avatar } from "./components/atoms/avatar.js";
import { Spinner } from "./components/atoms/spinner.js";
import { ToastViewport, toast } from "./lib/toast.js";
import { ThemeToggle } from "./components/atoms/theme-toggle.js";
import { ChatPage } from "./pages/Chat.js";
import { SettingsPage } from "./pages/Settings.js";
import { DoctorPage } from "./pages/Doctor.js";
import { ToolsPage } from "./pages/Tools.js";
import { AgentsPage } from "./pages/Agents.js";
import { MemoryPage } from "./pages/Memory.js";
import { SecurityPage } from "./pages/Security.js";
import { InsightsPage } from "./pages/Insights.js";
import { SkillsPage } from "./pages/Skills.js";
import { EvolutionPage } from "./pages/Evolution.js";
import { BackupPage } from "./pages/Backup.js";
import { WorkspacePage } from "./pages/Workspace.js";

// ── WS handle (singleton) ─────────────────────────────────────────────

let wsHandle = null;

function disposeWs() {
  if (wsHandle) {
    try {
      wsHandle.close();
    } catch (_) {
      /* ignore */
    }
    wsHandle = null;
  }
}

function connectFor(sid, token) {
  disposeWs();
  wsHandle = createWsClient({
    sessionId: sid,
    token,
    onEvent: (envelope) => {
      store.setState((s) => ({
        chat: applyEvent(s.chat, envelope),
        session: applySessionLifecycle(s.session, envelope),
      }));
    },
    onStatus: ({ status, error, attempt }) => {
      store.setState({
        connection: { status, lastError: error, reconnectAttempt: attempt },
      });
    },
  });
}

// ── Boot sequence ─────────────────────────────────────────────────────

async function boot() {
  // 1. Fetch pairing token (best-effort; null token is valid when auth
  //    is disabled).
  const auth = await fetchPairingToken();
  store.setState({
    auth: { token: auth.token, fetched: auth.fetched },
  });

  // 2. Pick the active sid: localStorage > newly minted.
  let sid = store.getState().session.activeSid;
  if (!sid) {
    sid = newSid();
    persistActiveSid(sid);
  }
  const sidsCurrent = store.getState().session.sids;
  const sids = sidsCurrent.includes(sid) ? sidsCurrent : sidsCurrent.concat([sid]);
  persistSidList(sids);
  store.setState({
    session: { ...store.getState().session, activeSid: sid, sids },
  });

  // 3. Connect.
  connectFor(sid, auth.token);
}

// ── Action helpers (bound into the page tree) ─────────────────────────

function sendComposer() {
  const s = store.getState();
  const text = (s.chat.composerDraft || "").trim();
  if (!text) return;
  if (!wsHandle) return;
  if (s.connection.status !== "connected") return;

  // Optimistic local echo. The daemon will mirror it back as USER_MESSAGE,
  // and the reducer will dedupe by id.
  const { id, chat: nextChat } = appendOptimisticUser(s.chat, text, {
    ultrathink: s.chat.ultrathink,
  });
  store.setState({ chat: { ...nextChat, composerDraft: "" } });

  wsHandle.send({
    type: "user",
    content: text,
    ultrathink: s.chat.ultrathink || undefined,
    correlation_id: id,
    plan_mode: s.chat.planMode || undefined,
    llm_profile_id: s.chat.llmProfileId || undefined,
  });
}

function setLlmProfile(profileId) {
  store.setState((s) => ({
    chat: { ...s.chat, llmProfileId: profileId || null },
  }));
}

function changeDraft(value) {
  store.setState((s) => ({ chat: { ...s.chat, composerDraft: value } }));
}

function togglePlan() {
  store.setState((s) => ({ chat: { ...s.chat, planMode: !s.chat.planMode } }));
}

function toggleUltrathink() {
  store.setState((s) => ({ chat: { ...s.chat, ultrathink: !s.chat.ultrathink } }));
}

function startNewSession() {
  const sid = newSid();
  const s = store.getState();
  const sids = [sid].concat(s.session.sids.filter((x) => x !== sid));
  persistActiveSid(sid);
  persistSidList(sids);
  store.setState({
    chat: {
      ...s.chat,
      messages: [],
      pendingAssistantId: null,
      composerDraft: "",
    },
    session: { ...s.session, activeSid: sid, sids },
  });
  connectFor(sid, s.auth.token);
}

// ── Routes ────────────────────────────────────────────────────────────

function Placeholder({ title, subtitle }) {
  return html`
    <section class="xmc-placeholder" aria-labelledby="placeholder-title">
      <h2 id="placeholder-title">${title}</h2>
      <p class="xmc-placeholder__subtitle">${subtitle}</p>
      <p class="xmc-placeholder__hint">
        即将上线 — 见 <code>docs/FRONTEND_DESIGN.md §4</code>。
      </p>
    </section>
  `;
}

const routes = {
  "/chat": (state) => html`
    <${ChatPage}
      chat=${state.chat}
      session=${state.session}
      connection=${state.connection}
      token=${state.auth.token}
      onSend=${sendComposer}
      onChangeDraft=${changeDraft}
      onTogglePlan=${togglePlan}
      onToggleUltrathink=${toggleUltrathink}
      onNewSession=${startNewSession}
      onChangeModel=${setLlmProfile}
    />
  `,
  "/workspace": (state) => html`<${WorkspacePage} token=${state.auth.token} />`,
  "/agents": (state) => html`<${AgentsPage} token=${state.auth.token} />`,
  "/skills": (state) => html`<${SkillsPage} token=${state.auth.token} />`,
  "/evolution": (state) => html`<${EvolutionPage} token=${state.auth.token} />`,
  "/memory": (state) => html`<${MemoryPage} token=${state.auth.token} />`,
  "/tools": (state) => html`<${ToolsPage} token=${state.auth.token} />`,
  "/security": (state) => html`<${SecurityPage} token=${state.auth.token} />`,
  "/backup": (state) => html`<${BackupPage} token=${state.auth.token} />`,
  "/doctor": (state) => html`<${DoctorPage} token=${state.auth.token} />`,
  "/insights": (state) => html`<${InsightsPage} token=${state.auth.token} />`,
  "/settings": (state) => html`<${SettingsPage} token=${state.auth.token} />`,
  "*": () => html`<${Placeholder} title="未找到" subtitle="未匹配的路由" />`,
};

// ── Shell ─────────────────────────────────────────────────────────────

// Sidebar follows docs/PRODUCT_REDESIGN.md §8 — collapsed from 12 to 9
// items, ordered by usage frequency. ModelProfiles folds into Settings +
// (future) top-bar picker. Backup/Doctor/Insights collapse into one
// "诊断" page in Phase 5; for now we still link the three pages but only
// surface 诊断 in the primary nav so the user has one entry point.
const SIDEBAR_ITEMS = [
  { path: "/chat", label: "对话", icon: "message" },
  { path: "/agents", label: "智能体", icon: "users" },
  { path: "/skills", label: "技能", icon: "book" },
  { path: "/evolution", label: "进化", icon: "sparkle", accent: true },
  { path: "/tools", label: "工具", icon: "wrench" },
  { path: "/memory", label: "记忆", icon: "layers" },
  { path: "/workspace", label: "工作区", icon: "folder" },
  { path: "/security", label: "安全", icon: "shield" },
  { path: "/doctor", label: "诊断", icon: "stethoscope" },
  { path: "/settings", label: "设置", icon: "cog" },
];

function Sidebar({ activePath }) {
  return html`
    <nav class="xmc-sidebar" aria-label="Primary">
      <div class="xmc-sidebar__brand">
        <${Avatar} initials="XM" />
        <strong>XMclaw</strong>
      </div>
      <ul class="xmc-sidebar__list">
        ${SIDEBAR_ITEMS.map(
          (item) => html`
            <li>
              <a
                href=${item.path}
                class=${"xmc-sidebar__item" +
                (item.path === activePath ? " is-active" : "") +
                (item.accent ? " is-accent" : "")}
                aria-current=${item.path === activePath ? "page" : null}
              >
                <${Icon} name=${item.icon} />
                <span>${item.label}</span>
              </a>
            </li>
          `
        )}
      </ul>
    </nav>
  `;
}

function TopBar({ bootstrapSource, sessionId, onToggleSidebar }) {
  return html`
    <header class="xmc-topbar" role="banner">
      <button
        type="button"
        class="xmc-topbar__sidebar-toggle"
        aria-label="折叠 / 展开侧栏"
        title="折叠 / 展开侧栏 (Cmd-B)"
        onClick=${onToggleSidebar}
      >☰</button>
      <div class="xmc-topbar__title">XMclaw</div>
      <div class="xmc-topbar__meta">
        ${sessionId
          ? html`<${Badge} tone="muted">sid: <code>${sessionId}</code></${Badge}>`
          : null}
        <${Badge} tone="muted">bootstrap: ${bootstrapSource}</${Badge}>
        <${Button} variant="ghost" size="sm" onClick=${startNewSession}>
          新会话
        </${Button}>
        <${ThemeToggle} />
      </div>
    </header>
  `;
}

function StatusBar({ connection, session }) {
  const tone =
    connection.status === "connected"
      ? "success"
      : connection.status === "auth_failed"
      ? "error"
      : connection.status === "reconnecting"
      ? "warn"
      : "muted";
  const detail =
    connection.status === "reconnecting"
      ? `重连中 (#${connection.reconnectAttempt || 1})`
      : connection.status === "auth_failed"
      ? "配对令牌被拒绝 — 请刷新页面"
      : connection.status === "connecting"
      ? "正在连接 daemon…"
      : connection.status === "connected"
      ? `lifecycle: ${session.lifecycle}`
      : connection.lastError || "未连接";
  return html`
    <footer class="xmc-statusbar" role="contentinfo">
      <${Badge} tone=${tone}>${connection.status}</${Badge}>
      <span class="xmc-statusbar__hint">${detail}</span>
      ${connection.status === "reconnecting"
        ? html`<${Spinner} size="sm" label="reconnecting" />`
        : null}
    </footer>
  `;
}

function _readSidebarPref() {
  try {
    return localStorage.getItem("xmc_sidebar_collapsed") === "true";
  } catch (_) { return false; }
}

function _toggleSidebarPref() {
  const next = !_readSidebarPref();
  try { localStorage.setItem("xmc_sidebar_collapsed", String(next)); }
  catch (_) { /* ignore */ }
  document.documentElement.dataset.sidebar = next ? "collapsed" : "expanded";
  return next;
}

// Initialize the dataset on first load so CSS sees the right state.
document.documentElement.dataset.sidebar = _readSidebarPref()
  ? "collapsed" : "expanded";

function App({ state }) {
  const route = routes[state.route.path] || routes["*"];
  const onToggleSidebar = () => {
    _toggleSidebarPref();
    // Force a re-render so any sidebar-aware children reflow.
    store.setState({});
  };
  return html`
    <div class="xmc-shell">
      <${TopBar}
        bootstrapSource=${state.bootstrap.source}
        sessionId=${state.session.activeSid}
        onToggleSidebar=${onToggleSidebar}
      />
      <div class="xmc-shell__body">
        <${Sidebar} activePath=${state.route.path} />
        <main class="xmc-main" role="main">${route(state)}</main>
      </div>
      <${StatusBar} connection=${state.connection} session=${state.session} />
      <${ToastViewport} />
    </div>
  `;
}

// ── Mount ─────────────────────────────────────────────────────────────

const root = document.getElementById("root");
root.removeAttribute("aria-busy");

function renderApp() {
  render(html`<${App} state=${store.getState()} />`, root);
}

installRouter(store, routes);
store.subscribe(renderApp);
renderApp();

// Kick off WS / token boot AFTER the first paint so the user sees the
// shell immediately. boot() is async; failures inside are reported via
// the connection slice (status:"disconnected" + lastError).
boot().catch((err) => {
  console.error("[xmc] boot failed", err);
  store.setState({
    connection: {
      status: "disconnected",
      lastError: String(err),
      reconnectAttempt: 0,
    },
  });
  toast.error("连接 daemon 失败：" + String(err.message || err), { ttl: 6000 });
});
