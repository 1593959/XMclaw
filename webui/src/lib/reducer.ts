// Mission Control — 事件 reducer。语义移植自旧 static/lib/chat_reducer.js
// + chat_reducer_streaming.js（B-89 弃流收尾 / B-232 call_id 键名 /
// B-267 工具事件乱序竞态 / B-269 取消回合守卫 / seq 去重 / 多 hop 不清
// pending / 截断 finalText 不覆盖更长流式文本），并新增 Mission Control
// 的 plan_* 步骤条状态。纯函数，零 DOM。

import type {
  Artifact,
  Block,
  ChatState,
  Entry,
  Envelope,
  PlanState,
  TodoItem,
  TokenUsage,
} from "./types";
import { resolveMediaUrl } from "./api";

function genId(): string {
  return "m_" + Math.random().toString(16).slice(2, 10);
}

function upsertById(entries: Entry[], id: string, patch: (e: Entry) => Entry): Entry[] {
  const idx = entries.findIndex((e) => e.id === id);
  if (idx === -1) return entries;
  const next = entries.slice();
  next[idx] = patch(next[idx]);
  return next;
}

// B-89: 新 assistant 回合开始时，把之前仍在 thinking/streaming 的
// 弃流条目收尾，避免"正在调用 LLM · Ns"永远转下去。
function finalizeAbandoned(entries: Entry[], keepId: string): Entry[] {
  let touched = false;
  const next = entries.map((e) => {
    if (e.id === keepId || e.role !== "assistant" || e.kind) return e;
    if (e.status !== "thinking" && e.status !== "streaming") return e;
    touched = true;
    return { ...e, status: "complete" as const, phase: null };
  });
  return touched ? next : entries;
}

// 漏掉 finished 事件的 running 工具卡：新一轮开始时收尾成终态。
function finalizeStaleTools(entries: Entry[], keepCorr: string | null): Entry[] {
  let touched = false;
  const next = entries.map((e) => {
    if (e.kind !== "tool_use" || e.status !== "running") return e;
    if (keepCorr && e.correlationId === keepCorr) return e;
    touched = true;
    return { ...e, status: (e.result != null ? "ok" : "done") as Entry["status"] };
  });
  return touched ? next : entries;
}

