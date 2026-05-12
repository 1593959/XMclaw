// XMclaw — app entry. Wires pairing, WS client, chat reducer, page routes.

const { h, render, Component } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

// B-223: top-level error boundary. Pre-B-223 a throw anywhere in the
// component tree (reducer-derived bad state, missing field on a route
// component, etc) bubbled past every layer and Preact unmounted the
// whole DOM → user saw a fully-black tab. The B-220 BubbleBoundary
// only protected individual MessageBubble rows; errors from MessageList
// / a route page / App itself reached the root unguarded.
//
// This boundary catches anything below <App>. On error we render a
// full-page panel with the stack + recovery actions instead of going
// black. Reload-from-here keeps localStorage (active session, prefs)
// so the user doesn't lose context.
class AppErrorBoundary extends Component {
  constructor() {
    super();
    this.state = { err: null, info: null };
  }
  componentDidCatch(err, info) {
    // eslint-disable-next-line no-console
    console.error("[xmc] App-level crash:", err, info);
    this.setState({ err, info });
  }
  render() {
    if (this.state.err) {
      const e = this.state.err;
      const msg = String((e && e.message) || e);
      const stack = String((e && e.stack) || "");
      return html`
        <div style="min-height:100vh;padding:2rem;font-family:var(--xmc-font-mono);background:#0d1212;color:#e0e0e0">
          <h1 style="color:#c66;margin:0 0 .5rem">XMclaw UI 渲染崩溃</h1>
          <p style="opacity:.85">页面树抛出未捕获错误。这通常是某个组件 bug — 不是后端问题。</p>
          <pre style="background:#1a1f1f;padding:1rem;border-radius:6px;overflow:auto;font-size:.78rem;line-height:1.4;max-height:50vh;color:#fbb">${msg}\n\n${stack.slice(0, 4000)}</pre>
          <div style="display:flex;gap:.5rem;margin-top:1rem">
            <button onClick=${() => window.location.reload()} style="padding:.5rem 1rem;background:#193;color:#fff;border:none;border-radius:4px;cursor:pointer">重新加载</button>
            <button onClick=${() => { try { localStorage.clear(); } catch (_) {} window.location.reload(); }} style="padding:.5rem 1rem;background:#933;color:#fff;border:none;border-radius:4px;cursor:pointer">清 localStorage 后加载</button>
          </div>
          <p style="margin-top:1.5rem;opacity:.6;font-size:.78rem">把上面的错误堆栈截给开发者,能精确定位 bug。</p>
        </div>
      `;
    }
    return this.props.children;
  }
}

import {
  app as store,
  persistActiveSid,
  persistSidList,
  persistActiveAgentId,
  newSid,
} from "./store.js";
import {
  fetchAgentsForPicker,
  switchAgentAction,
} from "./lib/agent_picker.js";
import { installRouter } from "./router.js";
import { fetchPairingToken } from "./lib/auth.js";
import { createWsClient } from "./lib/ws.js";
import {
  applyEvent,
  applySessionLifecycle,
  appendOptimisticUser,
  appendThinkingAssistant,
} from "./lib/chat_reducer.js";

// Atom imports kept — pages still consume them (Badge / Spinner /
// Button surface inside Chat / Settings / Doctor etc.). Shell-level
// renderer comes from HermesAppShell below.
import { ToastViewport, toast } from "./lib/toast.js";
import { DialogViewport } from "./lib/dialog.js";
// B-105: prompt history helper lives in Composer module.
import { appendPromptHistory } from "./components/molecules/Composer.js";
// Hermes 1:1 port — IS the shell. No legacy fallback.
import { AppShell as HermesAppShell } from "./components/organisms/AppShell.js";
// Side-effect: applies the persisted Hermes theme (or LENS_0 default)
// to :root before the first paint. Mirrors Hermes ThemeProvider.
import "./lib/hermes-themes.js";
import { ChatPage } from "./pages/Chat.js";
import { SettingsPage } from "./pages/Settings.js";
// Phase F (2026-05-12): Doctor / Backup / Config 合并进 Settings 后死代码删除。
// 旧路由 /doctor /backup /config 在 ``routes`` 表里仍 redirect 到 /settings —
// 不破坏书签 / 外部链接。
import { ToolsPage } from "./pages/Tools.js";
import { AgentsPage } from "./pages/Agents.js";
import { ChannelsPage } from "./pages/Channels.js";
import { MemoryPage } from "./pages/Memory.js";
import { SecurityPage } from "./pages/Security.js";
import { SkillsPage } from "./pages/Skills.js";
import { MarketplacePage } from "./pages/Marketplace.js";  // B-390 (Sprint 2)
import { EvolutionPage } from "./pages/Evolution.js";
import { CognitionPage } from "./pages/Cognition.js";
import { WorkspacePage } from "./pages/Workspace.js";
import { SessionsPage } from "./pages/Sessions.js";
import { CronPage } from "./pages/Cron.js";
import { LogsPage } from "./pages/Logs.js";
import { AnalyticsPage } from "./pages/Analytics.js";
import { DocsPage } from "./pages/Docs.js";
import { TracePage } from "./pages/Trace.js";
import { FilesPage } from "./pages/Files.js";
import { DashboardPage } from "./pages/Dashboard.js";

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

