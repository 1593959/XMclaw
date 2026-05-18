// XMclaw — chat-reducer streaming cases (B-323 split).
//
// Lifted out of lib/chat_reducer.js to keep that file under the
// 500-line UI budget (FRONTEND_DESIGN.md §1.4). Owns the three
// LLM-streaming events (llm_chunk / llm_thinking_chunk /
// llm_response) plus the small per-bubble timeline helpers they
// use (_appendThinkingEvent / _appendTextEvent — the shared
// per-row event log B-218 introduced).
//
// Contract: ``applyStreamingEvent(chat, envelope, helpers) → chat | null``.
// Returns ``null`` when the envelope's type isn't one we handle —
// the caller falls through to its own switch.

const HANDLED = new Set([
  "llm_chunk",
  "llm_thinking_chunk",
  "llm_response",
]);


export function isStreamingEventType(t) {
  return HANDLED.has(t);
}


// B-218: per-row event timeline helpers. Each message can carry an
// ``events: []`` ordered list — chat-bubble UIs that prefer linear
// per-block rendering (CoPaw / OpenClaw style) read this; the legacy
// ``message.toolCalls`` + ``message.thinking`` aggregates stay as
// they are for back-compat with components that still rely on them.
//
// Boundary rule:
//   * llm_thinking_chunk → append to trailing thinking event, OR
//     start a new thinking block when the last event is text/tool.
//   * llm_chunk          → same logic for text events.
function _appendThinkingEvent(events, mid, delta) {
  const arr = events ? [...events] : [];
  const last = arr[arr.length - 1];
  if (last && last.type === "thinking") {
    arr[arr.length - 1] = { ...last, content: (last.content || "") + delta };
  } else {
    arr.push({
      type: "thinking",
      id: mid + ":k" + arr.length,
      content: delta,
    });
  }
  return arr;
}


function _appendTextEvent(events, mid, delta) {
  const arr = events ? [...events] : [];
  const last = arr[arr.length - 1];
  if (last && last.type === "text") {
    arr[arr.length - 1] = { ...last, content: (last.content || "") + delta };
  } else {
    arr.push({
      type: "text",
      id: mid + ":t" + arr.length,
      content: delta,
    });
  }
  return arr;
}


export function applyStreamingEvent(chat, envelope, helpers) {
  if (!envelope || typeof envelope !== "object") return null;
  const t = envelope.type;
  if (!HANDLED.has(t)) return null;

  const { upsertById, finalizeAbandoned } = helpers;
  const payload = envelope.payload || {};
  const ts = envelope.ts || Date.now() / 1000;
  const corr = envelope.correlation_id || envelope.id || "";

  switch (t) {
    case "llm_chunk": {
      // Streaming token delta. Use correlation_id (turn id) as the
      // assistant message id so subsequent chunks merge into the same
      // bubble. Create the bubble lazily on the first chunk.
      const id = corr;
      // B-269: drop chunks for turns the user cancelled. Provider
      // streams have buffered chunks already in flight when cancel
      // hits; without this guard text keeps appending after Stop is
      // clicked, defeating the user's intent. The cancelledTurnIds
      // set is populated by ``cancelComposer`` (app.js) the moment
      // Stop fires, BEFORE the WS frame travels.
      if (chat.cancelledTurnIds && chat.cancelledTurnIds.has(id)) {
        return chat;
      }
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : typeof payload.content === "string"
          ? payload.content
          : "";
      // B-89: stop any prior abandoned-streaming bubble before this
      // turn starts producing chunks. Some providers skip llm_request
      // and start straight from llm_chunk, so we need this guard here
      // too.
      const cleaned = finalizeAbandoned(chat.messages, id);
      const idx = cleaned.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: id,
          messages: cleaned.concat({
            id,
            role: "assistant",
            content: delta,
            status: "streaming",
            ts,
            toolCalls: [],
            // B-218: chronological event timeline. Each entry =
            // one rendered row in MessageBubble. Mirrors what
            // OpenClaw / CoPaw / Hermes show: thinking-row →
            // tool-row → text-row in the order events arrived.
            events: [{ type: "text", id: id + ":t0", content: delta }],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        messages: upsertById(cleaned, id, (m) => ({
          ...m,
          content: m.content + delta,
          status: "streaming",
          // B-218: append to the trailing text event when the last
          // event is text; otherwise start a new text event after
          // the most recent thinking / tool block. Keeps things
          // grouped naturally.
          events: _appendTextEvent(m.events || [], id, delta),
        })),
      };
    }

    case "llm_thinking_chunk": {
      // B-91 / B-218: reasoning / extended-thinking token delta.
      // Pre-B-218 accumulated into a single ``message.thinking``
      // string, rendered in PhaseCard above the bubble. Now ALSO
      // appended into ``message.events`` so MessageBubble can show
      // each thinking BLOCK as its own collapsible row inline with
      // the tool calls — matches the per-row layout users see in
      // CoPaw / OpenClaw screenshots.
      const id = corr;
      // B-269: same cancel guard as llm_chunk — provider's reasoning
      // stream also buffers, drop late thinking-deltas for cancelled
      // turns.
      if (chat.cancelledTurnIds && chat.cancelledTurnIds.has(id)) {
        return chat;
      }
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : "";
      if (!delta) return chat;
      const cleaned = finalizeAbandoned(chat.messages, id);
      const idx = cleaned.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: id,
          messages: cleaned.concat({
            id,
            role: "assistant",
            content: "",
            thinking: delta,
            status: "thinking",
            phase: "calling_llm",
            ts,
            toolCalls: [],
            events: [{ type: "thinking", id: id + ":k0", content: delta }],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: id,
        messages: upsertById(cleaned, id, (m) => ({
          ...m,
          thinking: (m.thinking || "") + delta,
          // B-218: append to trailing thinking event OR open a new
          // thinking block when the last event is tool/text. This
          // is what gives us the per-block rendering peers have.
          events: _appendThinkingEvent(m.events || [], id, delta),
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
      // Wave-32+ UX fix: if the user already clicked Stop on this
      // turn, the bubble is already marked "cancelled" and
      // pendingAssistantId is already cleared — a late-arriving
      // terminal event must NOT overwrite the cancelled state with
      // "complete" (which would show the post-cancel reply the user
      // explicitly chose not to wait for). Same B-269 logic as for
      // late chunks.
      if (chat.cancelledTurnIds && chat.cancelledTurnIds.has(id)) {
        return chat;
      }
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

    default:
      return null;
  }
}
