// XMclaw — Chat event reducer
//
// Translates `BehavioralEvent` envelopes coming off the WS into mutations
// on the chat slice. Pure — no DOM, no store reference — so it's trivially
// unit-testable. The caller (app.js) wraps it as:
//
//   ws.onEvent = (envelope) => store.setState((s) => ({
//     chat: applyEvent(s.chat, envelope),
//     session: applySessionLifecycle(s.session, envelope),
//   }));
//
// We support the Phase-1 essential subset of EventType:
//   USER_MESSAGE              → push a finalized user bubble
//   LLM_CHUNK                 → append delta to the in-flight assistant
//   LLM_RESPONSE              → finalize / replace assistant content
//   TOOL_CALL_EMITTED         → start a tool card under the assistant turn
//   TOOL_INVOCATION_FINISHED  → close the tool card with status + result
//   ANTI_REQ_VIOLATION        → emit an inline system error bubble
//   SESSION_LIFECYCLE         → update session status (handled by sibling
//                               reducer; we only echo a system bubble for
//                               "create" / "ended" milestones).
//
// All other event types pass through unchanged. The chat slice uses a flat
// messages[] with stable string ids; streaming appends mutate the matching
// message rather than creating new ones, which keeps Preact's reconciler
// happy via key={msg.id}.
//
// B-323: secondary cases (skill / cost / grader / context_compressed /
// todo / prompt_injection / anti_req_violation) live in
// chat_reducer_secondary.js so this file stays under the 500-line UI
// budget (FRONTEND_DESIGN.md §1.4). The applyEvent below dispatches
// to the secondary reducer first; falls through to its own switch
// when not handled.

import {
  applySecondaryEvent,
  isSecondaryEventType,
} from "./chat_reducer_secondary.js";
import {
  applyStreamingEvent,
  isStreamingEventType,
} from "./chat_reducer_streaming.js";

export const PHASE_1_EVENT_TYPES = [
  "user_message",
  "llm_request",
  "llm_chunk",
  "llm_thinking_chunk",  // B-91
  "llm_response",
  "tool_call_emitted",
  "tool_invocation_started",
  "tool_invocation_finished",
  "agent_asked_question",   // B-92
  "user_answered_question", // B-92
  "cost_tick",              // B-107
  "anti_req_violation",
  "session_lifecycle",
  "skill_invoked",          // B-130: heuristic-path skill detections
  "skill_outcome",          // B-130: turn-level verdict for the skill
  "proactive_proposal",     // Sprint 1: agent-initiated message
];

function genId() {
  return "m_" + Math.random().toString(16).slice(2, 10);
}


function upsertById(messages, id, patcher) {
  const idx = messages.findIndex((m) => m.id === id);
  if (idx === -1) return messages;
  const next = messages.slice();
  next[idx] = patcher(next[idx]);
  return next;
}

// B-89: when a fresh assistant turn starts (new correlation_id), any
// PRIOR assistant bubble still in 'thinking' / 'streaming' state has
// effectively been abandoned — its terminal LLM_RESPONSE event never
// arrived (WS reconnect mid-stream / daemon kill / etc). Without this,
// the old bubble keeps the "正在调用 LLM · Ns" header running forever
// underneath the actual new assistant reply, which is the bug the
// user reported (11752s ticking on a stale bubble while a fresh
// answer is already streaming below it).
function _finalizeAbandoned(messages, newAssistantId) {
  let touched = false;
  const next = messages.map((m) => {
    if (m.id === newAssistantId) return m;
    if (m.role !== "assistant") return m;
    if (m.status !== "thinking" && m.status !== "streaming") return m;
    touched = true;
    return {
      ...m,
      status: "complete",  // best-effort: don't fail loud, just stop the spinner
      phase: null,         // kill the "正在调用 LLM · Ns" indicator
    };
  });
  return touched ? next : messages;
}

// 2026-06-06: 收尾「卡死的 running 工具卡」。漏了 tool_invocation_finished
// 事件的工具卡会永远停在 running（用户报「running ▸ 写死不变」）。新一轮
// 开始（llm_request / user_message）时，把不属于当前 turn（correlationId
// 不等于 keepCorr）的 running 工具卡收尾成终态：有结果→ok，无结果→done
// （ToolCard 把 done 渲染成中性「已结束」，不再转圈）。
function _finalizeStaleTools(messages, keepCorr) {
  let touched = false;
  const next = messages.map((m) => {
    if (m.kind !== "tool_use" || m.status !== "running") return m;
    if (keepCorr && m.correlationId === keepCorr) return m; // 当前 turn 在飞，保留
    touched = true;
    return { ...m, status: m.result != null ? "ok" : "done" };
  });
  return touched ? next : messages;
}