// B-60: rehydrate the chat panel from the daemon's persisted session
// store on connect. Without this, a page reload showed an empty chat
// even though the daemon retained the full conversation — every new
// turn landed with full server-side context but the user only saw
// turn N+1 going forward, producing a half-conversation feeling.
async function hydrateChatHistory(sid, token) {
  if (!sid) return;
  try {
    const url = `/api/v2/sessions/${encodeURIComponent(sid)}` +
      (token ? `?token=${encodeURIComponent(token)}` : "");
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json().catch(() => null);
    const msgs = (data && data.messages) || [];
    if (!msgs.length) return;
    // Map daemon's persisted shape → reducer's chat-message shape.
    const hydrated = [];
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const role = m.role;
      // Skip tool messages — they're rendered as ToolCard children
      // of their owning assistant bubble, which the reducer rebuilds
      // on the fly when assistant carries tool_calls.
      if (role === "tool") continue;
      hydrated.push({
        id: `restore_${i}`,
        role,
        content: m.content || "",
        status: "complete",
        ts: 0,
        toolCalls: (m.tool_calls || []).map((tc) => ({
          id: tc.id,
          name: tc.name,
          args: tc.args || {},
          status: "complete",
        })),
      });
    }
    if (hydrated.length === 0) return;
    store.setState((s) => {
      // Don't clobber if the user already typed something during the
      // restore window — concat with whatever's there.
      const cur = s.chat.messages || [];
      // De-dup: if cur already starts with our hydrated head, skip.
      if (cur.length >= hydrated.length) return s;
      return { ...s, chat: { ...s.chat, messages: hydrated.concat(cur) } };
    });
  } catch (_) {
    /* offline / not-found / stale token — fail silent */
  }
}

// B-99: rehydrate any in-flight ask_user_question. The daemon's tool
// future is still ``await``-ing on the server; we just need to put
// the QuestionCard back in the transcript so the user can answer.
async function rehydratePendingQuestions(token) {
  try {
    const url = "/api/v2/pending_questions" + (token ? `?token=${encodeURIComponent(token)}` : "");
    const r = await fetch(url);
    if (!r.ok) return;
    const data = await r.json();
    const items = Array.isArray(data && data.items) ? data.items : [];
    if (!items.length) return;
    store.setState((s) => {
      // Skip questions we already have a card for (multi-tab safety).
      const existingIds = new Set(
        s.chat.messages
          .filter((m) => m.kind === "question")
          .map((m) => m.question && m.question.id)
          .filter(Boolean),
      );
      const fresh = items
        .filter((q) => !existingIds.has(q.question_id))
        .map((q) => ({
          id: "q_" + q.question_id,
          role: "system",
          kind: "question",
          content: "",
          status: "pending",
          ts: Date.now() / 1000,
          question: {
            id: q.question_id,
            question: q.question || "",
            options: Array.isArray(q.options) ? q.options : [],
            multi_select: !!q.multi_select,
            allow_other: q.allow_other !== false,
            tool_call_id: q.tool_call_id || null,
          },
        }));
      if (!fresh.length) return s;
      return { ...s, chat: { ...s.chat, messages: s.chat.messages.concat(fresh) } };
    });
  } catch (_) {
    /* fail silent — picker is not critical */
  }
}

