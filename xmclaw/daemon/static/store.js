// XMclaw — minimal pub/sub store
//
// Designed for the app scale we expect (single-page, ~12 routes, one WS).
// No reducers, no middleware — just a `{ state, setState, subscribe }`
// primitive we can grow into a Zustand-style store once the Chat page lands
// in Phase 1.
//
// API contract:
//   createStore(initial)            → { getState, setState, subscribe }
//   getState()                      → frozen snapshot of current state
//   setState(patchOrFn)             → merge a patch OR apply a functional
//                                     updater; triggers all subscribers
//   subscribe(listener)             → () => unsubscribe
//
// The store exports `app` — the top-level store instance consumed by app.js.
// Every new slice (sessions, skills, …) appends onto the initial state here.

function freeze(obj) {
  // Object.freeze is shallow, which is what we want: deep freezing turns
  // trivial .push / .splice into throw-spaghetti inside subscribers.
  return Object.freeze(obj);
}

export function createStore(initial) {
  let state = freeze({ ...initial });
  const listeners = new Set();

  function getState() {
    return state;
  }

  function setState(patch) {
    const prev = state;
    const next =
      typeof patch === "function"
        ? { ...state, ...patch(state) }
        : { ...state, ...patch };
    state = freeze(next);
    for (const entry of listeners) {
      try {
        // P0 selector support (audit 2026-06-11): each subscription
        // may carry an optional selector function. When a selector
        // is present the listener only fires when the selected
        // slice reference has actually changed. This avoids the
        // full-tree re-render that was happening on every keystroke.
        if (typeof entry.selector === "function") {
          if (entry.selector(prev) !== entry.selector(state)) {
            entry.listener(state);
          }
        } else {
          entry.listener(state);
        }
      } catch (err) {
        // Never let one subscriber blow up the store.
        console.error("[xmc] store listener threw", err);
      }
    }
  }

  function subscribe(listener, selector) {
    const entry = { listener, selector: selector || null };
    listeners.add(entry);
    return () => listeners.delete(entry);
  }

  return { getState, setState, subscribe };
}

// ── Persistence helpers ────────────────────────────────────────────────
//
// Phase 1 keeps session ids in localStorage so a refresh doesn't drop the
// active conversation. The daemon already replays history server-side via
// SESSION_LIFECYCLE + replayed events, so we only need to remember the sid.

const ACTIVE_SID_KEY = "xmc.active_sid";
const SID_LIST_KEY = "xmc.sids";
const ACTIVE_AGENT_ID_KEY = "xmc.active_agent_id";  // B-133

function readActiveSid() {
  try {
    return localStorage.getItem(ACTIVE_SID_KEY) || null;
  } catch (_) {
    return null;
  }
}

function readActiveAgentId() {
  try {
    return localStorage.getItem(ACTIVE_AGENT_ID_KEY) || "main";
  } catch (_) {
    return "main";
  }
}

export function persistActiveAgentId(agentId) {
  try {
    if (agentId && agentId !== "main") {
      localStorage.setItem(ACTIVE_AGENT_ID_KEY, agentId);
    } else {
      localStorage.removeItem(ACTIVE_AGENT_ID_KEY);
    }
  } catch (_) {
    /* skip */
  }
}

