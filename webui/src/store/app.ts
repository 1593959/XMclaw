// Mission Control — 全局 store（zustand）。
// boot 流程移植自旧 app.js：pair token → 选 sid（localStorage）→ WS 连接
// + 历史水化（B-60）+ pending question 恢复（B-99）。

import { create } from "zustand";
import { apiDelete, apiGet, fetchPairingToken, setMediaToken } from "../lib/api";
import { createWsClient, type WsHandle } from "../lib/ws";
import {
  applyEvent,
  appendOptimisticUser,
  appendThinkingAssistant,
  normalizeQuestionOptions,
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

export interface LlmProfile {
  id: string;
  label: string;
  provider: string;
  model: string;
  is_default: boolean;
}

export interface Attachment {
  dataUrl: string;
  name: string;
  mime: string;
  // image → 走 WS 帧 images 字段（vision）；file → files 字段（落盘+工具）。
  channel: "image" | "file";
}

export interface LightboxState {
  url: string;
  kind: "image" | "video";
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
  // Composer 选项（与旧 UI 的 WS 帧字段一致：plan_mode / ultrathink /
  // llm_profile_id，missing = 默认）。
  planMode: boolean;
  ultrathink: boolean;
  llmProfileId: string;
  profiles: LlmProfile[];
  togglePlan(): void;
  toggleUltrathink(): void;
  setLlmProfile(id: string): void;
  // 多模态输入：粘贴/拖拽/选择的附件（随用户帧 images 字段发送）。
  attachments: Attachment[];
  addAttachments(files: FileList | File[]): void;
  removeAttachment(idx: number): void;
  // 当前页媒体放大查看（不跳新页面）。
  lightbox: LightboxState | null;
  openLightbox(url: string, kind?: "image" | "video"): void;
  closeLightbox(): void;
  // 四域导航（10.M3）：任务=主视图，其余为驾驶舱仪表域（模型配置在系统域子标签）。
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
  deleteSession(sid: string): Promise<void>;
  // power-user 动作（slash 命令 + 消息操作）
  retryLast(): void;
  undoLast(): void;
  clearChat(): void;
  toast: { text: string; tone: "info" | "ok" | "err" } | null;
  showToast(text: string, tone?: "info" | "ok" | "err"): void;
  refreshTasks(): Promise<void>;
  refreshHud(): Promise<void>;
  refreshProfiles(): Promise<void>;
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
            options: normalizeQuestionOptions(q.options),
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
    planMode: false,
    ultrathink: false,
    llmProfileId: "",
    profiles: [],
    togglePlan() {
      set((s) => ({ planMode: !s.planMode }));
    },
    toggleUltrathink() {
      set((s) => ({ ultrathink: !s.ultrathink }));
    },
    setLlmProfile(id: string) {
      set({ llmProfileId: id });
    },

    attachments: [],
    addAttachments(files: FileList | File[]) {
      const list = Array.from(files);
      for (const f of list) {
        const isImage = f.type.startsWith("image/");
        // 图片走 vision 通道(8MB), 其余走文件落盘通道(48MB, 后端
        // ws_file_intake 保存 + agent 用 file_read/voice_transcribe 处理)。
        const cap = isImage ? 8 * 1024 * 1024 : 48 * 1024 * 1024;
        if (f.size > cap) {
          console.warn(`[mc] 附件超过上限(${isImage ? "8" : "48"}MB):`, f.name);
          continue;
        }
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = String(reader.result || "");
          if (!dataUrl.startsWith("data:")) return;
          set((s) => ({
            attachments: [
              ...s.attachments,
              {
                dataUrl,
                name: f.name || (isImage ? "image" : "file"),
                mime: f.type || "application/octet-stream",
                channel: isImage ? ("image" as const) : ("file" as const),
              },
            ].slice(0, 8),
          }));
        };
        reader.readAsDataURL(f);
      }
    },
    removeAttachment(idx: number) {
      set((s) => ({ attachments: s.attachments.filter((_, i) => i !== idx) }));
    },

    lightbox: null,
    openLightbox(url: string, kind: "image" | "video" = "image") {
      set({ lightbox: { url, kind } });
    },
    closeLightbox() {
      set({ lightbox: null });
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
      // 模型 profile 列表（Composer 切换器；失败不致命）。
      try {
        const data = await apiGet<{ profiles?: LlmProfile[] }>("/api/v2/llm/profiles", auth.token);
        set({ profiles: Array.isArray(data?.profiles) ? data.profiles : [] });
      } catch {
        /* 列表为空 → 切换器隐藏 */
      }
    },

    sendUser(text: string) {
      const trimmed = text.trim();
      const s = get();
      const images = s.attachments.filter((a) => a.channel === "image").map((a) => a.dataUrl);
      const files = s.attachments
        .filter((a) => a.channel === "file")
        .map((a) => ({ name: a.name, mime: a.mime, data_url: a.dataUrl }));
      if ((!trimmed && images.length === 0 && files.length === 0) || !wsHandle) return;
      // 乐观回显图片缩略；文件以占位让用户知道已附带。
      const { id, chat } = appendOptimisticUser(s.chat, trimmed, images);
      set({ chat: appendThinkingAssistant(chat, id), draft: "", attachments: [] });
      wsHandle.send({
        type: "user",
        content: trimmed,
        correlation_id: id,
        images: images.length > 0 ? images : undefined,
        files: files.length > 0 ? files : undefined,
        // missing = 默认（与后端约定一致），只在非默认时带字段。
        plan_mode: s.planMode || undefined,
        ultrathink: s.ultrathink || undefined,
        llm_profile_id: s.llmProfileId || undefined,
      });
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

    toast: null,
    showToast(text: string, tone: "info" | "ok" | "err" = "info") {
      set({ toast: { text, tone } });
      setTimeout(() => {
        // 仅当还是同一条 toast 时清除（避免清掉后来的）。
        if (get().toast?.text === text) set({ toast: null });
      }, 2600);
    },

    retryLast() {
      // /retry：把最后一条用户消息回填输入框，供 review-and-resend。
      const s = get();
      for (let i = s.chat.entries.length - 1; i >= 0; i--) {
        const e = s.chat.entries[i];
        if (e.role === "user" && !e.kind) {
          set({ draft: e.content });
          get().showToast("已回填上一条指令，可编辑后重发", "info");
          return;
        }
      }
      get().showToast("没有可重试的指令", "err");
    },

    undoLast() {
      // /undo：剥掉本地最后一组 user+assistant，并请 daemon 同步弹出历史。
      if (!wsHandle) {
        get().showToast("WS 未连接，撤销失败", "err");
        return;
      }
      set((s) => {
        const es = s.chat.entries.slice();
        // 从尾部删到（含）最后一条 user 消息为止。
        let cut = es.length;
        for (let i = es.length - 1; i >= 0; i--) {
          if (es[i].role === "user" && !es[i].kind) {
            cut = i;
            break;
          }
        }
        return { chat: { ...s.chat, entries: es.slice(0, cut), pendingAssistantId: null } };
      });
      wsHandle.send({ type: "undo" });
      get().showToast("已撤销上一轮", "ok");
    },

    clearChat() {
      // /clear：仅清本地面板，daemon 历史保留。
      set((s) => ({ chat: { ...emptyChat(), tokenUsage: s.chat.tokenUsage } }));
      get().showToast("已清空本地面板（daemon 历史保留）", "info");
    },

    resumeSession(sid: string) {
      const s = get();
      if (sid === s.sid) return;
      const sids = [sid, ...s.sids.filter((x) => x !== sid)];
      persistSid(sid, sids);
      set({ sid, sids, chat: emptyChat() });
      connectFor(sid, s.token);
    },

    async deleteSession(sid: string) {
      const s = get();
      const remaining = s.sids.filter((x) => x !== sid);
      // 乐观从列表移除（任务栏 + 本地 sid 列表）。
      set({
        sids: remaining,
        tasks: s.tasks.filter((t) => t.sid !== sid),
      });
      try {
        await apiDelete(`/api/v2/sessions/${encodeURIComponent(sid)}`, s.token);
        get().showToast("已删除会话", "ok");
      } catch {
        get().showToast("删除失败（daemon 未响应）", "err");
      }
      // 删的是当前会话 → 切到下一个或新建。
      if (sid === s.sid) {
        if (remaining.length > 0) {
          get().resumeSession(remaining[0]);
        } else {
          get().startNewSession();
        }
      }
      get().refreshTasks();
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

    async refreshProfiles() {
      const { token } = get();
      if (!token) return;
      try {
        const data = await apiGet<{ profiles?: LlmProfile[] }>("/api/v2/llm/profiles", token);
        set({ profiles: Array.isArray(data?.profiles) ? data.profiles : [] });
      } catch {
        /* non-critical */
      }
    },
  };
});