function connectFor(sid, token, agentId) {
  disposeWs();
  // Fire-and-forget rehydrate; WS connect proceeds in parallel.
  hydrateChatHistory(sid, token);
  rehydratePendingQuestions(token);
  wsHandle = createWsClient({
    sessionId: sid,
    token,
    agentId,  // B-133: route to sub-agent when switched
    onEvent: (envelope) => {
      store.setState((s) => ({
        chat: applyEvent(s.chat, envelope),
        session: applySessionLifecycle(s.session, envelope),
      }));
    },
    onStatus: ({ status, error, attempt }) => {
      const pending = wsHandle?.getPendingCount?.() || 0;
      store.setState({
        connection: { status, lastError: error, reconnectAttempt: attempt, pendingFrames: pending },
      });
      // On a successful reconnect that drained queued frames, tell
      // the user their earlier messages went out — proves the
      // out-of-band recovery worked rather than failing silently.
      if (status === "connected" && wsHandle?.consumeLastFlushCount) {
        const flushed = wsHandle.consumeLastFlushCount();
        if (flushed > 0) {
          toast.success(`已重连 — ${flushed} 条排队消息已发送`);
        }
      }
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

  // 3. Connect with the persisted active agent (defaults to 'main').
  connectFor(sid, auth.token, store.getState().session.activeAgentId || "main");
  fetchAgentsForPicker(store, auth.token);  // B-133: load picker list

  // Iteration 2: start global event-banner polling.
  if (auth.token) startEventBanner(auth.token);
}

const switchAgent = (agentId) =>
  switchAgentAction(store, agentId, persistActiveAgentId, connectFor);

// ── Action helpers (bound into the page tree) ─────────────────────────
//
// B-321: composer-side helpers (send / cancel / answer / draft / plan
// / ultrathink toggles + setLlmProfile) live in lib/composer_actions.js
// to keep app.js under the 500-line UI budget (FRONTEND_DESIGN.md
// §1.4 hard limit). Same factory pattern as lib/chat_actions.js.

import { createComposerActions } from "./lib/composer_actions.js";
const _COMPOSER = createComposerActions({
  store,
  getWsHandle: () => wsHandle,
  toast,
  appendOptimisticUser,
  appendThinkingAssistant,
  appendPromptHistory,
});
const sendComposer = _COMPOSER.sendComposer;
const setLlmProfile = _COMPOSER.setLlmProfile;
const cancelComposer = _COMPOSER.cancelComposer;
const answerQuestion = _COMPOSER.answerQuestion;
const changeDraft = _COMPOSER.changeDraft;
const togglePlan = _COMPOSER.togglePlan;
const toggleUltrathink = _COMPOSER.toggleUltrathink;

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
      // B-269: drop cancelled-turn tracking on session switch — stale
      // ids would just leak memory + confuse a future turn that
      // happens to reuse a correlation_id.
      cancelledTurnIds: new Set(),
    },
    session: { ...s.session, activeSid: sid, sids },
  });
  connectFor(sid, s.auth.token);
}

// Local-only chat clear (does NOT delete daemon-side history). Used by
// SlashPopover's /clear command.
function clearChat() {
  store.setState((s) => ({
    chat: {
      ...s.chat, messages: [], pendingAssistantId: null,
      cancelledTurnIds: new Set(),  // B-269
    },
  }));
  toast.info("已清空本地 chat 面板（daemon 历史保留）");
}

// B-106: /retry + /undo helpers live in lib/chat_actions.js (split out
// to keep app.js under the 500-line UI budget). Bind to our store +
// wsHandle here.
import { createChatActions } from "./lib/chat_actions.js";
import { startEventBanner } from "./lib/event_banner.js";
const _CHAT_HELPERS = createChatActions({
  store, getWsHandle: () => wsHandle,
});
const retryLast = _CHAT_HELPERS.retryLast;
const undoLast = _CHAT_HELPERS.undoLast;

// Action bag passed into the chat page so SlashPopover can wire its
// command items without each having to import every helper itself.
const CHAT_ACTIONS = {
  startNewSession,
  clearChat,
  retryLast,
  undoLast,
  togglePlan: (force) => typeof force === "boolean"
    ? store.setState((s) => ({ chat: { ...s.chat, planMode: force } }))
    : togglePlan(),
  toggleDebug: () => toast.info("Debug 模式 toggle (Phase B-9.x): 当前是 toast-only"),
};

// ── Routes ────────────────────────────────────────────────────────────

const { useEffect } = window.__xmc.preact_hooks;

let _navigate = null;

function Placeholder({ title, subtitle }) {
  return html`
    <section class="xmc-placeholder" aria-labelledby="placeholder-title">
      <h2 id="placeholder-title">${title}</h2>
      <p class="xmc-placeholder__subtitle">${subtitle}</p>
      <p class="xmc-placeholder__hint">
        即将上线 — 见 <code>docs/FRONTEND_REWORK.md</code>。
      </p>
    </section>
  `;
}

function Redirect({ to }) {
  useEffect(() => {
    if (_navigate) {
      _navigate(to, { replace: true });
    }
  }, [to]);
  return html`<div style="padding:2rem;text-align:center;color:var(--xmc-fg-muted)">正在跳转…</div>`;
}

