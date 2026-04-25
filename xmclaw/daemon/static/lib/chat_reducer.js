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
  "llm_chunk",
  "llm_response",
  "tool_call_emitted",
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
      const id = corr;
      const finalText = typeof payload.content === "string"
        ? payload.content
        : (payload.text || "");
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: null,
          messages: chat.messages.concat({
            id,
            role: "assistant",
            content: finalText,
            status: "complete",
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
          content: finalText || m.content,
          status: "complete",
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
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          content: "Blocked: " + (payload.reason || payload.kind || "anti-requirement violation"),
          status: "error",
          ts,
        }),
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
