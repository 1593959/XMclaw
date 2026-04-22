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
const modelChipEl   = $("model-chip");
const themeBtn      = $("theme-btn");
const themeIconEl   = $("theme-icon");
const toggleWsBtn   = $("toggle-workspace");
const toggleSbBtn   = $("toggle-sidebar");
const sidebarEl     = $("sidebar");
const workspaceEl   = $("workspace");
const appEl         = document.querySelector(".app");
const ringFillEl    = document.querySelector(".ring-fill");
const ringLabelEl   = $("ring-label");
const contextRing   = $("context-ring");
const wsActivityEl  = $("ws-activity");
const wsToolsEl     = $("ws-tools");
const aboutDaemonEl = $("about-daemon");
const aboutModelEl  = $("about-model");
const aboutToolsEl  = $("about-tools");

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
  window.location.hash = "s=" + encodeURIComponent(sid);
  clearEvents();
  state.toolCards.clear();
  state.activity = [];
  renderActivity();
  renderSessionList();
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
    const e = el("div", "ws-empty", "No sessions yet.");
    e.style.padding = "8px 10px";
    sessionListEl.appendChild(e);
    return;
  }
  state.sessions.forEach(sid => {
    const item = el("div", "session-item" + (sid === state.sid ? " active" : ""));
    item.appendChild(el("span", "label", sid));
    const kill = el("button", "kill", "×");
    kill.title = "Remove session";
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

function clearEvents() { eventsEl.textContent = ""; }

function renderEmptyState() {
  const wrap = el("div", "msg system");
  wrap.appendChild(el("div", "av", "ⓘ"));
  const bubble = el("div", "bubble");
  bubble.innerHTML =
    "<strong>Hi. 👋</strong><br>" +
    "I'm XMclaw -- a local agent with filesystem, shell, and web tools. " +
    "Ask me to list your Desktop, summarize a file, run a quick " +
    "<code>dir</code> / <code>git status</code>, or search something up.";
  wrap.appendChild(bubble);
  eventsEl.appendChild(wrap);
}

function removeEmptyState() {
  const empty = eventsEl.querySelector(".msg.system");
  if (empty) empty.remove();
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

function updateToolCard(callId, ok, resultText, errorText) {
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
  body.appendChild(Object.assign(el("div", "block-body"),
    { textContent: ok ? (resultText ?? "(no content)") : (errorText ?? "(no error)") }));

  // Update the most recent matching activity item.
  for (let i = state.activity.length - 1; i >= 0; i--) {
    if (state.activity[i].ok === null) {
      state.activity[i].ok = ok;
      state.activity[i].error = errorText;
      break;
    }
  }
  renderActivity();
}

// ── workspace (right pane) ───────────────────────────────────────

function renderActivity() {
  wsActivityEl.textContent = "";
  if (state.activity.length === 0) {
    wsActivityEl.appendChild(el("div", "ws-empty", "No tool calls yet."));
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

function setupWsTabs() {
  document.querySelectorAll(".ws-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".ws-tab").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".ws-panel").forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      const panel = document.querySelector(`.ws-panel[data-panel="${tab.dataset.tab}"]`);
      if (panel) panel.classList.add("active");
    });
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

// ── context ring ─────────────────────────────────────────────────

function updateContextRing(tokens) {
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
}

function updateCostTicker(p) {
  const spent  = (p.spent_usd  || 0).toFixed(4);
  const budget = p.budget_usd;
  costTickerEl.textContent =
    `cost $${spent}${budget ? " / $" + budget : ""}`;
}

// ── event dispatch ───────────────────────────────────────────────

function renderEvent(evt) {
  const p = evt.payload || {};
  switch (evt.type) {
    case "user_message":
      // Already echoed locally on send -- suppress to avoid double rendering.
      return;

    case "llm_request":
      if ((p.hop ?? 0) === 0) appendThinking();
      return;

    case "llm_response": {
      if (p.ok === false) {
        appendViolation("llm error: " + (p.error || "?"));
        return;
      }
      // Update context ring as tokens arrive.
      updateContextRing((p.prompt_tokens || 0) + (p.completion_tokens || 0));
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
      const resultTxt = typeof p.result === "string" ?
        p.result : JSON.stringify(p.result, null, 2);
      updateToolCard(p.call_id, p.ok !== false, resultTxt, p.error);
      return;
    }

    case "cost_tick":
      updateCostTicker(p);
      return;

    case "anti_req_violation":
      appendViolation(p.message || "anti-req violation");
      return;

    case "session_lifecycle":
      return;

    default:
      return;
  }
}

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
  if (status === "connected") {
    connEl.classList.add("conn-up");
    connTextEl.textContent = "connected";
  } else {
    connEl.classList.add("conn-down");
    connTextEl.textContent = status;
  }
}

function sendUser() {
  const val = userInput.value.trim();
  if (!val || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  appendUser(val);
  state.ws.send(JSON.stringify({ type: "user", content: val }));
  userInput.value = "";
  userInput.style.height = "auto";
}

// ── init ─────────────────────────────────────────────────────────

function autoGrow() {
  userInput.style.height = "auto";
  userInput.style.height = Math.min(userInput.scrollHeight, 180) + "px";
}

async function init() {
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  setupWsTabs();

  themeBtn.addEventListener("click", cycleTheme);
  toggleWsBtn.addEventListener("click", toggleWorkspace);
  if (toggleSbBtn) toggleSbBtn.addEventListener("click", toggleSidebar);
  newSessionBtn.addEventListener("click", createSession);

  sendForm.addEventListener("submit", (ev) => { ev.preventDefault(); sendUser(); });
  userInput.addEventListener("input", autoGrow);
  userInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault(); sendUser();
    }
  });

  state.sessions = loadSessions();
  state.token = await fetchPairingToken();
  const health = await fetchHealth();
  if (health) {
    buildInfoEl.textContent = health.version || "—";
    agentSubEl.textContent  = `v${health.version}  ·  ${health.bus || ""}`;
    aboutDaemonEl.textContent = `${window.location.host} (v${health.version})`;
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

  renderEmptyState();

  // Populate the Tools + About panels once we know the daemon model.
  try {
    // The daemon currently doesn't expose /api/v2/tools; fall back to the
    // static list we know the default BuiltinTools ships with.
    const known = [
      ["file_read",  "Read a UTF-8 text file."],
      ["file_write", "Write text to a file (creates parent dirs)."],
      ["list_dir",   "List directory entries (optional glob pattern)."],
      ["bash",       "Run a shell command. PowerShell on Windows, bash on POSIX."],
      ["web_fetch",  "GET a URL and return its body."],
      ["web_search", "DuckDuckGo HTML search, no API key."],
    ];
    wsToolsEl.textContent = "";
    known.forEach(([name, desc]) => {
      const row = el("div", "ws-tool-entry");
      row.appendChild(el("div", "name", name));
      row.appendChild(el("div", "desc", desc));
      wsToolsEl.appendChild(row);
    });
    aboutToolsEl.textContent = known.map(k => k[0]).join(", ");
  } catch (_) { /* ignore */ }
}

init();