// B-MULTIMODAL-UI: append the pairing token to /api/v2/media/* URLs so
// the <img src> request passes the daemon's auth middleware. Without
// this, screenshots from tool results render as broken thumbnails
// (401). Centralized so user_message AND tool_invocation_finished
// branches share one implementation — duplicating it inside
// user_message was the original Wave 25.8 bug.
export function _resolveMediaUrl(u) {
  if (!u || typeof u !== "string") return u;
  if (u.startsWith("data:") || u.startsWith("http")) return u;
  // Token is fetched from /api/v2/pair and exposed as window.__xmc_token
  // by boot(). Fall back to the legacy URL-param path so tests + direct
  // navigation still work, but the primary source is the global.
  let token = "";
  try {
    if (typeof window !== "undefined") {
      token = window.__xmc_token || "";
      if (!token) {
        token = new URL(window.location.href).searchParams.get("token") || "";
      }
    }
  } catch (_e) { /* SSR or missing window */ }
  if (!token) return u;
  const sep = u.includes("?") ? "&" : "?";
  return u + sep + "token=" + encodeURIComponent(token);
}

// Wave 26 (fix-2): rewrite src/href attributes pointing at /api/v2/media/
// inside a pre-sanitized HTML string so markdown-rendered ``![alt](path)``
// images load through the auth middleware. Called on the html string from
// marked → DOMPurify, NOT on raw LLM output (so we don't have to re-escape
// anything). Only rewrites URLs that already start with /api/v2/media/
// or that DOMPurify rewrote to a relative form — leaves data:/http(s)
// URLs alone.
export function resolveMediaTokenInHtml(html) {
  if (!html || typeof html !== "string") return html;
  if (!html.includes("/api/v2/media/")) return html;
  return html.replace(
    /(src|href)="(\/api\/v2\/media\/[^"]+)"/g,
    (_m, attr, url) => `${attr}="${_resolveMediaUrl(url)}"`,
  );
}

