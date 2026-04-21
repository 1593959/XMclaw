// XMclaw v2 web UI — sidebar-sessions layout, auto-pairing, bubble chat.
// Vanilla ES2020, no framework. Keep event rendering in sync with
// xmclaw/cli/v2_chat.py::format_event — same schema, same tags.

"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// ── persistence keys ─────────────────────────────────────────────
const SESS_KEY  = "xmclaw_v2_sessions";
const ACTIVE_KEY = "xmclaw_v2_active";

// ── DOM refs ─────────────────────────────────────────────────────
const sidebarFooter = $("conn-indicator");
const connTextEl    = $("conn-text");
const buildInfoEl   = $("build-info");
const sessionListEl = $("session-list");
const newSessionBtn = $("new-session");
const agentSubEl    = $("agent-sub");
const sessionIdEl   = $("session-id");
const eventsEl      = $("events");
const sendForm      = $("send-form");
const userInput     = $("user-input");
const costTickerEl  = $("cost-ticker");

// ── state ────────────────────────────────────────────────────────
const state = {
  ws: null,
  token: null,
  sid: null,
  sessions: [],
  thinkingRow: null,
};

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
    else                           createSession();
  } else {
    renderSessionList();
  }
}

function renderSessionList() {
  sessionListEl.innerHTML = "";
  for (const sid of state.sessions) {
    const row = el("button", "session-item" + (sid === state.sid ? " active" : ""));
    row.appendChild(el("span", "session-dot"));
    row.appendChild(el("span", "sess-label", sid));
    const del = el("span", "sess-del", "×");
    del.title = "remove from list";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      removeSession(sid);
    });
    row.appendChild(del);
    row.addEventListener("click", () => {
      if (sid !== state.sid) activateSession(sid);
    });
    sessionListEl.appendChild(row);
  }
}

// ── rendering ────────────────────────────────────────────────────

function setConnState(s, label) {
  sidebarFooter.className = "indicator " + s;
  connTextEl.textContent = label || s;
}

function clearEvents() {
  eventsEl.innerHTML = "";
  state.thinkingRow = null;
  renderEmptyState();
}

function renderEmptyState() {
  const empty = el("div", "events-empty");
  empty.appendChild(el("div", "big-lobster", "🦞"));
  empty.appendChild(el("h2", null, "Ready when you are."));
  empty.appendChild(el("p", null,
    "Type a message below to start. The agent can call tools " +
    "(file_read, file_write, MCP servers), track its own budget, " +
    "and stream every step back to you."));
  const p2 = el("p");
  p2.appendChild(document.createTextNode("Press "));
  p2.appendChild(el("kbd", null, "Enter"));
  p2.appendChild(document.createTextNode(" to send, "));
  p2.appendChild(el("kbd", null, "Shift+Enter"));
  p2.appendChild(document.createTextNode(" for newline."));
  empty.appendChild(p2);
  eventsEl.appendChild(empty);
}

function removeEmptyState() {
  const e = eventsEl.querySelector(".events-empty");
  if (e) e.remove();
}

