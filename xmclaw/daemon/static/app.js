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

// Atom imports kept — pages still consume them (Badge / Spinner /
// Button surface inside Chat / Settings / Doctor etc.). Shell-level
// renderer comes from HermesAppShell below.
import { ToastViewport, toast } from "./lib/toast.js";
// Hermes 1:1 port — IS the shell. No legacy fallback.
import { AppShell as HermesAppShell } from "./components/organisms/AppShell.js";
// Side-effect: applies the persisted Hermes theme (or LENS_0 default)
// to :root before the first paint. Mirrors Hermes ThemeProvider.
import "./lib/hermes-themes.js";
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
import { SessionsPage } from "./pages/Sessions.js";
import { CronPage } from "./pages/Cron.js";
import { ConfigPage } from "./pages/Config.js";

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
  "/sessions": (state) => html`<${SessionsPage} token=${state.auth.token} />`,
  "/cron": (state) => html`<${CronPage} token=${state.auth.token} />`,
  "/config": (state) => html`<${ConfigPage} token=${state.auth.token} />`,
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
//
// Phase B 1:1 port of Hermes web/src/App.tsx. The Hermes shell IS the
// shell — no toggles, no legacy fallback. Pages render into the main
// pane. Theme + language switch lives inside the sidebar footer.

function App({ state }) {
  const route = routes[state.route.path] || routes["*"];
  return html`
    <${HermesAppShell} activePath=${state.route.path}>
      ${route(state)}
      <${ToastViewport} />
    </${HermesAppShell}>
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