export function applyEvent(chat, envelope) {
  if (!envelope || typeof envelope !== "object") return chat;
  const t = envelope.type;
  const payload = envelope.payload || {};
  const ts = envelope.ts || Date.now() / 1000;
  const corr = envelope.correlation_id || envelope.id || genId();

  // B-323: streaming cases (llm_chunk / llm_thinking_chunk /
  // llm_response) live in chat_reducer_streaming.js, secondary cases
  // (cost_tick / grader_verdict / skill_* / anti_req_violation /
  // context_compressed / todo_updated / prompt_injection_detected)
  // live in chat_reducer_secondary.js — both keep this file under the
  // 500-line UI budget. Each sub-reducer returns null when the event
  // type isn't theirs; we fall through to the main switch below.
  if (isStreamingEventType(t)) {
    const next = applyStreamingEvent(chat, envelope, {
      upsertById, finalizeAbandoned: _finalizeAbandoned,
    });
    if (next !== null) return next;
  }
  if (isSecondaryEventType(t)) {
    const next = applySecondaryEvent(chat, envelope, { upsertById });
    if (next !== null) return next;
  }

  switch (t) {
    case "user_message": {
      // The daemon echoes the user's frame back as USER_MESSAGE — this is
      // how we get a canonical ts/id even for our own send. If we already
      // have a local-optimistic copy with the same correlation_id, replace
      // it; otherwise append.
      const id = corr;
      const serverImages = Array.isArray(payload.images)
        ? payload.images.map(_resolveMediaUrl)
        : [];
      // 新用户消息 = 上一轮彻底结束 → 收尾所有残留 running 工具卡。
      const sweptMsgs = _finalizeStaleTools(chat.messages, null);
      const exists = sweptMsgs.some((m) => m.id === id);
      if (exists) {
        return {
          ...chat,
          messages: upsertById(sweptMsgs, id, (m) => ({
            ...m,
            content: typeof payload.content === "string" ? payload.content : m.content,
            status: "complete",
            ts,
            // Prefer the server-side persisted URLs (resolves through
            // reload), but keep the optimistic data: URLs as fallback
            // if the server payload didn't carry images.
            images: serverImages.length > 0 ? serverImages : (m.images || []),
          })),
        };
      }
      return {
        ...chat,
        messages: sweptMsgs.concat({
          id,
          role: "user",
          content: typeof payload.content === "string" ? payload.content : "",
          status: "complete",
          ts,
          ultrathink: !!payload.ultrathink,
          images: serverImages,
        }),
      };
    }

    case "llm_request": {
      // B-43: phase-update — let the user see what stage the turn is in
      // ("calling LLM" vs the generic "thinking dots") so a slow first
      // token doesn't feel like a hang. Upserts the existing thinking
      // bubble; create one if our optimistic-echo missed (race).
      const id = corr;
      // B-89: a new turn starting → any abandoned earlier bubble must
      // stop spinning. Same call-site exists in llm_chunk + tool_call
      // below for the cases where llm_request didn't fire first.
      const cleaned = _finalizeStaleTools(_finalizeAbandoned(chat.messages, id), id);
      // B-90: snapshot the request metadata onto the bubble so PhaseCard
      // can show useful detail when the user expands it (model name,
      // tool-loop hop, history depth, available tool count). Append to
      // any existing phaseMeta so a tool→LLM→tool→LLM loop preserves
      // hop history rather than clobbering it.
      const newMeta = {
        kind: "llm_request",
        model: payload.model || null,
        hop: payload.hop != null ? payload.hop : null,
        messages_count: payload.messages_count != null ? payload.messages_count : null,
        tools_count: payload.tools_count != null ? payload.tools_count : null,
        llm_profile_id: payload.llm_profile_id || null,
        started_at: ts,
      };
      const idx = cleaned.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: id,
          messages: cleaned.concat({
            id,
            role: "assistant",
            content: "",
            status: "thinking",
            phase: "calling_llm",
            phaseMeta: newMeta,
            phaseHistory: [newMeta],
            ts,
            toolCalls: [],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        messages: upsertById(cleaned, id, (m) => ({
          ...m,
          phase: "calling_llm",
          phaseMeta: newMeta,
          phaseHistory: [...(m.phaseHistory || []), newMeta],
        })),
      };
    }

    case "tool_invocation_started": {
      // B-220: with tool_use as its own sibling message, started
      // is largely redundant (emitted already created with
      // status=running). Keep as defensive no-op patch in case some
      // MCP shim emits started without emitted.
      const callId = payload.call_id || payload.id;
      const idx = chat.messages.findIndex(
        (m) => m.kind === "tool_use" && m.id === callId,
      );
      if (idx === -1) return chat;
      return {
        ...chat,
        messages: upsertById(chat.messages, callId, (m) => ({
          ...m,
          status: m.status === "running" ? m.status : "running",
        })),
      };
    }


    case "tool_call_emitted": {
      // B-220: tool_use is now its OWN top-level sibling message —
      // matches the standard chat-log (each tool execution is its own
      // child of the linear chat container). The previous bubble's
      // toolCalls aggregator is gone; tool_invocation_finished still
      // finds the entry by callId because we use callId as message.id.
      const toolName = payload.name || payload.tool_name || "tool";
      // B-231: ``ask_user_question`` and friends own their dedicated
      // UI surface (QuestionCard for ask_user_question via the
      // ``agent_asked_question`` event). Rendering a generic ToolCard
      // alongside dumps the raw args JSON in the chat AND leaves it
      // stuck at "running" (the result lands when the user clicks an
      // option — but the question ITSELF is already the user-facing
      // affordance, the tool_use card is redundant noise). Skip
      // ToolCard creation for these tools; tool_invocation_finished
      // will not find a matching entry and become a clean no-op.
      const _UI_SUPPRESSED_TOOLS = new Set(["ask_user_question"]);
      if (_UI_SUPPRESSED_TOOLS.has(toolName)) {
        return chat;
      }
      // B-232: the daemon's BehavioralEvent payload key is ``call_id``
      // (snake_case, with underscore — see agent_loop.py:2467/2473/2519).
      // Pre-B-232 the reducer only checked ``tool_call_id`` / ``id``,
      // both undefined → fell through to ``genId()`` which assigned
      // a fresh random id. Then ``tool_invocation_finished`` couldn't
      // match the message because it had been keyed under the random
      // id, NOT the real call_id. End result: every tool_use bubble
      // stuck at "running" forever (list_agents was the most visible
      // case because it returns instantly, so the gap between
      // emitted and finished is microseconds — the user expected to
      // see ✓ but saw a perpetual ⏳).
      const callId = payload.call_id || payload.tool_call_id || payload.id || genId();
      // B-267: if tool_invocation_finished arrived FIRST (race), we
      // already created a bubble at this id with status="ok|error"
      // and a real result. Don't trample it with status="running"
      // — patch in name/args metadata if missing, leave finished state.
      const existingIdx = chat.messages.findIndex(
        (m) => m.kind === "tool_use" && m.id === callId,
      );
      if (existingIdx !== -1) {
        return {
          ...chat,
          messages: upsertById(chat.messages, callId, (m) => ({
            ...m,
            // Only fill in missing/placeholder fields. NEVER overwrite
            // status or result — those came from the finished event.
            name: m.name && m.name !== "tool" ? m.name : toolName,
            args: m.args && Object.keys(m.args).length > 0
              ? m.args
              : (payload.args || payload.arguments || {}),
            correlationId: m.correlationId || corr,
          })),
        };
      }
      const cleanedTC = _finalizeAbandoned(chat.messages, corr);
      return {
        ...chat,
        messages: cleanedTC.concat({
          id: callId,
          kind: "tool_use",
          role: "assistant",
          correlationId: corr,
          name: toolName,
          args: payload.args || payload.arguments || {},
          status: "running",
          result: null,
          ts,
        }),
      };
    }

    case "tool_invocation_finished": {
      // B-220: find the tool_use sibling by its message.id (= callId)
      // and patch in place. The old bubble.toolCalls path is gone.
      // B-232: same payload-key bug as tool_call_emitted — the daemon
      // emits ``call_id`` not ``tool_call_id``. Reading the wrong key
      // here was the second half of the "stuck at running" bug.
      const callId = payload.call_id || payload.tool_call_id || payload.id;
      const status = payload.error ? "error" : "ok";
      const result = payload.error
        ? String(payload.error)
        : (typeof payload.result === "string"
            ? payload.result
            : JSON.stringify(payload.result || {}, null, 2));
      // B-MULTIMODAL-UI / Wave 26: surface attached media. Backend
      // publishes ``payload.images`` + ``payload.videos`` +
      // ``payload.audios`` as lists of /api/v2/media/<filename> URLs
      // (from screenshot / image_read / view_image / view_video /
      // camera_capture / etc.). Append the pairing token so the
      // <img>/<video>/<audio> fetch passes auth.
      const images = Array.isArray(payload.images)
        ? payload.images.map(_resolveMediaUrl)
        : [];
      const videos = Array.isArray(payload.videos)
        ? payload.videos.map(_resolveMediaUrl)
        : [];
      const audios = Array.isArray(payload.audios)
        ? payload.audios.map(_resolveMediaUrl)
        : [];
      const idx = chat.messages.findIndex(
        (m) => m.kind === "tool_use" && m.id === callId,
      );
      if (idx === -1) {
        // B-267: tool_invocation_finished arrived BEFORE tool_call_emitted
        // (WS multiplexing reorders, fast tools like list_agents
        // complete in 0.022ms — emit and finish events race to the
        // client). Pre-B-267 we ``return chat`` and dropped the result
        // forever; the bubble (when emit eventually arrived) stayed
        // "running" with no way to recover. Now we synthesise the
        // bubble in finished state, carrying the result. If
        // tool_call_emitted later arrives with the same callId, the
        // existing upsertById path patches name/args onto this bubble
        // (id collision = same message). Net: no race-induced data
        // loss, regardless of arrival order.
        return {
          ...chat,
          messages: chat.messages.concat({
            id: callId,
            kind: "tool_use",
            role: "assistant",
            correlationId: corr,
            name: payload.name || payload.tool_name || "tool",
            args: payload.args || payload.arguments || {},
            status,
            result,
            images,
            videos,
            audios,
            ts,
          }),
        };
      }
      return {
        ...chat,
        messages: upsertById(chat.messages, callId, (m) => ({
          ...m,
          status,
          result,
          images,
          videos,
          audios,
        })),
      };
    }

    case "proactive_proposal": {
      // Sprint 1: ProactiveAgent surfaces a trigger as an agent-
      // initiated bubble. No correlation_id (these aren't part of
      // any user turn). Each proposal renders as a regular
      // assistant message tagged proactive=true; clicking it can
      // open a follow-up turn (handled in MessageBubble).
      const id = "proactive_" + (payload.trigger || "x")
        + "_" + Math.floor(ts * 1000);
      const exists = chat.messages.some((m) => m.id === id);
      if (exists) return chat;
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "assistant",
          content: typeof payload.message === "string"
            ? payload.message
            : "(proactive trigger without message text)",
          status: "complete",
          ts,
          proactive: true,
          proactiveTrigger: payload.trigger || "",
          proactiveUrgency: payload.urgency || "normal",
        }),
      };
    }

    case "agent_asked_question": {
      // B-345: live-render the QuestionCard the moment the daemon
      // emits AGENT_ASKED_QUESTION. Pre-B-345 the event type was in
      // PHASE_1_EVENT_TYPES (recognised) but the switch had no case —
      // so the message-bus event reached the reducer and silently
      // fell through to ``default``. The QuestionCard only ever
      // appeared via ``rehydratePendingQuestions`` on WS connect (a
      // GET against /api/v2/pending_questions). End result: agent
      // mid-turn ``ask_user_question`` was invisible until the user
      // refreshed the tab — exactly the bug user reported.
      //
      // Payload shape matches the rehydrate path in app.js so a card
      // built here is interchangeable with one built from the
      // recovery API. Idempotent: skip when a card with the same
      // question_id already exists (covers the rare race where
      // rehydrate ran first then the live event arrived).
      const qid = payload.question_id;
      if (!qid) return chat;
      const exists = chat.messages.some(
        (m) => m.kind === "question" && m.question && m.question.id === qid,
      );
      if (exists) return chat;
      return {
        ...chat,
        messages: chat.messages.concat({
          id: "q_" + qid,
          role: "system",
          kind: "question",
          content: "",
          status: "pending",
          ts,
          question: {
            id: qid,
            question: payload.question || "",
            options: Array.isArray(payload.options) ? payload.options : [],
            multi_select: !!payload.multi_select,
            allow_other: payload.allow_other !== false,
            tool_call_id: payload.tool_call_id || null,
          },
        }),
      };
    }

    case "user_answered_question": {
      // B-345: when the answer comes back (either echoed by the
      // daemon after the user clicked a card option, OR replayed
      // from the bus on session resume), mark the matching card as
      // answered. QuestionCard reads ``message.status === "complete"``
      // to flip into the read-only summary view and reads
      // ``message.answer`` for the chosen value — match that exact
      // shape (NOT ``status="answered"`` or nesting under
      // ``question.answer``) so the card flips to "已回答" without a
      // refresh. Without this case a stale active card stayed live
      // and the user could submit twice.
      const qid = payload.question_id;
      if (!qid) return chat;
      const cardId = "q_" + qid;
      const idx = chat.messages.findIndex((m) => m.id === cardId);
      if (idx === -1) return chat;
      return {
        ...chat,
        messages: upsertById(chat.messages, cardId, (m) => ({
          ...m,
          status: "complete",
          answer: payload.value !== undefined ? payload.value : null,
        })),
      };
    }

    default:
      return chat;
  }
}

