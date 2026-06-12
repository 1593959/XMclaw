// Mission Control — 全局 store（zustand）。
// boot 流程移植自旧 app.js：pair token → 选 sid（localStorage）→ WS 连接
// + 历史水化（B-60）+ pending question 恢复（B-99）。

import { create } from "zustand";
import { apiGet, fetchPairingToken, setMediaToken } from "../lib/api";
import { createWsClient, type WsHandle } from "../lib/ws";
import {
  applyEvent,
  appendOptimisticUser,
  appendThinkingAssistant,
  stripInjectedBlocks,
} from "../lib/reducer";
import { emptyChat, type ChatState, type ConnectionStatus, type Entry, type TaskSnapshot } from "../lib/types";

const SID_KEY = "mc.activeSid";
const SIDS_KEY = "mc.sids";

function newSid(): string {
  return "s_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function loadSid(): string {
  try {
    return localStorage.getItem(SID_KEY) || "";
  } catch {
    return "";
  }
}

function persistSid(sid: string, sids: string[]) {
  try {
    localStorage.setItem(SID_KEY, sid);
    localStorage.setItem(SIDS_KEY, JSON.stringify(sids));
  } catch {
    /* private mode */
  }
}

function loadSids(): string[] {
  try {
    const raw = localStorage.getItem(SIDS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

interface HudStatus {
  model?: string;
  memory_facts?: number;
  goals?: number;
  [k: string]: unknown;
}

interface AppState {
  token: string | null;
  authFetched: boolean;
  sid: string;
  sids: string[];
  connection: { status: ConnectionStatus; lastError: string | null; attempt: number };
  chat: ChatState;
  tasks: TaskSnapshot[];
  hud: HudStatus | null;
  draft: string;
  // 四域导航（10.M3）：任务=主视图，其余为驾驶舱仪表域。
  view: "tasks" | "memory" | "skills" | "system";
  setView(v: AppState["view"]): void;
  // 工作区联动：时间线点击 → 右栏聚焦文件；nonce 触发重渲染。
  workspaceFocus: { path: string; nonce: number } | null;
  // 跟随 agent：新 artifact/截图/文件变更自动切右栏对应 tab。
  followAgent: boolean;
  focusWorkspaceFile(path: string): void;
  setFollowAgent(v: boolean): void;
  boot(): Promise<void>;
  sendUser(text: string): void;
  cancelTurn(): void;
  answerQuestion(questionId: string, value: unknown): void;
  setDraft(v: string): void;
  startNewSession(): void;
  resumeSession(sid: string): void;
  refreshTasks(): Promise<void>;
  refreshHud(): Promise<void>;
}

let wsHandle: WsHandle | null = null;

// B-60: 页面加载后从 daemon 持久化 store 取回历史，否则刷新后空屏。
async function hydrateHistory(sid: string, token: string | null, set: (fn: (s: AppState) => Partial<AppState>) => void) {
  if (!sid) return;
  try {
    const url = `/api/v2/sessions/${encodeURIComponent(sid)}` + (token ? `?token=${encodeURIComponent(token)}` : "");
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json().catch(() => null);
    const msgs: Array<Record<string, unknown>> = data?.messages || [];
    if (!msgs.length) return;
    const hydrated: Entry[] = [];
    msgs.forEach((m, i) => {
      const role = m.role as string;
      if (role !== "user" && role !== "assistant") return;
      hydrated.push({
        id: `restore_${i}`,
        role: role as Entry["role"],
        content: stripInjectedBlocks((m.content as string) || ""),
        status: "complete",
        ts: 0,
      });
      // 历史里的 tool_calls 还原为终态工具卡，保持时间线完整。
      const tcs = Array.isArray(m.tool_calls) ? (m.tool_calls as Array<Record<string, unknown>>) : [];
      for (const tc of tcs) {
        hydrated.push({
          id: (tc.id as string) || `restore_${i}_tc`,
          kind: "tool_use",
          role: "assistant",
          content: "",
          name: (tc.name as string) || "tool",
          args: (tc.args as Record<string, unknown>) || {},
          status: "ok",
          result: null,
          ts: 0,
        });
      }
    });
    if (!hydrated.length) return;
    set((s) => {
      const cur = s.chat.entries;
      if (cur.length >= hydrated.length) return {};
      return { chat: { ...s.chat, entries: hydrated.concat(cur) } };
    });
  } catch {
    /* offline / stale token — fail silent */
  }
}

// B-99: daemon 侧仍在 await 的 ask_user_question，重连后把审批卡放回。
async function rehydrateQuestions(token: string | null, set: (fn: (s: AppState) => Partial<AppState>) => void) {
  try {
    const url = "/api/v2/pending_questions" + (token ? `?token=${encodeURIComponent(token)}` : "");
    const r = await fetch(url);
    if (!r.ok) return;
    const data = await r.json();
    const items: Array<Record<string, unknown>> = Array.isArray(data?.items) ? data.items : [];
    if (!items.length) return;
    set((s) => {
      const existing = new Set(
        s.chat.entries.filter((e) => e.kind === "question").map((e) => e.question?.id),
      );
      const fresh: Entry[] = items
        .filter((q) => !existing.has(q.question_id as string))
        .map((q) => ({
          id: `q_${q.question_id}`,
          role: "system" as const,
          kind: "question" as const,
          content: "",
          status: "pending" as const,
          ts: Date.now() / 1000,
          question: {
            id: q.question_id as string,
            question: (q.question as string) || "",
            options: Array.isArray(q.options) ? (q.options as string[]) : [],
            multi_select: !!q.multi_select,
            allow_other: q.allow_other !== false,
            tool_call_id: (q.tool_call_id as string) || null,
          },
        }));
      if (!fresh.length) return {};
      return { chat: { ...s.chat, entries: s.chat.entries.concat(fresh) } };
    });
  } catch {
    /* picker 非关键 */
  }
}

export const useApp = create<AppState>((set, get) => {
  function connectFor(sid: string, token: string | null) {
    wsHandle?.close();
    hydrateHistory(sid, token, set);
    rehydrateQuestions(token, set);
    wsHandle = createWsClient({
      sessionId: sid,
      token,
      onEvent: (envelope) => set((s) => ({ chat: applyEvent(s.chat, envelope) })),
      onStatus: ({ status, error, attempt }) =>
        set({ connection: { status, lastError: error, attempt } }),
    });
  }

  return {
    token: null,
    authFetched: false,
    sid: "",
    sids: [],
    connection: { status: "disconnected", lastError: null, attempt: 0 },
    chat: emptyChat(),
    tasks: [],
    hud: null,
    draft: "",
    view: "tasks",
    setView(v) {
      set({ view: v });
    },
    workspaceFocus: null,
    followAgent: true,

    focusWorkspaceFile(path: string) {
      const cur = get().workspaceFocus;
      set({ workspaceFocus: { path, nonce: (cur?.nonce || 0) + 1 } });
    },

    setFollowAgent(v: boolean) {
      set({ followAgent: v });
    },

    async boot() {
      const auth = await fetchPairingToken();
      setMediaToken(auth.token);
      let sid = loadSid();
      if (!sid) sid = newSid();
      const sidsRaw = loadSids();
      const sids = sidsRaw.includes(sid) ? sidsRaw : [sid, ...sidsRaw];
      persistSid(sid, sids);
      set({ token: auth.token, authFetched: auth.fetched, sid, sids });
      connectFor(sid, auth.token);
      get().refreshTasks();
      get().refreshHud();
    },

    sendUser(text: string) {
      const trimmed = text.trim();
      if (!trimmed || !wsHandle) return;
      const s = get();
      const { id, chat } = appendOptimisticUser(s.chat, trimmed);
      set({ chat: appendThinkingAssistant(chat, id), draft: "" });
      wsHandle.send({ type: "user", content: trimmed, correlation_id: id });
    },

    cancelTurn() {
      const s = get();
      const pending = s.chat.pendingAssistantId;
      if (pending) {
        // B-269: 立刻记入取消集，在途残余 chunk 全部丢弃。
        const cancelled = new Set(s.chat.cancelledTurnIds);
        cancelled.add(pending);
        set({
          chat: {
            ...s.chat,
            cancelledTurnIds: cancelled,
            pendingAssistantId: null,
            entries: s.chat.entries.map((e) =>
              e.id === pending ? { ...e, status: "cancelled" as const, phase: null } : e,
            ),
          },
        });
      }
      wsHandle?.send({ type: "cancel" });
    },

    answerQuestion(questionId: string, value: unknown) {
      wsHandle?.send({ type: "answer_question", question_id: questionId, value });
    },

    setDraft(v: string) {
      set({ draft: v });
    },

    startNewSession() {
      const s = get();
      const sid = newSid();
      const sids = [sid, ...s.sids.filter((x) => x !== sid)];
      persistSid(sid, sids);
      set({ sid, sids, chat: emptyChat() });
      connectFor(sid, s.token);
      get().refreshTasks();
    },

    resumeSession(sid: string) {
      const s = get();
      if (sid === s.sid) return;
      const sids = [sid, ...s.sids.filter((x) => x !== sid)];
      persistSid(sid, sids);
      set({ sid, sids, chat: emptyChat() });
      connectFor(sid, s.token);
    },

    async refreshTasks() {
      const { token } = get();
      if (!token) return;
      try {
        const data = await apiGet<{ tasks: TaskSnapshot[] }>("/api/v2/tasks", token);
        set({ tasks: Array.isArray(data?.tasks) ? data.tasks : [] });
      } catch {
        /* daemon 老版本无此端点 → 任务栏退化为 session 列表 */
      }
    },

    async refreshHud() {
      const { token } = get();
      if (!token) return;
      try {
        const data = await apiGet<{ telemetry?: HudStatus } & HudStatus>("/api/v2/status", token);
        set({ hud: (data?.telemetry as HudStatus) || data || null });
      } catch {
        /* 非关键 */
      }
    },
  };
});
