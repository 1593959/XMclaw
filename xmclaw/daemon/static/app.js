// XMclaw — app entry. Wires pairing, WS client, chat reducer, page routes.

const { h, render } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

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
import { DoctorPage } from "./pages/Doctor.js";
import { ToolsPage } from "./pages/Tools.js";
import { AgentsPage } from "./pages/Agents.js";
import { ChannelsPage } from "./pages/Channels.js";
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
import { LogsPage } from "./pages/Logs.js";
import { EnvPage } from "./pages/Env.js";
import { AnalyticsPage } from "./pages/Analytics.js";
import { DocsPage } from "./pages/Docs.js";
import { TracePage } from "./pages/Trace.js";

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
}

const switchAgent = (agentId) =>
  switchAgentAction(store, agentId, persistActiveAgentId, connectFor);

// ── Action helpers (bound into the page tree) ─────────────────────────

function sendComposer() {
  const s = store.getState();
  const text = (s.chat.composerDraft || "").trim();
  if (!text) return;
  if (!wsHandle) {
    toast.error("WS 未连接，消息未发送 — 请检查 daemon 状态");
    return;
  }
  // B-105: persist this prompt in the up/down history before send.
  try {
    appendPromptHistory(text);
  } catch (_) { /* never block send on history */ }

  // Allow send even when reconnecting; the WS client now queues frames
  // and flushes them on reconnect (B-13 fix). Without this gate,
  // pressing Enter during a daemon restart would silently lose the
  // message — UI showed an optimistic bubble but the server never
  // got the frame.

  // Optimistic local echo. The daemon will mirror it back as USER_MESSAGE,
  // and the reducer will dedupe by id.
  const { id, chat: afterUser } = appendOptimisticUser(s.chat, text, {
    ultrathink: s.chat.ultrathink,
  });
  // Push a "thinking" assistant bubble keyed by `id` so the UI shows
  // immediate feedback. The reducer's llm_chunk / llm_response cases
  // upsert by id, transitioning this bubble into streaming/complete.
  const nextChat = appendThinkingAssistant(afterUser, id);
  store.setState({ chat: { ...nextChat, composerDraft: "" } });

  const result = wsHandle.send({
    type: "user",
    content: text,
    ultrathink: s.chat.ultrathink || undefined,
    correlation_id: id,
    plan_mode: s.chat.planMode || undefined,
    llm_profile_id: s.chat.llmProfileId || undefined,
  });

  // Tell the user when the frame is queued vs. sent. Queued frames
  // ride out the reconnect; rejected frames need to be retyped.
  if (result && result.queued) {
    toast.info(
      `当前未连接 daemon，消息已排队 (#${result.pendingCount}) — 重连后自动发送`,
    );
  } else if (result && !result.ok) {
    toast.error("发送失败：" + (result.reason || "未知"));
  }
}

function setLlmProfile(profileId) {
  store.setState((s) => ({
    chat: { ...s.chat, llmProfileId: profileId || null },
  }));
}

// B-38: send a cancel frame so the daemon's WS handler signals the
// running run_turn to bail at its next hop boundary. No-op when no
// turn is in flight (the server happily processes a stray cancel).
function cancelComposer() {
  if (!wsHandle) {
    toast.error("WS 未连接");
    return;
  }
  const result = wsHandle.send({ type: "cancel" });
  if (result && !result.ok) {
    toast.error("取消请求失败：" + (result.reason || "未知"));
  } else {
    toast.info("已请求停止当前回答");
  }
}

// B-92: forward an answer to the daemon. The QuestionCard built by
// MessageBubble calls this when the user clicks an option (or types
// "Other" free text). The daemon's WS handler resolves the in-flight
// ask_user_question Future and the agent's run_turn loop continues.
// ``value`` is a string for single-select / Other, or an array for
// multi-select.
function answerQuestion(questionId, value) {
  if (!wsHandle) {
    toast.error("WS 未连接，无法提交回答");
    return;
  }
  const result = wsHandle.send({
    type: "answer_question",
    question_id: questionId,
    value,
  });
  if (result && !result.ok) {
    toast.error("回答提交失败：" + (result.reason || "未知"));
  }
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

// Local-only chat clear (does NOT delete daemon-side history). Used by
// SlashPopover's /clear command.
function clearChat() {
  store.setState((s) => ({
    chat: { ...s.chat, messages: [], pendingAssistantId: null },
  }));
  toast.info("已清空本地 chat 面板（daemon 历史保留）");
}

// B-106: /retry + /undo helpers live in lib/chat_actions.js (split out
// to keep app.js under the 500-line UI budget). Bind to our store +
// wsHandle here.
import { createChatActions } from "./lib/chat_actions.js";
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
  togglePlan: (force) => {
    if (typeof force === "boolean") {
      store.setState((s) => ({ chat: { ...s.chat, planMode: force } }));
    } else {
      togglePlan();
    }
  },
  toggleDebug: () => {
    toast.info("Debug 模式 toggle (Phase B-9.x): 当前是 toast-only");
  },
};

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
  "/config": (state) => html`<${ConfigPage} token=${state.auth.token} />`,
  "/logs":      (state) => html`<${LogsPage}      token=${state.auth.token} />`,
  // B-137: /env 合入 /settings；route 留兼容性给旧书签。
  "/env":       (state) => html`<${EnvPage}       token=${state.auth.token} />`,
  "/analytics": (state) => html`<${AnalyticsPage} token=${state.auth.token} />`,
  "/docs":      ()      => html`<${DocsPage} />`,
  "/trace":     (state) => html`<${TracePage} token=${state.auth.token} />`,
  "/workspace": (state) => html`<${WorkspacePage} token=${state.auth.token} />`,
  "/agents": (state) => html`<${AgentsPage} token=${state.auth.token} />`,
  "/channels": (state) => html`<${ChannelsPage} token=${state.auth.token} />`,
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
    <${HermesAppShell} activePath=${state.route.path} token=${state.auth.token} tokenUsage=${state.chat.tokenUsage}>
      ${route(state)}
      <${ToastViewport} />
      <${DialogViewport} />
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