function renderMarkdownLite(container, text) {
  const fence = /```([a-zA-Z0-9_-]*)?\n([\s\S]*?)```/g;
  let lastIdx = 0;
  let m;
  while ((m = fence.exec(text)) !== null) {
    if (m.index > lastIdx) appendTextWithInline(container, text.slice(lastIdx, m.index));
    container.appendChild(el("pre", "block", m[2]));
    lastIdx = m.index + m[0].length;
  }
  if (lastIdx < text.length) appendTextWithInline(container, text.slice(lastIdx));
}
function appendTextWithInline(container, text) {
  const inline = /`([^`\n]+?)`/g;
  let last = 0;
  let m;
  while ((m = inline.exec(text)) !== null) {
    if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
    container.appendChild(el("code", "inline", m[1]));
    last = m.index + m[0].length;
  }
  if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
}

function renderRow(kind, body) {
  removeEmptyState();
  const row = el("div", "row " + kind);
  if (kind === "user") {
    row.appendChild(body);
    row.appendChild(el("div", "avatar user", "你"));
  } else if (kind === "agent" || kind === "thinking") {
    row.appendChild(el("div", "avatar agent", "🦞"));
    row.appendChild(body);
  } else if (kind === "violation") {
    row.appendChild(body);
  }
  eventsEl.appendChild(row);
  eventsEl.scrollTop = eventsEl.scrollHeight;
  return row;
}

function appendUserMessage(content) {
  const bubble = el("div", "bubble");
  bubble.textContent = content;
  renderRow("user", bubble);
}

function appendAgentMessage(content) {
  if (state.thinkingRow) { state.thinkingRow.remove(); state.thinkingRow = null; }
  const bubble = el("div", "bubble");
  renderMarkdownLite(bubble, content);
  renderRow("agent", bubble);
}

function appendThinking() {
  if (state.thinkingRow) return;
  const bubble = el("div", "bubble");
  bubble.appendChild(document.createTextNode("thinking"));
  const dots = el("span", "thinking-dots");
  dots.appendChild(el("span"));
  dots.appendChild(el("span"));
  dots.appendChild(el("span"));
  bubble.appendChild(dots);
  state.thinkingRow = renderRow("thinking", bubble);
}

function dismissThinking() {
  if (state.thinkingRow) { state.thinkingRow.remove(); state.thinkingRow = null; }
}

function appendToolLine(cls, text) {
  removeEmptyState();
  const line = el("div", "tool-line " + cls);
  line.textContent = text;
  eventsEl.appendChild(line);
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

function appendViolation(msg) {
  dismissThinking();
  const bubble = el("div", "bubble");
  bubble.textContent = "⚠ " + msg;
  renderRow("violation", bubble);
}

function updateCostTicker(p) {
  const spent = (p.spent_usd || 0).toFixed(4);
  const budget = p.budget_usd;
  costTickerEl.textContent = `spent $${spent}${budget ? " / $" + budget : ""}`;
  costTickerEl.classList.add("active");
}

// ── event dispatch ───────────────────────────────────────────────

function renderEvent(evt) {
  const p = evt.payload || {};
  switch (evt.type) {
    case "user_message":
      // echoed locally on send; suppress here to avoid double-render.
      return;

    case "llm_request":
      if ((p.hop ?? 0) === 0) appendThinking();
      return;

    case "llm_response": {
      if (p.ok === false) {
        appendViolation("llm error: " + (p.error || "?"));
        return;
      }
      const tc = p.tool_calls_count | 0;
      const c = (p.content || "").trim();
      if (tc === 0 && c) appendAgentMessage(c);
      return;
    }

    case "tool_call_emitted": {
      dismissThinking();
      const argsStr = JSON.stringify(p.args ?? {});
      const short = argsStr.length > 80 ? argsStr.slice(0, 77) + "..." : argsStr;
      appendToolLine("call", "→ " + (p.name || "?") + "(" + short + ")");
      return;
    }

    case "tool_invocation_finished": {
      const name = p.name || "?";
      if (!p.ok) {
        appendToolLine("error", "← " + name + " failed: " + (p.error || ""));
        return;
      }
      const side = p.expected_side_effects || [];
      if (side.length > 0) {
        appendToolLine("result", "← " + name + " ok, wrote: " + JSON.stringify(side));
        return;
      }
      const r = p.result;
      const sum = typeof r === "string"
        ? (r.length < 80 ? r : r.slice(0, 77) + "...")
        : (typeof r);
      appendToolLine("result", "← " + name + " ok: " + sum);
      return;
    }

    case "anti_req_violation":
      appendViolation(p.message || "unspecified");
      return;

    case "cost_tick":
      updateCostTicker(p);
      return;

    case "session_lifecycle":
      return;

    default:
      console.debug("unrendered event", evt);
  }
}

// ── WS lifecycle ─────────────────────────────────────────────────

function wsUrl() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  let url = `${proto}//${window.location.host}/agent/v2/${state.sid}`;
  if (state.token) url += "?token=" + encodeURIComponent(state.token);
  return url;
}

function connect() {
  if (state.ws) { try { state.ws.close(); } catch (_) {} }
  if (!state.sid) return;
  setConnState("connecting");

  const ws = new WebSocket(wsUrl());
  state.ws = ws;

  ws.addEventListener("open", () => {
    setConnState("connected");
    userInput.focus();
  });
  ws.addEventListener("message", (msg) => {
    try { renderEvent(JSON.parse(msg.data)); } catch (_) {}
  });
  ws.addEventListener("close", (ev) => {
    setConnState("disconnected");
    state.ws = null;
    dismissThinking();
    if (ev.code === 4401) {
      appendViolation(
        "rejected by daemon (401): pairing token wrong or unavailable. " +
        "Restart `xmclaw v2 serve` and refresh this page."
      );
    } else if (ev.code && ev.code !== 1000 && ev.code !== 1001) {
      appendViolation("disconnected (code " + ev.code + "): " + (ev.reason || ""));
    }
  });
}

// ── compose ──────────────────────────────────────────────────────

function send(text) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    appendViolation("not connected");
    return;
  }
  appendUserMessage(text);
  state.ws.send(JSON.stringify({ type: "user", content: text }));
}

sendForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = userInput.value.trim();
  if (!text) return;
  userInput.value = "";
  autoResize();
  send(text);
});

userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendForm.dispatchEvent(new Event("submit"));
  }
});

function autoResize() {
  userInput.style.height = "auto";
  userInput.style.height = Math.min(userInput.scrollHeight, 200) + "px";
}
userInput.addEventListener("input", autoResize);

newSessionBtn.addEventListener("click", createSession);

// ── boot ─────────────────────────────────────────────────────────

async function fetchPairingToken() {
  try {
    const r = await fetch("/api/v2/pair", { credentials: "same-origin" });
    if (!r.ok) return null;
    const body = await r.json();
    return body.token || null;
  } catch (_) {
    return null;
  }
}

async function fetchHealth() {
  try {
    const r = await fetch("/health");
    return await r.json();
  } catch (_) {
    return null;
  }
}

async function boot() {
  const h = await fetchHealth();
  if (h) {
    agentSubEl.textContent = "xmclaw v" + (h.version || "?");
    buildInfoEl.textContent = "daemon v" + (h.version || "?");
  } else {
    agentSubEl.textContent = "daemon offline";
  }

  state.token = await fetchPairingToken();

  state.sessions = loadSessions();
  const hashMatch = window.location.hash.match(/s=([^&]+)/);
  let sid = hashMatch ? decodeURIComponent(hashMatch[1]) : null;
  if (!sid) sid = localStorage.getItem(ACTIVE_KEY) || state.sessions[0];
  if (!sid) { createSession(); return; }
  if (!state.sessions.includes(sid)) state.sessions.unshift(sid);
  saveSessions(state.sessions);
  activateSession(sid);
}

boot();