const routes = {
  "/chat": (state) => html`
    <${ChatPage}
      chat=${state.chat}
      session=${state.session}
      connection=${state.connection}
      token=${state.auth.token}
      onSend=${sendComposer}
      onCancel=${cancelComposer}
      onAnswerQuestion=${answerQuestion}
      onChangeDraft=${changeDraft}
      onTogglePlan=${togglePlan}
      onToggleUltrathink=${toggleUltrathink}
      onNewSession=${startNewSession}
      onChangeModel=${setLlmProfile}
      onSwitchAgent=${switchAgent}
      slashStore=${CHAT_ACTIONS}
    />
  `,
  "/sessions": (state) => html`<${SessionsPage} token=${state.auth.token} />`,
  "/cron": (state) => html`<${CronPage} token=${state.auth.token} />`,
  "/logs":      (state) => html`<${LogsPage}      token=${state.auth.token} />`,
  // B-157: /env 路由删除 (B-137 已合入 /settings)。旧书签 → "未找到"
  // 路由由通配符兜底，不会 404。EnvPage 文件也一并删了。
  "/analytics": (state) => html`<${AnalyticsPage} token=${state.auth.token} />`,
  "/docs":      ()      => html`<${DocsPage} />`,
  "/trace":     (state) => html`<${TracePage} token=${state.auth.token} />`,
  "/workspace": (state) => html`<${WorkspacePage} token=${state.auth.token} />`,
  "/agents": (state) => html`<${AgentsPage} token=${state.auth.token} />`,
  "/channels": (state) => html`<${ChannelsPage} token=${state.auth.token} />`,
  "/skills": (state) => html`<${SkillsPage} token=${state.auth.token} />`,
  "/marketplace": (state) => html`<${MarketplacePage} token=${state.auth.token} />`,
  "/evolution": (state) => html`<${EvolutionPage} token=${state.auth.token} />`,
  "/cognition": (state) => html`<${CognitionPage} token=${state.auth.token} />`,
  "/memory": (state) => html`<${MemoryPage} token=${state.auth.token} />`,
  "/tools": (state) => html`<${ToolsPage} token=${state.auth.token} />`,
  "/security": (state) => html`<${SecurityPage} token=${state.auth.token} />`,
  // Phase F: /config /backup /doctor 合并到 /settings
  "/config":  () => html`<${Redirect} to="/settings" />`,
  "/backup":  () => html`<${Redirect} to="/settings" />`,
  "/doctor":  () => html`<${Redirect} to="/settings" />`,
  "/files":   (state) => html`<${FilesPage}   token=${state.auth.token} />`,
  "/dashboard": (state) => html`<${DashboardPage} token=${state.auth.token} />`,
  // B-159: /insights 整合到 /trace。route 留通配符兜底，不再注册。
  "/settings": (state) => html`<${SettingsPage} token=${state.auth.token} />`,
  "*": () => html`<${Placeholder} title="未找到" subtitle="未匹配的路由" />`,
};

// ── Shell ─────────────────────────────────────────────────────────────
//
// Phase B 1:1 port of Hermes web/src/App.tsx. The Hermes shell IS the
// shell — no toggles, no legacy fallback. Pages render into the main
// pane. Theme + language switch lives inside the sidebar footer.

function App({ state }) {
  // B-214: gate route rendering on auth fetched. Pre-B-214 the
  // route children mounted with token=undefined → their useEffect
  // hooks fired apiGet immediately, daemon returned 401 → page
  // showed "401 Unauthorized" or got stuck in a TokenNotReady
  // catch loop. Now we hold the route children until boot()'s
  // fetchPairingToken resolves (auth.fetched=true), so when they
  // mount the token is already real.
  const route = routes[state.route.path] || routes["*"];
  const ready = !!state.auth.fetched;
  return html`
    <${HermesAppShell} activePath=${state.route.path} token=${state.auth.token} tokenUsage=${state.chat.tokenUsage}>
      ${ready
        ? route(state)
        : html`<div class="xmc-h-loading" style="padding:2rem;text-align:center;color:var(--xmc-fg-muted)">正在初始化…</div>`}
      <${ToastViewport} />
      <${DialogViewport} />
    </${HermesAppShell}>
  `;
}

// ── Mount ─────────────────────────────────────────────────────────────

const root = document.getElementById("root");
root.removeAttribute("aria-busy");

function renderApp() {
  // B-223: wrap in top-level error boundary. Any throw inside <App>
  // surfaces as a recovery panel instead of blacking the whole tab.
  render(html`<${AppErrorBoundary}><${App} state=${store.getState()} /></${AppErrorBoundary}>`, root);
}

const { navigate } = installRouter(store, routes);
_navigate = navigate;
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