function readSidList() {
  try {
    const raw = localStorage.getItem(SID_LIST_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((s) => typeof s === "string") : [];
  } catch (_) {
    return [];
  }
}

export function persistActiveSid(sid) {
  try {
    if (sid) localStorage.setItem(ACTIVE_SID_KEY, sid);
    else localStorage.removeItem(ACTIVE_SID_KEY);
  } catch (_) {
    /* private mode / quota — skip silently */
  }
}

export function persistSidList(list) {
  try {
    localStorage.setItem(SID_LIST_KEY, JSON.stringify(list));
  } catch (_) {
    /* skip */
  }
}

export function newSid() {
  // 8 hex chars after the prefix is plenty for human-readable sids and
  // matches what the legacy UI used.
  return "chat-" + Math.random().toString(16).slice(2, 10);
}

// ── App-wide store ────────────────────────────────────────────────────

export const app = createStore({
  // Router slice (router.js writes here).
  route: { path: "/chat", params: {} },

  // Session slice — populated from localStorage on boot, then by user
  // creating new sessions or switching via the sidebar.
  // B-133: activeAgentId routes the WS to a specific sub-agent. Default
  // 'main' = primary config-built agent.
  session: {
    activeSid: readActiveSid(),
    sids: readSidList(),
    lifecycle: "idle",
    activeAgentId: readActiveAgentId(),
    agents: [],  // populated from /api/v2/agents — for the picker
  },

  // Connection slice (WS lifecycle).
  //   status: "disconnected" | "connecting" | "connected" | "reconnecting" | "auth_failed"
  //   lastError: human-readable last failure (for status bar tooltip)
  //   reconnectAttempt: monotonically increasing int (UI uses this to render "retry n/∞")
  connection: {
    status: "disconnected",
    lastError: null,
    reconnectAttempt: 0,
  },

  // Auth slice — pairing token cached after the first /api/v2/pair call.
  auth: { token: null, fetched: false },

  // Chat slice — flat array of messages, each with a stable id so the
  // streaming reducer can append tokens without reflowing the whole list.
  // Shape per message:
  //   {
  //     id: string,              // event correlation_id or generated
  //     role: "user"|"assistant"|"tool"|"system",
  //     content: string,         // accumulated text (LLM_CHUNK appends)
  //     status: "streaming"|"complete"|"error",
  //     ts: number,
  //     toolCalls?: [{ id, name, args, status, result }],
  //     ultrathink?: boolean,
  //   }
  chat: {
    messages: [],
    pendingAssistantId: null,    // id of the in-flight assistant turn, or null
    composerDraft: "",
    // B-MULTIMODAL-UI: image attachments staged in the composer.
    // Each entry: { url: data-URI or /api/v2/media/... , name: string }.
    // Cleared on send + included in the user_message WS frame.
    composerImages: [],
    planMode: false,             // Plan vs Act
    // Wave-32+ OutputStyles: which preset prompt fragment to splice
    // into the system prompt. null / "default" → no extra style.
    // Server-validated against the registry (built-in + on-disk).
    outputStyle: "default",
    ultrathink: false,
    // Multi-model: which configured LLM profile this session routes to.
    // null → daemon picks the registry default (legacy single-LLM block).
    llmProfileId: null,
    // B-269: ids of turns the user clicked Stop on. Reducer's llm_chunk
    // / llm_thinking_chunk handlers consult this set and silently drop
    // late-arriving deltas for cancelled turns. Without it the
    // provider's already-buffered chunks keep appending after Stop is
    // clicked, defeating the user's intent.
    cancelledTurnIds: new Set(),

    // ── Agent UI panels (Phase 1: Agent-style redesign) ──────────
    // Plan steps surfaced from plan mode or LLM plan-first output.
    planSteps: [],
    activePlanStep: null,
    planGenerated: false,
    // Flat tool execution log — extracted from message bubbles into
    // a dedicated real-time feed. Entries: { id, toolName, args,
    // status: "pending"|"running"|"done"|"error", startedAt,
    // durationMs, result, error, correlationId }.
    toolExecutionLog: [],
    toolCallCount: 0,
    // Thinking/reasoning segments, built from llm_thinking_chunk events.
    // Each segment: { id, content, startedAt, endedAt }.
    thinkingSegments: [],
    // Artifacts produced by the agent (canvas / files).
    artifacts: [],
    // Memory operations surfaced as a recall indicator.
    memoryOps: [],
    // Agent session metrics.
    sessionElapsed: 0,
    currentHop: 0,
    tokenUsage: { promptTokens: 0, completionTokens: 0, totalTokens: 0, costUsd: 0 },
    // Panel UI preferences.
    referenceMessageCount: 3,
    thinkingCollapsed: false,
    memoryRecallExpanded: false,
    showAllToolCalls: false,
  },

  // ── Workspace slice (files + recent ops) ─────────────────────
  workspace: {
    files: [],
    currentDirectory: "~",
    recentOperations: [],
  },

  // UI prefs (Phase 5 settings page will bind here).
  ui: {
    theme: "dark",
    density: "comfortable",
    locale: "zh-CN",
  },

  // Bootstrap diag (populated from window.__xmc.bootstrapSource).
  bootstrap: {
    source: window.__xmc ? window.__xmc.bootstrapSource : "unknown",
  },
});
