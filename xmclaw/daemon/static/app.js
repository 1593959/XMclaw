// XMclaw v2 web UI -- three-pane layout + tool-call cards + themes.
// Vanilla ES2020 -- no framework, no bundler. Event rendering must stay
// in sync with xmclaw/cli/chat.py::format_event (same schema tags).

"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// ── persistence keys ─────────────────────────────────────────────
const SESS_KEY   = "xmclaw_v2_sessions";
const ACTIVE_KEY = "xmclaw_v2_active";
const THEME_KEY  = "xmclaw_v2_theme";

const THEMES = ["dark", "light", "slate", "oled"];
const THEME_ICONS = { dark: "◐", light: "○", slate: "◑", oled: "●" };

// ── DOM refs ─────────────────────────────────────────────────────
const connEl        = $("conn-indicator");
const connTextEl    = $("conn-text");
const buildInfoEl   = $("build-info");
const sessionListEl = $("session-list");
const sessionCountEl= $("session-count");
const newSessionBtn = $("new-session");
const agentSubEl    = $("agent-sub");
const sessionIdEl   = $("session-id");
const eventsEl      = $("events");
const sendForm      = $("send-form");
const userInput     = $("user-input");
const costTickerEl  = $("cost-ticker");
const charCounterEl = $("char-counter");
const themeBtn      = $("theme-btn");
const themeIconEl   = $("theme-icon");
const toggleWsBtn   = $("toggle-workspace");
const toggleSbBtn   = $("toggle-sidebar");
const wsCloseBtn    = $("ws-close");
const wsTitleEl     = $("ws-title");
const sidebarEl     = $("sidebar");
const workspaceEl   = $("workspace");
const appEl         = document.querySelector(".app");
const ringFillEl    = document.querySelector(".ring-fill");
const ringLabelEl   = $("ring-label");
const contextRing   = $("context-ring");
const wsActivityEl  = $("ws-activity");
const wsToolsEl     = $("ws-tools");
const wsModelsEl    = $("ws-models");
const wsSessionsEl  = $("ws-sessions");
const wsConfigEl    = $("ws-config");
const aboutDaemonEl = $("about-daemon");
const aboutModelEl  = $("about-model");
const aboutToolsEl  = $("about-tools");
const welcomeEl     = $("welcome");
const chatTitleEl   = $("chat-title");
const brandVerEl    = $("brand-version");
const modelBtn      = $("model-btn");
const modelMenu     = $("model-menu");
const modelNameEl   = $("model-name");
const modelListEl   = $("model-list");
const secAuthEl     = $("sec-auth");
const secSandboxEl  = $("sec-sandbox");
const secBashEl     = $("sec-bash");
const secWebEl      = $("sec-web");
const tokPromptEl   = $("tok-prompt");
const tokComplEl    = $("tok-completion");
const tokTotalEl    = $("tok-total");
const tokCostEl     = $("tok-cost");
const ultrathinkBtn = $("ultrathink-btn");
const ultrathinkChip = $("ultrathink-chip");
const wsTodosEl     = $("ws-todos");
const wsTimelineEl  = $("ws-timeline");
const timelineClearBtn = $("timeline-clear");
const navBadgeTodos = $("nav-badge-todos");
const navBadgeActivity = $("nav-badge-activity");
const navBadgeTimeline = $("nav-badge-timeline");
const wsMcpEl       = $("ws-mcp");
const wsSkillsEl    = $("ws-skills");
const wsAgentsEl    = $("ws-agents");
const wsFilesEl     = $("ws-files");
const fbPathEl      = $("fb-path");
const fbGoBtn       = $("fb-go");

const ULTRATHINK_KEY = "xmclaw_v2_ultrathink";
const TIMELINE_MAX = 300;

// ── state ────────────────────────────────────────────────────────
const state = {
  ws: null,
  token: null,
  sid: null,
  sessions: [],
  thinkingRow: null,
  // Map tool_call id -> the <details> element rendered inline,
  // so we can update its status as tool_invocation_finished arrives.
  toolCards: new Map(),
  // Recent activity items for the workspace pane.
  activity: [],
  totalTokens: 0,
  // True while the server is streaming the hydration buffer after a
  // reconnect. We suppress the thinking-dots animation during this
  // window (otherwise a fresh ellipsis appears next to every replayed
  // turn) and we don't touch per-session token counters.
  replaying: false,
  // Ultrathink toggle state. When true, every subsequent user frame
  // carries ultrathink=true and the server prepends a step-by-step
  // directive to the model's input.
  ultrathink: false,
  // Timeline panel state: bounded ring of events seen, newest last.
  timeline: [],
  // Todos panel state: last TODO_UPDATED payload for this session.
  todos: [],
};

const CTX_WINDOW = 120_000;  // match config memory.max_context_tokens

// ── theme ────────────────────────────────────────────────────────

function applyTheme(name) {
  if (!THEMES.includes(name)) name = "dark";
  document.documentElement.setAttribute("data-theme", name);
  themeIconEl.textContent = THEME_ICONS[name] || "◐";
  localStorage.setItem(THEME_KEY, name);
}

function cycleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  applyTheme(next);
}

// ── sessions ─────────────────────────────────────────────────────

function loadSessions() {
  try {
    const arr = JSON.parse(localStorage.getItem(SESS_KEY) || "[]");
    if (Array.isArray(arr)) return arr.filter(s => typeof s === "string");
  } catch (_) { /* ignore */ }
  return [];
}

function saveSessions(list) {
  localStorage.setItem(SESS_KEY, JSON.stringify(list));
}

function newSid() {
  return "chat-" + Math.random().toString(16).slice(2, 10);
}

function activateSession(sid) {
  state.sid = sid;
  localStorage.setItem(ACTIVE_KEY, sid);
  sessionIdEl.textContent = sid;
  if (chatTitleEl) chatTitleEl.textContent = sid;
  window.location.hash = "s=" + encodeURIComponent(sid);
  clearEvents();
  state.toolCards.clear();
  state.activity = [];
  // Reset per-session token totals (but history lives server-side).
  state.totalTokens = 0;
  state.totalPrompt = 0;
  state.totalCompl  = 0;
  state.timeline = [];
  state.todos = [];
  renderActivity();
  renderTodos();
  renderTimeline();
  updateNavBadges();
  renderSessionList();
  if (typeof renderSessionsPanel === "function") renderSessionsPanel();
  connect();
}

function createSession() {
  const sid = newSid();
  if (!state.sessions.includes(sid)) state.sessions.unshift(sid);
  saveSessions(state.sessions);
  activateSession(sid);
}

function removeSession(sid) {
  state.sessions = state.sessions.filter(s => s !== sid);
  saveSessions(state.sessions);
  if (state.sid === sid) {
    if (state.sessions.length > 0) activateSession(state.sessions[0]);
    else createSession();
  } else {
    renderSessionList();
  }
}

function renderSessionList() {
  sessionListEl.textContent = "";
  sessionCountEl.textContent = state.sessions.length.toString();
  if (state.sessions.length === 0) {
    const e = el("div", "ws-empty", "还没有会话。");
    e.style.padding = "8px 10px";
    sessionListEl.appendChild(e);
    return;
  }
  state.sessions.forEach(sid => {
    const item = el("div", "session-item" + (sid === state.sid ? " active" : ""));
    item.appendChild(el("span", "label", sid));
    const kill = el("button", "kill", "×");
    kill.title = "删除会话";
    kill.addEventListener("click", (ev) => {
      ev.stopPropagation();
      removeSession(sid);
    });
    item.appendChild(kill);
    item.addEventListener("click", () => activateSession(sid));
    sessionListEl.appendChild(item);
  });
}

// ── events rendering helpers ─────────────────────────────────────

function clearEvents() {
  eventsEl.textContent = "";
  // Welcome is shown whenever the event list is empty.
  showWelcome(true);
}

function showWelcome(show) {
  if (!welcomeEl) return;
  welcomeEl.hidden = !show;
}

function removeEmptyState() {
  // No longer an inline ".msg.system" row -- welcome is a sibling panel.
  showWelcome(false);
}

function renderRow(kind, inner) {
  removeEmptyState();
  const row = el("div", "msg " + kind);
  const av = el("div", "av");
  av.textContent = kind === "user" ? "U" : (
    kind === "violation" ? "⚠" : "🦞"
  );
  row.appendChild(av);
  row.appendChild(inner);
  eventsEl.appendChild(row);
  eventsEl.scrollTop = eventsEl.scrollHeight;
  return row;
}

/** Lightweight Markdown renderer: fenced code blocks + inline code.
 *  Good enough for LLM output; avoids pulling in a parser library. */
