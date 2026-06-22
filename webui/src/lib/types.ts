// Mission Control — 共享类型。WS 协议与事件 schema 以
// xmclaw/core/bus/events.py 为准（Events are the contract）。

export interface Envelope {
  type: string;
  ts?: number;
  id?: string;
  correlation_id?: string;
  payload?: Record<string, unknown>;
  // server-side historical replay marker (app.py agent_ws)
  replayed?: boolean;
}

export type EntryStatus =
  | "complete"
  | "thinking"
  | "streaming"
  | "running"
  | "ok"
  | "error"
  | "done"
  | "pending"
  | "review"
  | "cancelled";

export interface Block {
  type: "text" | "thinking";
  id: string;
  content: string;
}

export interface Question {
  id: string;
  question: string;
  options: Array<{ label: string; value: string }>;
  multi_select: boolean;
  allow_other: boolean;
  tool_call_id: string | null;
}

// 时间线条目 = 旧 UI 的 message。保持旧 shape 的字段语义
// （role/kind/status/correlationId）以便行为对照移植。
export interface Entry {
  id: string;
  role: "user" | "assistant" | "system";
  kind?: "tool_use" | "question" | "worker" | "subagent" | "security" | "fanout";
  severity?: string;
  content: string;
  status: EntryStatus;
  ts: number;
  blocks?: Block[];
  thinking?: string;
  phase?: string | null;
  correlationId?: string;
  // tool_use
  name?: string;
  args?: Record<string, unknown>;
  result?: string | null;
  images?: string[];
  videos?: string[];
  audios?: string[];
  documents?: Array<{ url: string; name: string; mime?: string }>;
  // question
  question?: Question;
  answer?: unknown;
  // proactive
  proactive?: boolean;
  proactiveTrigger?: string;
  // 2026-06-17: Expert Team (P0) — fanout leader card
  goal?: string;
  plan?: Array<{ index: number; role: string; subtask: string; specialist: string }>;
  total?: number;
  synthesis?: string;
  // #3 派发前编辑拆解：status==="review" 时 FanoutCard 进可编辑态，
  // reviewId 用于回传 fanout_review_decision 帧。
  reviewId?: string;
  // worker / subagent（并行子代理执行组）
  workerId?: string;
  taskId?: string;
  subagentIndex?: number | string;
  roleHint?: string;
  promptPreview?: string;
  outputPreview?: string;
  errorPreview?: string;
  hops?: number;
  elapsedSeconds?: number | null;
  // 2026-06-15: long-running tool progress heartbeat
  progress?: number | null;
  progressMessage?: string | null;
  // Minimum wall-clock timestamp until which a "running" tool card
  // must stay visible, preventing sub-300ms flashes.
  minVisibleUntilTs?: number | null;
  // If a FINISHED arrives before minVisibleUntilTs, the finish result
  // is parked here and applied once the window expires.
  pendingFinish?: {
    status: EntryStatus;
    result: string | null;
    error: string | null;
    ok: boolean;
    images: string[];
    videos: string[];
    audios: string[];
    documents: Array<{ url: string; name: string; mime?: string }>;
    attachments?: Record<string, unknown>[];
  } | null;
}

// canvas_artifact_* 事件驱动的实时产物（右栏预览渲染）。
export interface Artifact {
  id: string;
  kind: "mermaid" | "html" | "svg" | "chart" | "table" | string;
  title: string;
  content: string;
  ts: number;
  closed?: boolean;
}

// agent 视觉流：工具结果里的截图（computer-use / browser / camera）。
export interface LiveShot {
  url: string;
  tool: string;
  ts: number;
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  spent_usd: number;
  budget_usd: number;
  last_model: string;
  turns: number;
}

export interface PlanStep {
  id: string;
  index: number;
  status: "pending" | "running" | "done" | "failed";
}

export interface PlanState {
  active: boolean;
  steps: PlanStep[];
  status: "running" | "completed" | "repaired" | "failed" | null;
}

export interface TodoItem {
  content?: string;
  status?: string;
  [k: string]: unknown;
}

export interface ChatState {
  entries: Entry[];
  pendingAssistantId: string | null;
  cancelledTurnIds: Set<string>;
  // 已收到 llm_response 收尾的回合 corr。乱序到达的迟到 llm_chunk
  // 不得把这些已 complete 的气泡打回 streaming（紫光标永久闪）。
  completedTurnIds: Set<string>;
  seenChunks: Record<string, boolean>;
  tokenUsage: TokenUsage | null;
  todos: { items: TodoItem[]; count: number; ts: number } | null;
  plan: PlanState;
  // 实时预览数据源（Phase 10.M2 深度融合）
  artifacts: Artifact[];
  liveShots: LiveShot[];
  workspaceVersion: number;
  workspaceLastPaths: string[];
}

export type ConnectionStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "auth_failed"
  | "superseded";

// /api/v2/tasks 聚合 router 的快照 shape（daemon 侧 TaskSnapshot）。
export interface TaskSnapshot {
  sid: string;
  title: string;
  status: "running" | "awaiting_input" | "done" | "failed" | "chat";
  steps_total: number;
  steps_done: number;
  updated_at: number;
  last_activity: string;
}

export const emptyChat = (): ChatState => ({
  entries: [],
  pendingAssistantId: null,
  cancelledTurnIds: new Set(),
  completedTurnIds: new Set(),
  seenChunks: {},
  tokenUsage: null,
  todos: null,
  plan: { active: false, steps: [], status: null },
  artifacts: [],
  liveShots: [],
  workspaceVersion: 0,
  workspaceLastPaths: [],
});
