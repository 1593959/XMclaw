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

export const PHASE_1_EVENT_TYPES = [
  "user_message",
  "llm_request",
  "llm_chunk",
  "llm_response",
  "tool_call_emitted",
  "tool_invocation_started",
  "tool_invocation_finished",
  "anti_req_violation",
  "session_lifecycle",
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

export function applyEvent(chat, envelope) {
  if (!envelope || typeof envelope !== "object") return chat;
  const t = envelope.type;
  const payload = envelope.payload || {};
  const ts = envelope.ts || Date.now() / 1000;
  const corr = envelope.correlation_id || envelope.id || genId();

  switch (t) {
    case "user_message": {
      // The daemon echoes the user's frame back as USER_MESSAGE — this is
      // how we get a canonical ts/id even for our own send. If we already
      // have a local-optimistic copy with the same correlation_id, replace
      // it; otherwise append.
      const id = corr;
      const exists = chat.messages.some((m) => m.id === id);
      if (exists) {
        return {
          ...chat,
          messages: upsertById(chat.messages, id, (m) => ({
            ...m,
            content: typeof payload.content === "string" ? payload.content : m.content,
            status: "complete",
            ts,
          })),
        };
      }
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "user",
          content: typeof payload.content === "string" ? payload.content : "",
          status: "complete",
          ts,
          ultrathink: !!payload.ultrathink,
        }),
      };
    }

    case "llm_request": {
      // B-43: phase-update — let the user see what stage the turn is in
      // ("calling LLM" vs the generic "thinking dots") so a slow first
      // token doesn't feel like a hang. Upserts the existing thinking
      // bubble; create one if our optimistic-echo missed (race).
      const id = corr;
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: id,
          messages: chat.messages.concat({
            id,
            role: "assistant",
            content: "",
            status: "thinking",
            phase: "calling_llm",
            ts,
            toolCalls: [],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          phase: "calling_llm",
        })),
      };
    }

    case "tool_invocation_started": {
      // B-43: bubble phase update. The matching tool_call_emitted case
      // (below) already adds the ToolCard with status=pending; this
      // event flips it to 'running' AND tags the bubble's phase so the
      // header reads "正在执行工具" instead of "正在思考".
      const id = corr;
      const callId = payload.call_id || payload.id;
      return {
        ...chat,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          phase: "tool_running",
          toolCalls: (m.toolCalls || []).map((tc) =>
            tc.id === callId ? { ...tc, status: "running" } : tc,
          ),
        })),
      };
    }

    case "llm_chunk": {
      // Streaming token delta. Use correlation_id (turn id) as the
      // assistant message id so subsequent chunks merge into the same
      // bubble. Create the bubble lazily on the first chunk.
      const id = corr;
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : typeof payload.content === "string"
          ? payload.content
          : "";
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: id,
          messages: chat.messages.concat({
            id,
            role: "assistant",
            content: delta,
            status: "streaming",
            ts,
            toolCalls: [],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          content: m.content + delta,
          status: "streaming",
        })),
      };
    }

    case "llm_response": {
      // Final assistant turn. If we never saw chunks (non-streaming model),
      // create the bubble in one shot.
      // B-46: an LLM call that errored out emits {ok: false, error: ...}
      // with no text. Mark the bubble as 'error' (not 'complete') so the
      // user sees the failure instead of an empty completed bubble, and
      // clear `phase` so the "正在调用 LLM · Ns" indicator stops ticking.
      const id = corr;
      const finalText = typeof payload.content === "string"
        ? payload.content
        : (payload.text || "");
      const ok = payload.ok !== false;
      const finalStatus = ok ? "complete" : "error";
      const errBody = !ok ? `LLM 调用失败：${payload.error || "未知"}` : "";
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: null,
          messages: chat.messages.concat({
            id,
            role: "assistant",
            content: finalText || errBody,
            status: finalStatus,
            phase: null,
            ts,
            toolCalls: [],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: null,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          // If the server sent the canonical full text, prefer it over
          // accumulated chunks — this is how we recover from a dropped
          // chunk mid-stream.
          content: finalText || m.content || errBody,
          status: finalStatus,
          phase: null,
          ts,
        })),
      };
    }

    case "tool_call_emitted": {
      const aid = corr;
      const callId = payload.tool_call_id || payload.id || genId();
      const tc = {
        id: callId,
        name: payload.name || payload.tool_name || "tool",
        args: payload.args || payload.arguments || {},
        status: "running",
        result: null,
      };
      const idx = chat.messages.findIndex((m) => m.id === aid);
      if (idx === -1) {
        // No assistant bubble yet — create one with the tool card attached.
        return {
          ...chat,
          messages: chat.messages.concat({
            id: aid,
            role: "assistant",
            content: "",
            status: "streaming",
            ts,
            toolCalls: [tc],
          }),
        };
      }
      return {
        ...chat,
        messages: upsertById(chat.messages, aid, (m) => ({
          ...m,
          toolCalls: (m.toolCalls || []).concat(tc),
        })),
      };
    }

    case "tool_invocation_finished": {
      const aid = corr;
      const callId = payload.tool_call_id || payload.id;
      const status = payload.error ? "error" : "ok";
      const result = payload.error
        ? String(payload.error)
        : (typeof payload.result === "string"
            ? payload.result
            : JSON.stringify(payload.result || {}, null, 2));
      const idx = chat.messages.findIndex((m) => m.id === aid);
      if (idx === -1) return chat;
      return {
        ...chat,
        messages: upsertById(chat.messages, aid, (m) => ({
          ...m,
          toolCalls: (m.toolCalls || []).map((tc) =>
            tc.id === callId ? { ...tc, status, result } : tc
          ),
        })),
      };
    }

    case "anti_req_violation": {
      // Always render as an inline system bubble so the user can see why a
      // turn was blocked.
      const id = "antireq_" + corr;
      // B-38 + B-46: a violation event terminates the turn. Two cleanups:
      //   1) clear pendingAssistantId so Stop flips back to Send.
      //   2) flip the in-flight assistant bubble's status from
      //      'thinking'/'streaming' → 'error' so the "正在调用 LLM · Ns"
      //      indicator stops ticking. Without (2) the indicator stuck at
      //      thousands of seconds — the LLM call legitimately ended (the
      //      anti_req fired), but the bubble never got a terminal status
      //      because llm_response wasn't emitted (the violation took its
      //      place).
      const reason = payload.reason || payload.message || payload.kind || "anti-requirement violation";
      const haveBubble = chat.messages.findIndex((m) => m.id === corr) !== -1;
      const messages = chat.messages.concat({
        id,
        role: "system",
        content: "Blocked: " + reason,
        status: "error",
        ts,
      });
      const finalMessages = haveBubble
        ? upsertById(messages, corr, (m) => (
            m.status === "complete" ? m : { ...m, status: "error", phase: null }
          ))
        : messages;
      return {
        ...chat,
        pendingAssistantId: null,
        messages: finalMessages,
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
export function appendOptimisticUser(chat, content, { ultrathink = false } = {}) {
  const id = genId();
  const messages = chat.messages.concat({
    id,
    role: "user",
    content,
    status: "complete",
    ts: Date.now() / 1000,
    ultrathink,
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