function renderMarkdownLite(text) {
  const bubble = el("div", "bubble");
  const parts = text.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const lines = part.split("\n");
      const firstLine = lines[0];
      const maybeLang = /^[a-zA-Z0-9_+-]+$/.test(firstLine.trim()) ? firstLine : "";
      const body = maybeLang ? lines.slice(1).join("\n") : part;
      const pre = el("pre"); const code = el("code", null, body);
      pre.appendChild(code); bubble.appendChild(pre);
    } else {
      const paras = part.split(/\n\n+/);
      paras.forEach(para => {
        if (!para.trim()) return;
        const p = el("p");
        para.split(/(`[^`]+`)/).forEach(seg => {
          if (seg.startsWith("`") && seg.endsWith("`") && seg.length >= 2) {
            p.appendChild(el("code", null, seg.slice(1, -1)));
          } else {
            p.appendChild(document.createTextNode(seg));
          }
        });
        bubble.appendChild(p);
      });
    }
  });
  return bubble;
}

function appendAgent(text) {
  dismissThinking();
  renderRow("agent", renderMarkdownLite(text));
}

function appendUser(text) {
  const bubble = el("div", "bubble");
  bubble.textContent = text;
  renderRow("user", bubble);
}

function appendThinking() {
  if (state.thinkingRow) return;
  const bubble = el("div", "bubble");
  const dots = el("div", "thinking");
  dots.appendChild(el("span"));
  dots.appendChild(el("span"));
  dots.appendChild(el("span"));
  bubble.appendChild(dots);
  state.thinkingRow = renderRow("agent", bubble);
}

function dismissThinking() {
  if (state.thinkingRow) { state.thinkingRow.remove(); state.thinkingRow = null; }
}

function appendViolation(msg) {
  dismissThinking();
  const bubble = el("div", "bubble");
  bubble.textContent = "⚠ " + msg;
  renderRow("violation", bubble);
}

// ── tool-call cards (inline, expandable) ────────────────────────

function appendToolCard(callId, name, args) {
  dismissThinking();
  removeEmptyState();
  const wrap = el("div", "tool-card");
  wrap.appendChild(Object.assign(el("div", "av"), { textContent: "⚙" }));

  const details = document.createElement("details");
  details.className = "card";

  const summary = document.createElement("summary");
  summary.appendChild(Object.assign(el("span", "tool-name"), { textContent: name }));
  const preview = el("span", "tool-arg-preview", JSON.stringify(args || {}));
  summary.appendChild(preview);
  const status = el("span", "tool-status running", "running…");
  summary.appendChild(status);
  details.appendChild(summary);

  const body = el("div", "tool-body");
  body.appendChild(Object.assign(el("div", "block-label"), { textContent: "args" }));
  body.appendChild(Object.assign(el("div", "block-body"),
    { textContent: JSON.stringify(args || {}, null, 2) }));
  details.appendChild(body);

  wrap.appendChild(details);
  eventsEl.appendChild(wrap);
  eventsEl.scrollTop = eventsEl.scrollHeight;

  state.toolCards.set(callId, { details, status, body });
  state.activity.push({
    name, args, ts: Date.now(), ok: null, error: null,
  });
  renderActivity();
}

function updateToolCard(callId, ok, result, errorText, name) {
  const card = state.toolCards.get(callId);
  if (!card) return;
  const { status, body } = card;
  status.classList.remove("running");
  if (ok) {
    status.classList.add("ok");
    status.textContent = "ok";
  } else {
    status.classList.add("fail");
    status.textContent = "failed";
  }
  body.appendChild(Object.assign(el("div", "block-label"),
    { textContent: ok ? "result" : "error" }));

  if (!ok) {
    body.appendChild(Object.assign(el("div", "block-body"),
      { textContent: errorText ?? "(no error)" }));
  } else {
    renderToolResult(body, name, result);
  }

  for (let i = state.activity.length - 1; i >= 0; i--) {
    if (state.activity[i].ok === null) {
      state.activity[i].ok = ok;
      state.activity[i].error = errorText;
      break;
    }
  }
  renderActivity();
  // Auto-open the details for newly-finished tools so the rich
  // content is immediately visible. User can collapse to tidy up.
  card.details.open = true;
}

// Render a tool's structured result with tool-name-specific affordances:
//   - browser_screenshot: inline <img> from the data_url
//   - browser_snapshot:   title + link list + collapsed text body
//   - everything else:    pretty-printed JSON (unchanged behavior)
function renderToolResult(body, name, result) {
  if (name === "browser_screenshot" && result && result.data_url) {
    const caption = el("div", "tool-img-caption",
      `${result.bytes ?? "?"} B · ${result.url || ""}`);
    const img = document.createElement("img");
    img.className = "tool-img";
    img.alt = "browser screenshot";
    img.loading = "lazy";
    img.src = result.data_url;
    body.appendChild(img);
    body.appendChild(caption);
    return;
  }
  if (name === "browser_snapshot" && result && typeof result === "object") {
    const { title, url, text, links } = result;
    if (title) {
      body.appendChild(el("div", "tool-snap-title", title));
    }
    if (url) {
      const link = document.createElement("a");
      link.href = url; link.target = "_blank"; link.rel = "noopener";
      link.className = "tool-snap-url";
      link.textContent = url;
      body.appendChild(link);
    }
    if (Array.isArray(links) && links.length) {
      const label = el("div", "block-label", `links (${links.length})`);
      body.appendChild(label);
      const list = el("ul", "tool-snap-links");
      links.slice(0, 20).forEach(l => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = l.href; a.target = "_blank"; a.rel = "noopener";
        a.textContent = l.label || l.href;
        li.appendChild(a);
        list.appendChild(li);
      });
      body.appendChild(list);
    }
    if (text) {
      body.appendChild(el("div", "block-label", "text"));
      const pre = el("pre", "tool-snap-text");
      pre.textContent = text;
      body.appendChild(pre);
    }
    return;
  }
  // Fallback: pretty-printed JSON (or string pass-through).
  const txt = typeof result === "string"
    ? result
    : (result === null || result === undefined)
      ? "(no content)"
      : JSON.stringify(result, null, 2);
  body.appendChild(Object.assign(el("div", "block-body"),
    { textContent: txt }));
}

// ── workspace (right pane) ───────────────────────────────────────

function renderActivity() {
  wsActivityEl.textContent = "";
  if (state.activity.length === 0) {
    wsActivityEl.appendChild(el("div", "ws-empty", "暂无工具调用。"));
    return;
  }
  // newest first
  const rows = state.activity.slice().reverse();
  rows.forEach(a => {
    const cls = a.ok === true ? "ok" : (a.ok === false ? "fail" : "");
    const row = el("div", "activity-item " + cls);
    row.appendChild(el("div", "name", a.name));
    const args = JSON.stringify(a.args || {});
    row.appendChild(el("div", "args", args.length > 60
      ? args.slice(0, 60) + "…" : args));
    const ts = new Date(a.ts).toLocaleTimeString();
    row.appendChild(el("div", "ts", ts + (a.error ? " · " + a.error : "")));
    wsActivityEl.appendChild(row);
  });
}

const WS_TAB_TITLES = {
  activity: "工具活动", todos: "待办", timeline: "事件流",
  sessions: "所有会话", files: "文件", skills: "技能", tools: "工具",
  mcp: "MCP", config: "运行配置",
  agents: "智能体", models: "模型", security: "安全",
  tokens: "Token 消耗", about: "关于",
};

function switchWsTab(name) {
  if (!name) return;
  document.querySelectorAll(".nav-item").forEach(n =>
    n.classList.toggle("active", n.dataset.wsTab === name)
  );
  document.querySelectorAll(".ws-panel").forEach(p =>
    p.classList.toggle("active", p.dataset.panel === name)
  );
  if (wsTitleEl) wsTitleEl.textContent = WS_TAB_TITLES[name] || name;
  // Make sure the workspace is visible when a nav-item is clicked.
  if (appEl && appEl.classList.contains("no-workspace")) {
    appEl.classList.remove("no-workspace");
  }
}

function setupNav() {
  document.querySelectorAll(".nav-item").forEach(item => {
    if (item.classList.contains("nav-disabled")) return;
    const tab = item.dataset.wsTab;
    if (!tab) return;
    item.addEventListener("click", () => switchWsTab(tab));
  });
}

function toggleWorkspace() {
  if (!appEl) return;
  appEl.classList.toggle("no-workspace");
}

function toggleSidebar() {
  if (!sidebarEl) return;
  sidebarEl.classList.toggle("open");
}

// ── welcome-card click-through populates the composer ─────────────

function setupWelcomeCards() {
  document.querySelectorAll(".welcome-card").forEach(card => {
    card.addEventListener("click", () => {
      const text = card.dataset.suggest || "";
      if (!text) return;
      userInput.value = text;
      userInput.focus();
      autoGrow();
      updateCharCounter();
    });
  });
}

// ── character counter ──────────────────────────────────────────────

function updateCharCounter() {
  if (!charCounterEl || !userInput) return;
  const len = (userInput.value || "").length;
  const max = Number(userInput.getAttribute("maxlength") || 10000);
  charCounterEl.textContent = `${len}/${max}`;
  charCounterEl.classList.remove("warn", "err");
  const pct = len / max;
  if (pct > 0.95)      charCounterEl.classList.add("err");
  else if (pct > 0.8)  charCounterEl.classList.add("warn");
}

// ── model picker ───────────────────────────────────────────────────

function toggleModelMenu(forceShow) {
  if (!modelMenu) return;
  const hiddenNow = modelMenu.hasAttribute("hidden");
  const show = forceShow === undefined ? hiddenNow : forceShow;
  if (show) modelMenu.removeAttribute("hidden");
  else      modelMenu.setAttribute("hidden", "");
}

function setActiveModel(name, provider) {
  if (modelNameEl) modelNameEl.textContent = name || "—";
  if (aboutModelEl) aboutModelEl.textContent = name
    ? `${name}  (${provider || "?"})` : "—";
}

function populateModelMenu(active) {
  if (!modelListEl) return;
  // Static roster for now -- the daemon doesn't yet expose a
  // /api/v2/models endpoint. Providers are what factory recognizes.
  const known = [
    { name: active || "—", provider: "active" },
  ];
  modelListEl.textContent = "";
  known.forEach(m => {
    const row = el("div", "model-entry" + (m.provider === "active" ? " active" : ""));
    row.appendChild(el("span", "model-dot-small"));
    row.appendChild(el("span", null, m.name));
    row.appendChild(el("span", "model-provider", m.provider));
    modelListEl.appendChild(row);
  });
  // Also render into the Models workspace panel.
  if (wsModelsEl) {
    wsModelsEl.textContent = "";
    known.forEach(m => {
      const entry = el("div", "ws-tool-entry");
      entry.appendChild(el("div", "name", m.name));
      entry.appendChild(el("div", "desc",
        `provider: ${m.provider}. Change in daemon/config.json.`));
      wsModelsEl.appendChild(entry);
    });
  }
}

// ── context ring ─────────────────────────────────────────────────

function updateContextRing(tokens, breakdown) {
  state.totalTokens = (state.totalTokens || 0) + (tokens || 0);
  const pct = Math.min(1, state.totalTokens / CTX_WINDOW);
  const circumference = 2 * Math.PI * 13;
  const offset = circumference * (1 - pct);
  if (ringFillEl) ringFillEl.setAttribute("stroke-dashoffset", offset.toFixed(1));
  if (ringLabelEl) ringLabelEl.textContent = Math.round(pct * 100) + "%";
  if (contextRing) {
    contextRing.classList.remove("warn", "err");
    if (pct > 0.9) contextRing.classList.add("err");
    else if (pct > 0.7) contextRing.classList.add("warn");
  }
  // Workspace > Tokens panel
  if (breakdown) {
    state.totalPrompt = (state.totalPrompt || 0) + (breakdown.prompt || 0);
    state.totalCompl  = (state.totalCompl  || 0) + (breakdown.completion || 0);
    if (tokPromptEl) tokPromptEl.textContent = state.totalPrompt.toLocaleString();
    if (tokComplEl)  tokComplEl.textContent  = state.totalCompl.toLocaleString();
    if (tokTotalEl)  tokTotalEl.textContent  = state.totalTokens.toLocaleString();
  }
}

function updateCostTicker(p) {
  const spent  = (p.spent_usd  || 0).toFixed(4);
  const budget = p.budget_usd;
  costTickerEl.textContent =
    `cost $${spent}${budget ? " / $" + budget : ""}`;
  if (tokCostEl) tokCostEl.textContent = "$" + spent;
}

// ── event dispatch ───────────────────────────────────────────────

function renderEvent(evt) {
  const p = evt.payload || {};
  const isReplay = evt.replayed === true;

  // Timeline captures EVERY event regardless of type (including the
  // session_replay markers) so the user can always inspect the raw stream.
  pushTimeline(evt);

  switch (evt.type) {
    case "session_replay": {
      // Marker frames bracket the hydration stream.
      if (p.phase === "start") {
        state.replaying = true;
        clearEvents();        // wipe the empty-state so replays can paint
        showWelcome(false);
      } else if (p.phase === "end") {
        state.replaying = false;
        // If after replay the events div is still empty, show welcome again.
        if (eventsEl.children.length === 0) showWelcome(true);
      }
      return;
    }

    case "user_message":
      // Normally suppressed -- sender echo-renders locally. But on a
      // replay we didn't echo (the sender is long gone), so render it.
      if (isReplay) {
        const text = (p.content || "").toString();
        if (text) appendUser(text);
      }
      return;

    case "llm_request":
      // Never show the thinking spinner for replayed events -- the
      // response already exists in the buffer, we'd just flash dots.
      if (!isReplay && (p.hop ?? 0) === 0) appendThinking();
      return;

    case "llm_response": {
      if (p.ok === false) {
        if (!isReplay) appendViolation("llm error: " + (p.error || "?"));
        return;
      }
      // Update context ring (tokens). On replay we DO want the ring to
      // reflect the full history's usage, so don't skip this.
      updateContextRing(
        (p.prompt_tokens || 0) + (p.completion_tokens || 0),
        { prompt: p.prompt_tokens || 0, completion: p.completion_tokens || 0 },
      );
      // Terminal hop: no tool calls AND has content -> show assistant text.
      if ((p.tool_calls_count || 0) === 0 && (p.content_length || 0) > 0) {
        appendAgent(p.content || "");
      } else {
        // Mid-loop: the tool is next. Keep thinking indicator visible.
      }
      return;
    }

    case "tool_call_emitted":
      appendToolCard(p.call_id, p.name || "tool", p.args || {});
      return;

    case "tool_invocation_started":
      // Visible status already on the card ("running…").
      return;

    case "tool_invocation_finished": {
      // Pass the structured result through so updateToolCard can
      // render rich views for known tool shapes (image screenshots,
      // link lists, etc). Falls back to pretty-printed JSON otherwise.
      updateToolCard(p.call_id, p.ok !== false, p.result, p.error, p.name);
      return;
    }

    case "cost_tick":
      updateCostTicker(p);
      return;

    case "anti_req_violation":
      if (!isReplay) appendViolation(p.message || "anti-req violation");
      return;

    case "session_lifecycle":
      return;

    case "todo_updated":
      state.todos = Array.isArray(p.items) ? p.items : [];
      renderTodos();
      updateNavBadges();
      return;

    default:
      return;
  }
}

// ── Todos panel rendering ────────────────────────────────────────

function renderTodos() {
  if (!wsTodosEl) return;
  wsTodosEl.textContent = "";
  if (!state.todos.length) {
    wsTodosEl.appendChild(el("div", "ws-empty", "还没有待办项。"));
    return;
  }
  state.todos.forEach((t, i) => {
    const status = t.status || "pending";
    const row = el("div", "todo-item todo-" + status);
    const glyph = {
      pending: "○", in_progress: "◐", done: "●",
    }[status] || "○";
    row.appendChild(el("span", "todo-glyph", glyph));
    row.appendChild(el("span", "todo-num", (i + 1).toString()));
    row.appendChild(el("span", "todo-content", t.content || ""));
    row.appendChild(el("span", "todo-status", {
      pending: "待定", in_progress: "进行中", done: "完成",
    }[status] || status));
    wsTodosEl.appendChild(row);
  });
}

// ── Timeline panel: bounded ring of every event seen ─────────────

function pushTimeline(evt) {
  state.timeline.push({
    type: evt.type,
    ts: evt.ts || (Date.now() / 1000),
    payload: evt.payload || {},
    replayed: evt.replayed === true,
  });
  if (state.timeline.length > TIMELINE_MAX) {
    state.timeline.splice(0, state.timeline.length - TIMELINE_MAX);
  }
  renderTimeline();
  updateNavBadges();
}

function renderTimeline() {
  if (!wsTimelineEl) return;
  wsTimelineEl.textContent = "";
  if (!state.timeline.length) {
    wsTimelineEl.appendChild(el("div", "ws-empty", "暂无事件。"));
    return;
  }
  // newest first
  const rows = state.timeline.slice().reverse();
  rows.forEach(ev => {
    const row = document.createElement("details");
    row.className = "timeline-entry timeline-" + ev.type;
    const summary = document.createElement("summary");
    const t = new Date(ev.ts * 1000);
    summary.appendChild(el("span", "tl-time",
      t.toLocaleTimeString("zh-CN", { hour12: false })));
    summary.appendChild(el("span", "tl-type", ev.type));
    const previewKey = Object.keys(ev.payload)[0];
    if (previewKey) {
      const v = ev.payload[previewKey];
      const s = typeof v === "string" ? v : JSON.stringify(v);
      summary.appendChild(el("span", "tl-preview",
        `${previewKey}=${s.slice(0, 60)}`));
    }
    if (ev.replayed) summary.appendChild(el("span", "tl-replay", "已重放"));
    row.appendChild(summary);
    const body = el("pre", "tl-body");
    body.textContent = JSON.stringify(ev.payload, null, 2);
    row.appendChild(body);
    wsTimelineEl.appendChild(row);
  });
}

function clearTimeline() {
  state.timeline = [];
  renderTimeline();
  updateNavBadges();
}

function updateNavBadges() {
  if (navBadgeActivity) {
    const n = state.activity.length;
    navBadgeActivity.textContent = n > 0 ? String(n) : "";
  }
  if (navBadgeTodos) {
    const n = state.todos.length;
    navBadgeTodos.textContent = n > 0 ? String(n) : "";
  }
  if (navBadgeTimeline) {
    const n = state.timeline.length;
    navBadgeTimeline.textContent = n > 0 ? (n > 99 ? "99+" : String(n)) : "";
  }
}

// ── Ultrathink toggle ────────────────────────────────────────────

function applyUltrathink(on) {
  state.ultrathink = !!on;
  localStorage.setItem(ULTRATHINK_KEY, on ? "1" : "0");
  if (ultrathinkBtn) {
    ultrathinkBtn.classList.toggle("active", state.ultrathink);
  }
  if (ultrathinkChip) {
    ultrathinkChip.hidden = !state.ultrathink;
  }
}

function toggleUltrathink() { applyUltrathink(!state.ultrathink); }

// ── WS connection ────────────────────────────────────────────────

async function fetchPairingToken() {
  try {
    const r = await fetch("/api/v2/pair");
    if (!r.ok) return null;
    const j = await r.json();
    return j.token || null;
  } catch (_) { return null; }
}

async function fetchHealth() {
  try {
    const r = await fetch("/health");
    if (!r.ok) return null;
    return await r.json();
  } catch (_) { return null; }
}

function connect() {
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
    state.ws = null;
  }
  const url = new URL("/agent/v2/" + state.sid, window.location.href);
  url.protocol = url.protocol.replace("http", "ws");
  if (state.token) url.searchParams.set("token", state.token);
  const ws = new WebSocket(url.toString());
  state.ws = ws;
  setConn("connecting");
  ws.addEventListener("open", () => setConn("connected"));
  ws.addEventListener("close", () => setConn("disconnected"));
  ws.addEventListener("error", () => setConn("disconnected"));
  ws.addEventListener("message", (ev) => {
    try { renderEvent(JSON.parse(ev.data)); }
    catch (err) { console.error("bad frame", err, ev.data); }
  });
}

function setConn(status) {
  connEl.classList.remove("conn-up", "conn-down");
  const labels = {
    connected: "已连接", connecting: "连接中…", disconnected: "未连接",
  };
  if (status === "connected") {
    connEl.classList.add("conn-up");
  } else {
    connEl.classList.add("conn-down");
  }
  connTextEl.textContent = labels[status] || status;
}

function sendUser() {
  const val = userInput.value.trim();
  if (!val || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  appendUser(val);
  const payload = { type: "user", content: val };
  if (state.ultrathink) payload.ultrathink = true;
  state.ws.send(JSON.stringify(payload));
  userInput.value = "";
  userInput.style.height = "auto";
  updateCharCounter();
}

// ── init ─────────────────────────────────────────────────────────

function autoGrow() {
  userInput.style.height = "auto";
  userInput.style.height = Math.min(userInput.scrollHeight, 180) + "px";
}

function renderSessionsPanel() {
  if (!wsSessionsEl) return;
  wsSessionsEl.textContent = "";
  if (!state.sessions.length) {
    wsSessionsEl.appendChild(el("div", "ws-empty",
      "No sessions yet. Click + New chat to start one."));
    return;
  }
  state.sessions.forEach(sid => {
    const row = el("div", "ws-sess-row" + (sid === state.sid ? " active" : ""));
    row.appendChild(el("span", null, sid));
    wsSessionsEl.appendChild(row);
  });
}

function renderSecurityPanel(health, hasAuth, tools) {
  if (secAuthEl)    secAuthEl.textContent    = hasAuth ? "pairing token required" : "no auth (local only)";
  if (secSandboxEl) secSandboxEl.textContent = "no allowed_dirs (full user access)";
  if (secBashEl)    secBashEl.textContent    = tools.includes("bash") ? "enabled" : "disabled";
  if (secWebEl)     secWebEl.textContent     = (tools.includes("web_fetch") ||
                                                tools.includes("web_search")) ? "enabled" : "disabled";
}

function renderConfigPanel(cfg) {
  if (!wsConfigEl) return;
  wsConfigEl.textContent = "";
  if (!cfg) {
    wsConfigEl.appendChild(el("div", "ws-empty",
      "daemon 没有加载 config 文件,所有设定走默认。"));
    return;
  }
  const pre = el("pre");
  pre.textContent = JSON.stringify(cfg, null, 2);
  wsConfigEl.appendChild(pre);
}

// ── /api/v2/status + config -> populate multiple panels ──────────

async function fetchStatus() {
  try {
    const r = await fetch("/api/v2/status");
    if (!r.ok) return null;
    return await r.json();
  } catch (_) { return null; }
}

async function fetchConfigSnapshot() {
  try {
    const r = await fetch("/api/v2/config");
    if (!r.ok) return null;
    return await r.json();
  } catch (_) { return null; }
}

function renderMcpPanel(servers) {
  if (!wsMcpEl) return;
  wsMcpEl.textContent = "";
  if (!servers || servers.length === 0) {
    wsMcpEl.appendChild(el("div", "ws-empty",
      "没有配置 MCP 服务器。在 daemon/config.json 的 mcp_servers 下加一个条目再重启。"));
    return;
  }
  servers.forEach(name => {
    const entry = el("div", "ws-tool-entry");
    entry.appendChild(el("div", "name", name));
    entry.appendChild(el("div", "desc",
      "声明于 config.mcp_servers。(自动挂载到 agent 是后续任务。)"));
    wsMcpEl.appendChild(entry);
  });
}

function renderSkillsPanel() {
  if (!wsSkillsEl) return;
  wsSkillsEl.textContent = "";
  // We don't have a /api/v2/skills yet; the SkillRegistry exists but
  // no HTTP surface. Show a useful placeholder until the endpoint lands.
  wsSkillsEl.appendChild(el("div", "ws-empty",
    "SkillRegistry 里当前没有安装技能。EvolutionEngine 生成并 promote 后会出现在这里。"));
}

function renderAgentsPanel(status) {
  if (!wsAgentsEl) return;
  wsAgentsEl.textContent = "";
  const entry = el("div", "ws-tool-entry");
  entry.appendChild(el("div", "name", "agent"));
  entry.appendChild(el("div", "desc",
    `默认智能体,model=${status?.model || "—"}。` +
    `多智能体隔离会在后续 worktree 隔离落地时一起上。`));
  wsAgentsEl.appendChild(entry);
}

// ── File browser (uses /api/v2/status info + XHR list via bash) ──

function renderFileBrowser(lines) {
  if (!wsFilesEl) return;
  wsFilesEl.textContent = "";
  if (!lines || !lines.length) {
    wsFilesEl.appendChild(el("div", "ws-empty", "该目录为空。"));
    return;
  }
  lines.forEach(line => {
    const row = el("div", "fb-row", line);
    wsFilesEl.appendChild(row);
  });
}

async function runFileBrowser() {
  const path = (fbPathEl?.value || "").trim();
  if (!path) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // We ask the agent to list via its existing list_dir tool; simplest.
  // This renders in the chat AND shows up in activity. A dedicated
  // /api/v2/files endpoint is a follow-up if this feels too indirect.
  userInput.value = `请用 list_dir 查看 ${path} 里的内容`;
  document.getElementById("send-form").requestSubmit();
}

async function init() {
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  setupNav();
  setupWelcomeCards();
  applyUltrathink(localStorage.getItem(ULTRATHINK_KEY) === "1");

  themeBtn.addEventListener("click", cycleTheme);
  toggleWsBtn.addEventListener("click", toggleWorkspace);
  if (wsCloseBtn) wsCloseBtn.addEventListener("click", toggleWorkspace);
  if (toggleSbBtn) toggleSbBtn.addEventListener("click", toggleSidebar);
  newSessionBtn.addEventListener("click", createSession);
  if (ultrathinkBtn) ultrathinkBtn.addEventListener("click", toggleUltrathink);
  if (timelineClearBtn) timelineClearBtn.addEventListener("click", clearTimeline);
  if (fbGoBtn) fbGoBtn.addEventListener("click", runFileBrowser);
  if (fbPathEl) {
    fbPathEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); runFileBrowser(); }
    });
  }

  // Model picker: click toggles menu, outside click closes.
  if (modelBtn) modelBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleModelMenu();
  });
  document.addEventListener("click", (ev) => {
    if (modelMenu && !modelMenu.hidden &&
        !modelMenu.contains(ev.target) &&
        ev.target !== modelBtn && !modelBtn.contains(ev.target)) {
      toggleModelMenu(false);
    }
  });

  sendForm.addEventListener("submit", (ev) => { ev.preventDefault(); sendUser(); });
  userInput.addEventListener("input", () => { autoGrow(); updateCharCounter(); });
  userInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault(); sendUser();
    }
  });
  updateCharCounter();

  // Default to Activity tab selected.
  switchWsTab("activity");
  // But we opened the workspace only because switchWsTab un-collapses it.
  // On first load we want the workspace visible by default on desktop.

  state.sessions = loadSessions();
  state.token = await fetchPairingToken();
  const health = await fetchHealth();
  if (health) {
    buildInfoEl.textContent = health.version || "—";
    if (brandVerEl) brandVerEl.textContent = "v" + (health.version || "—");
    agentSubEl.textContent    = `v${health.version}  ·  ${health.bus || ""}`;
    aboutDaemonEl.textContent = `${window.location.host}  v${health.version}`;
  }

  // Fetch active model + build the Tools / Models / Security panels.
  const known = [
    ["file_read",  "Read a UTF-8 text file."],
    ["file_write", "Write text to a file (creates parent dirs)."],
    ["list_dir",   "List directory entries (optional glob pattern)."],
    ["bash",       "Run a shell command. PowerShell on Windows, bash on POSIX."],
    ["web_fetch",  "GET a URL and return its body."],
    ["web_search", "DuckDuckGo HTML search, no API key."],
  ];
  if (wsToolsEl) {
    wsToolsEl.textContent = "";
    known.forEach(([name, desc]) => {
      const row = el("div", "ws-tool-entry");
      row.appendChild(el("div", "name", name));
      row.appendChild(el("div", "desc", desc));
      wsToolsEl.appendChild(row);
    });
  }
  if (aboutToolsEl) aboutToolsEl.textContent = known.map(k => k[0]).join(", ");

  // Active model -- daemon doesn't expose it yet; probe would require a
  // /api/v2/status endpoint. For now infer from config.json (can't read)
  // or fall back to "default".
  const activeModel = state.token ? "local-configured" : "no-auth mode";
  setActiveModel(activeModel, "anthropic-compat");
  populateModelMenu(activeModel);

  // Security + config panels (populated from whatever we know).
  const status = await fetchStatus();
  const cfgResp = await fetchConfigSnapshot();
  const tools = status && status.tools ? status.tools : known.map(k => k[0]);
  renderSecurityPanel(health, !!state.token, tools);
  renderConfigPanel(cfgResp && cfgResp.config);
  renderMcpPanel(status ? status.mcp_servers : []);
  renderSkillsPanel();
  renderAgentsPanel(status);
  if (status && status.model) setActiveModel(status.model, "active");
  if (status && status.tools && wsToolsEl) {
    const localDescs = Object.fromEntries(known);
    wsToolsEl.textContent = "";
    status.tools.forEach(name => {
      const row = el("div", "ws-tool-entry");
      row.appendChild(el("div", "name", name));
      row.appendChild(el("div", "desc",
        localDescs[name] || "(from daemon)"));
      wsToolsEl.appendChild(row);
    });
  }

  // hash-state fallback -> localStorage -> new
  const hashSid = (window.location.hash.match(/s=([^&]+)/) || [])[1];
  const initial = hashSid
    ? decodeURIComponent(hashSid)
    : (localStorage.getItem(ACTIVE_KEY) || null);

  if (initial && !state.sessions.includes(initial)) {
    state.sessions.unshift(initial);
    saveSessions(state.sessions);
  }
  if (initial) activateSession(initial);
  else if (state.sessions.length > 0) activateSession(state.sessions[0]);
  else createSession();

  // Welcome-panel visibility: true when the chat has no events, false
  // as soon as the user sends the first message.
  showWelcome(true);

  renderSessionsPanel();
}

init();