function appendBlock(blocks: Block[] | undefined, type: "text" | "thinking", mid: string, delta: string): Block[] {
  const arr = blocks ? [...blocks] : [];
  const last = arr[arr.length - 1];
  if (last && last.type === type) {
    arr[arr.length - 1] = { ...last, content: (last.content || "") + delta };
  } else {
    arr.push({ type, id: `${mid}:${type[0]}${arr.length}`, content: delta });
  }
  return arr;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

// daemon 会把上下文块搭在 user 消息尾部持久化（session-workspace 提示 /
// memory 注入 / output_schema 等，见 agent_loop.py F1 注释）。这些是
// 给 LLM 看的，不是用户打的字 — 展示层剥掉。标签名单与
// routers/tasks.py 的 _INJECTED_BLOCKS 保持同步。
const INJECTED_BLOCKS =
  /<(session-workspace|output_schema|memory-[\w-]+|recalled-memory-files|recalled|curriculum-[\w-]+|user-uploaded-files)>[\s\S]*?<\/\1>/g;
export function stripInjectedBlocks(text: string): string {
  return text.replace(INJECTED_BLOCKS, "").trim();
}
const mediaList = (v: unknown): string[] =>
  Array.isArray(v) ? (v as string[]).map(resolveMediaUrl) : [];

export function applyEvent(chat: ChatState, envelope: Envelope): ChatState {
  if (!envelope || typeof envelope !== "object") return chat;
  const t = envelope.type;
  const payload = (envelope.payload || {}) as Record<string, unknown>;
  const ts = envelope.ts || Date.now() / 1000;
  const corr = envelope.correlation_id || envelope.id || genId();

  switch (t) {
    case "user_message": {
      const id = corr;
      const serverImages = mediaList(payload.images);
      // 新用户消息 = 上一轮彻底结束 → 收尾所有残留 running 工具卡。
      const swept = finalizeStaleTools(chat.entries, null);
      if (swept.some((e) => e.id === id)) {
        return {
          ...chat,
          entries: upsertById(swept, id, (e) => ({
            ...e,
            content: stripInjectedBlocks(str(payload.content)) || e.content,
            status: "complete",
            ts,
            images: serverImages.length > 0 ? serverImages : e.images || [],
          })),
        };
      }
      return {
        ...chat,
        entries: swept.concat({
          id,
          role: "user",
          content: stripInjectedBlocks(str(payload.content)),
          status: "complete",
          ts,
          images: serverImages,
        }),
      };
    }

    case "llm_request": {
      const id = corr;
      const cleaned = finalizeStaleTools(finalizeAbandoned(chat.entries, id), id);
      if (!cleaned.some((e) => e.id === id)) {
        return {
          ...chat,
          pendingAssistantId: id,
          entries: cleaned.concat({
            id,
            role: "assistant",
            content: "",
            status: "thinking",
            phase: "calling_llm",
            ts,
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        entries: upsertById(cleaned, id, (e) => ({ ...e, phase: "calling_llm" })),
      };
    }

    case "llm_chunk": {
      const id = corr;
      // B-269: 用户已点 Stop 的回合，丢弃在途残余 chunk。
      if (chat.cancelledTurnIds.has(id)) return chat;
      // seq 去重：WS 重连/bus 重发可能送达同一 chunk 两次。
      const seq = payload.seq;
      const chunkKey = seq != null ? `${id}:${seq}` : null;
      if (chunkKey && chat.seenChunks[chunkKey]) return chat;
      const seen = chunkKey ? { ...chat.seenChunks, [chunkKey]: true } : chat.seenChunks;
      const delta = str(payload.delta) || str(payload.content);
      const cleaned = finalizeAbandoned(chat.entries, id);
      if (!cleaned.some((e) => e.id === id)) {
        return {
          ...chat,
          seenChunks: seen,
          pendingAssistantId: id,
          entries: cleaned.concat({
            id,
            role: "assistant",
            content: delta,
            status: "streaming",
            ts,
            blocks: [{ type: "text", id: `${id}:t0`, content: delta }],
          }),
        };
      }
      return {
        ...chat,
        seenChunks: seen,
        pendingAssistantId: id,
        entries: upsertById(cleaned, id, (e) => ({
          ...e,
          content: e.content + delta,
          status: "streaming",
          blocks: appendBlock(e.blocks, "text", id, delta),
        })),
      };
    }

    case "llm_thinking_chunk": {
      const id = corr;
      if (chat.cancelledTurnIds.has(id)) return chat;
      const seq = payload.seq;
      const key = seq != null ? `${id}:k:${seq}` : null;
      if (key && chat.seenChunks[key]) return chat;
      const seen = key ? { ...chat.seenChunks, [key]: true } : chat.seenChunks;
      const delta = str(payload.delta);
      if (!delta) return chat;
      const cleaned = finalizeAbandoned(chat.entries, id);
      if (!cleaned.some((e) => e.id === id)) {
        return {
          ...chat,
          seenChunks: seen,
          pendingAssistantId: id,
          entries: cleaned.concat({
            id,
            role: "assistant",
            content: "",
            thinking: delta,
            status: "thinking",
            phase: "calling_llm",
            ts,
            blocks: [{ type: "thinking", id: `${id}:k0`, content: delta }],
          }),
        };
      }
      return {
        ...chat,
        seenChunks: seen,
        pendingAssistantId: id,
        entries: upsertById(cleaned, id, (e) => ({
          ...e,
          thinking: (e.thinking || "") + delta,
          blocks: appendBlock(e.blocks, "thinking", id, delta),
        })),
      };
    }

    case "llm_response": {
      const id = corr;
      // 已取消的回合：迟到的终止事件不得把 cancelled 改写成 complete。
      if (chat.cancelledTurnIds.has(id)) return chat;
      const finalText = str(payload.content) || str(payload.text);
      const ok = payload.ok !== false;
      const finalStatus: Entry["status"] = ok ? "complete" : "error";
      const errBody = !ok ? `LLM 调用失败：${payload.error || "未知"}` : "";
      // 多 hop 中段（还有工具要跑）不清 pending，避免按钮 Stop/Send 闪烁。
      const moreHopsComing = ok && ((payload.tool_calls_count as number) || 0) > 0;
      const nextPending = moreHopsComing ? id : null;
      if (!chat.entries.some((e) => e.id === id)) {
        return {
          ...chat,
          pendingAssistantId: nextPending,
          entries: chat.entries.concat({
            id,
            role: "assistant",
            content: finalText || errBody,
            status: moreHopsComing ? "thinking" : finalStatus,
            phase: moreHopsComing ? "calling_llm" : null,
            ts,
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: nextPending,
        entries: upsertById(chat.entries, id, (e) => ({
          ...e,
          // finalText 截断时保留更长的流式文本（防止渲染好的回复
          // 在 llm_response 落地瞬间塌成碎片）。
          content: (() => {
            const streamed = e.content || "";
            if (!finalText) return streamed || errBody;
            return finalText.length >= streamed.length ? finalText : streamed;
          })(),
          status: moreHopsComing ? "thinking" : finalStatus,
          phase: moreHopsComing ? "calling_llm" : null,
          ts,
        })),
      };
    }

    case "tool_call_emitted": {
      const toolName = str(payload.name) || str(payload.tool_name) || "tool";
      // ask_user_question 有专属 UI（审批卡），不再出冗余工具卡。
      if (toolName === "ask_user_question") return chat;
      // B-232: daemon 的键名是 call_id（snake_case）。
      const callId = str(payload.call_id) || str(payload.tool_call_id) || str(payload.id) || genId();
      // B-267: finished 先到的竞态——已有终态条目时只补元数据，不回退状态。
      if (chat.entries.some((e) => e.kind === "tool_use" && e.id === callId)) {
        return {
          ...chat,
          entries: upsertById(chat.entries, callId, (e) => ({
            ...e,
            name: e.name && e.name !== "tool" ? e.name : toolName,
            args:
              e.args && Object.keys(e.args).length > 0
                ? e.args
                : ((payload.args || payload.arguments || {}) as Record<string, unknown>),
            correlationId: e.correlationId || corr,
          })),
        };
      }
      const cleaned = finalizeAbandoned(chat.entries, corr);
      return {
        ...chat,
        entries: cleaned.concat({
          id: callId,
          kind: "tool_use",
          role: "assistant",
          correlationId: corr,
          name: toolName,
          args: (payload.args || payload.arguments || {}) as Record<string, unknown>,
          content: "",
          status: "running",
          result: null,
          ts,
        }),
      };
    }

    case "tool_invocation_started": {
      const callId = str(payload.call_id) || str(payload.id);
      if (!chat.entries.some((e) => e.kind === "tool_use" && e.id === callId)) return chat;
      return {
        ...chat,
        entries: upsertById(chat.entries, callId, (e) => ({ ...e, status: "running" })),
      };
    }

    case "tool_invocation_finished": {
      const callId = str(payload.call_id) || str(payload.tool_call_id) || str(payload.id);
      const status: Entry["status"] = payload.error ? "error" : "ok";
      const result = payload.error
        ? String(payload.error)
        : typeof payload.result === "string"
          ? payload.result
          : JSON.stringify(payload.result || {}, null, 2);
      const images = mediaList(payload.images);
      const videos = mediaList(payload.videos);
      const audios = mediaList(payload.audios);
      if (!chat.entries.some((e) => e.kind === "tool_use" && e.id === callId)) {
        // B-267: finished 先于 emitted 到达 → 直接合成终态卡，
        // 之后 emitted 到达时走上面的补元数据路径，零数据丢失。
        return {
          ...chat,
          liveShots: images.length
            ? [
                ...images.map((url) => ({
                  url,
                  tool: str(payload.name) || str(payload.tool_name) || "tool",
                  ts,
                })),
                ...chat.liveShots,
              ].slice(0, 12)
            : chat.liveShots,
          entries: chat.entries.concat({
            id: callId,
            kind: "tool_use",
            role: "assistant",
            correlationId: corr,
            name: str(payload.name) || str(payload.tool_name) || "tool",
            args: (payload.args || payload.arguments || {}) as Record<string, unknown>,
            content: "",
            status,
            result,
            images,
            videos,
            audios,
            ts,
          }),
        };
      }
      // 实时预览：截图类结果进 liveShots 流（封顶 12 张，新的在前）。
      const toolName = str(payload.name) || str(payload.tool_name) || "tool";
      const liveShots = images.length
        ? [...images.map((url) => ({ url, tool: toolName, ts })), ...chat.liveShots].slice(0, 12)
        : chat.liveShots;
      return {
        ...chat,
        liveShots,
        entries: upsertById(chat.entries, callId, (e) => ({
          ...e,
          status,
          result,
          images,
          videos,
          audios,
        })),
      };
    }

    case "agent_asked_question": {
      const qid = str(payload.question_id);
      if (!qid) return chat;
      if (chat.entries.some((e) => e.kind === "question" && e.question?.id === qid)) return chat;
      return {
        ...chat,
        entries: chat.entries.concat({
          id: `q_${qid}`,
          role: "system",
          kind: "question",
          content: "",
          status: "pending",
          ts,
          question: {
            id: qid,
            question: str(payload.question),
            options: (Array.isArray(payload.options) ? (payload.options as Array<Record<string, unknown>>) : [])
              .map((o) => {
                const label = str(o.label || o.name || "");
                const value = str(o.value || o);
                return label ? { label, value } : null;
              })
              .filter(Boolean) as Array<{ label: string; value: string }>,
            multi_select: !!payload.multi_select,
            allow_other: payload.allow_other !== false,
            tool_call_id: str(payload.tool_call_id) || null,
          },
        }),
      };
    }

    case "user_answered_question": {
      const qid = str(payload.question_id);
      if (!qid) return chat;
      if (!chat.entries.some((e) => e.id === `q_${qid}`)) return chat;
      return {
        ...chat,
        entries: upsertById(chat.entries, `q_${qid}`, (e) => ({
          ...e,
          status: "complete",
          answer: payload.value !== undefined ? payload.value : null,
        })),
      };
    }

    case "proactive_proposal": {
      const id = `proactive_${payload.trigger || "x"}_${Math.floor(ts * 1000)}`;
      if (chat.entries.some((e) => e.id === id)) return chat;
      return {
        ...chat,
        entries: chat.entries.concat({
          id,
          role: "assistant",
          content: str(payload.message) || "(proactive trigger without message text)",
          status: "complete",
          ts,
          proactive: true,
          proactiveTrigger: str(payload.trigger),
        }),
      };
    }

    case "cost_tick": {
      const prev: TokenUsage = chat.tokenUsage || {
        prompt_tokens: 0,
        completion_tokens: 0,
        spent_usd: 0,
        budget_usd: 0,
        last_model: "",
        turns: 0,
      };
      return {
        ...chat,
        tokenUsage: {
          prompt_tokens: prev.prompt_tokens + (Number(payload.prompt_tokens) || 0),
          completion_tokens: prev.completion_tokens + (Number(payload.completion_tokens) || 0),
          spent_usd: prev.spent_usd + (Number(payload.cost_usd ?? payload.spent_usd) || 0),
          budget_usd: Number(payload.budget_usd) || prev.budget_usd,
          last_model: str(payload.model) || prev.last_model,
          turns: prev.turns + 1,
        },
      };
    }

    case "todo_updated": {
      const items = Array.isArray(payload.items) ? (payload.items as TodoItem[]) : [];
      return {
        ...chat,
        todos: {
          items,
          count: typeof payload.count === "number" ? payload.count : items.length,
          ts,
        },
      };
    }

    // ── plan_* → 步骤条（Mission Control 新增） ─────────────────
    case "plan_started": {
      const stepIds = Array.isArray(payload.step_ids) ? (payload.step_ids as string[]) : [];
      const plan: PlanState = {
        active: true,
        status: "running",
        steps: stepIds.map((sid, i) => ({ id: sid, index: i, status: "pending" })),
      };
      return { ...chat, plan };
    }
    case "plan_step_started":
    case "plan_step_completed":
    case "plan_step_failed": {
      const sid = str(payload.step_id);
      const nextStatus =
        t === "plan_step_started" ? "running" : t === "plan_step_completed" ? "done" : "failed";
      const steps = chat.plan.steps.map((s) =>
        s.id === sid ? { ...s, status: nextStatus as PlanState["steps"][number]["status"] } : s,
      );
      return { ...chat, plan: { ...chat.plan, steps } };
    }
    case "plan_completed":
    case "plan_failed": {
      const status = (str(payload.status) || (t === "plan_completed" ? "completed" : "failed")) as PlanState["status"];
      return { ...chat, plan: { ...chat.plan, active: false, status } };
    }

    // ── 安全事件红条（10.M2.5） ─────────────────────────────────

    case "anti_req_violation": {
      // 违规终止当前回合：清 pending + 在飞泡转 error（B-38/B-46 语义），
      // 否则"正在调用 LLM · Ns"永远转下去。
      const id = "antireq_" + corr;
      const reason =
        str(payload.reason) || str(payload.message) || str(payload.kind) || "anti-requirement violation";
      const withAlert = chat.entries.concat({
        id,
        role: "system" as const,
        kind: "security" as const,
        severity: "high",
        content: `回合被拦截：${reason}`,
        status: "error" as const,
        ts,
      });
      const haveBubble = chat.entries.some((e) => e.id === corr);
      return {
        ...chat,
        pendingAssistantId: null,
        entries: haveBubble
          ? upsertById(withAlert, corr, (e) =>
              e.status === "complete" ? e : { ...e, status: "error" as const, phase: null },
            )
          : withAlert,
      };
    }

    case "prompt_injection_detected": {
      const severity = str(payload.severity) || "low";
      const source = str(payload.source) || "?";
      const id = `inj_${corr}_${payload.tool_call_id || "x"}`;
      if (chat.entries.some((e) => e.id === id)) return chat;
      const findings = Array.isArray(payload.findings)
        ? (payload.findings as Array<Record<string, unknown>>)
            .map((f) => str(f.pattern_id) || "?")
            .join(", ")
        : "";
      return {
        ...chat,
        entries: chat.entries.concat({
          id,
          role: "system",
          kind: "security",
          severity,
          content: `Prompt 注入检测（源: ${source}）${findings ? " — " + findings : ""}${payload.acted ? "（已按策略处置）" : ""}`,
          status: "complete",
          ts,
        }),
      };
    }

    // ── 实时预览数据源（10.M2 深度融合） ────────────────────────

    case "canvas_artifact_created": {
      const aid = str(payload.artifact_id);
      if (!aid) return chat;
      const art: Artifact = {
        id: aid,
        kind: str(payload.kind) || "html",
        title: str(payload.title) || aid,
        content: str(payload.content),
        ts,
      };
      const others = chat.artifacts.filter((a) => a.id !== aid);
      return { ...chat, artifacts: [art, ...others].slice(0, 8) };
    }

    case "canvas_artifact_updated": {
      const aid = str(payload.artifact_id);
      if (!chat.artifacts.some((a) => a.id === aid)) return chat;
      return {
        ...chat,
        artifacts: chat.artifacts.map((a) =>
          a.id === aid ? { ...a, content: str(payload.content), ts } : a,
        ),
      };
    }

    case "canvas_artifact_closed": {
      const aid = str(payload.artifact_id);
      return { ...chat, artifacts: chat.artifacts.filter((a) => a.id !== aid) };
    }

    case "workspace_file_changed": {
      // 路径字段后端可能给 path 或 paths；都兼容。
      const paths = Array.isArray(payload.paths)
        ? (payload.paths as string[])
        : str(payload.path)
          ? [str(payload.path)]
          : [];
      return {
        ...chat,
        workspaceVersion: chat.workspaceVersion + 1,
        workspaceLastPaths: paths.length ? paths : chat.workspaceLastPaths,
      };
    }

    // ── 并行子代理执行组（与旧 UI chat_reducer_secondary 同语义） ──

    case "worker_started": {
      const id = `w_${payload.worker_id || "?"}_${payload.task_id || "?"}`;
      if (chat.entries.some((e) => e.id === id)) return chat;
      return {
        ...chat,
        entries: chat.entries.concat({
          id,
          role: "system",
          kind: "worker",
          content: "",
          status: "running",
          ts,
          workerId: str(payload.worker_id) || "?",
          taskId: str(payload.task_id) || "?",
          promptPreview: str(payload.prompt_preview).slice(0, 240),
        }),
      };
    }

    case "worker_completed":
    case "worker_failed": {
      const id = `w_${payload.worker_id || "?"}_${payload.task_id || "?"}`;
      const ok = t === "worker_completed";
      const patch = (e: Entry): Entry => ({
        ...e,
        status: ok ? "ok" : "error",
        outputPreview: str(payload.output_preview).slice(0, 500),
        errorPreview: str(payload.error).slice(0, 500),
        elapsedSeconds: (payload.elapsed_seconds as number) ?? null,
      });
      if (!chat.entries.some((e) => e.id === id)) {
        return {
          ...chat,
          entries: chat.entries.concat(
            patch({
              id,
              role: "system",
              kind: "worker",
              content: "",
              status: "running",
              ts,
              workerId: str(payload.worker_id) || "?",
              taskId: str(payload.task_id) || "?",
            }),
          ),
        };
      }
      return { ...chat, entries: upsertById(chat.entries, id, patch) };
    }

    case "subagent_started": {
      const idx = (payload.index as number | string) ?? "?";
      const id = `sub_${ts}_${idx}`;
      if (chat.entries.some((e) => e.id === id)) return chat;
      return {
        ...chat,
        entries: chat.entries.concat({
          id,
          role: "system",
          kind: "subagent",
          content: "",
          status: "running",
          ts,
          subagentIndex: idx,
          roleHint: str(payload.role) || "general",
          promptPreview: str(payload.subtask).slice(0, 240),
        }),
      };
    }

    case "subagent_completed": {
      const idx = (payload.index as number | string) ?? "?";
      // 匹配该 index 最近的 running 子代理卡（与旧 UI 同策略）。
      const target = chat.entries
        .filter((e) => e.kind === "subagent" && e.subagentIndex === idx)
        .sort((a, b) => (b.ts || 0) - (a.ts || 0))[0];
      const fields = {
        status: (payload.ok ? "ok" : "error") as Entry["status"],
        outputPreview: str(payload.output).slice(0, 2000),
        errorPreview: str(payload.error).slice(0, 500),
        hops: (payload.hops as number) || 0,
        elapsedSeconds: (payload.elapsed_s as number) ?? null,
      };
      if (!target) {
        return {
          ...chat,
          entries: chat.entries.concat({
            id: `sub_${ts}_${idx}`,
            role: "system",
            kind: "subagent",
            content: "",
            ts,
            subagentIndex: idx,
            ...fields,
          }),
        };
      }
      return { ...chat, entries: upsertById(chat.entries, target.id, (e) => ({ ...e, ...fields })) };
    }

    default:
      return chat;
  }
}

// 发送时的本地乐观回显（服务端 USER_MESSAGE 镜像回来前先上屏）。
export function appendOptimisticUser(
  chat: ChatState,
  content: string,
  images: string[] = [],
): { id: string; chat: ChatState } {
  const id = genId();
  return {
    id,
    chat: {
      ...chat,
      entries: chat.entries.concat({
        id,
        role: "user",
        content,
        status: "complete",
        ts: Date.now() / 1000,
        images,
      }),
    },
  };
}

// 发送后立刻挂 thinking 占位，消除"发出去没反应"的空窗。
export function appendThinkingAssistant(chat: ChatState, correlationId: string): ChatState {
  if (!correlationId || chat.entries.some((e) => e.id === correlationId)) return chat;
  return {
    ...chat,
    pendingAssistantId: correlationId,
    entries: chat.entries.concat({
      id: correlationId,
      role: "assistant",
      content: "",
      status: "thinking",
      ts: Date.now() / 1000,
    }),
  };
}