export function applySessionLifecycle(session, envelope) {
  if (!envelope || envelope.type !== "session_lifecycle") return session;
  const payload = envelope.payload || {};
  const phase = payload.phase || payload.lifecycle || "unknown";
  return { ...session, lifecycle: phase };
}

// Helper for the composer's optimistic local echo when sending a user
// message before the server's USER_MESSAGE event arrives. Returns the
// generated id so the caller can hand it to ws.send() as correlation_id.
export function appendOptimisticUser(chat, content, { ultrathink = false, images = [] } = {}) {
  const id = genId();
  const messages = chat.messages.concat({
    id,
    role: "user",
    content,
    status: "complete",
    ts: Date.now() / 1000,
    ultrathink,
    // B-MULTIMODAL-UI: include images in the optimistic echo so the
    // user sees their attachments immediately, before the server
    // mirrors the USER_MESSAGE event back. Each entry is a data: URL
    // or /api/v2/media/... reference.
    images: Array.isArray(images) ? images : [],
  });
  return { id, chat: { ...chat, messages } };
}

// Right after a send, push an empty assistant bubble with
// `status: "thinking"` keyed by the *correlation_id* (== turn id).
// The first `llm_chunk` event upserts it into `status: "streaming"`
// with content; `llm_response` finalizes to `status: "complete"`.
//
// Without this, the user sees a gap between "你: ..." and the eventual
// reply — sometimes seconds — with no visible signal that the agent
// even received the message. The thinking bubble bridges that gap.
export function appendThinkingAssistant(chat, correlationId) {
  if (!correlationId) return chat;
  // Defensive: if a bubble for this id already exists (race with the
  // server's first chunk), don't double-up.
  if (chat.messages.some((m) => m.id === correlationId)) return chat;
  return {
    ...chat,
    pendingAssistantId: correlationId,
    messages: chat.messages.concat({
      id: correlationId,
      role: "assistant",
      content: "",
      status: "thinking",
      ts: Date.now() / 1000,
      toolCalls: [],
    }),
  };
}
