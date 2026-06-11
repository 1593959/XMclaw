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
// per-block rendering (comparable agents style) read this; the legacy
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
      // B-DEDUP: WS reconnect or bus retry can deliver the same chunk
      // twice. Skip duplicates by (turn_id, seq) so the user never
      // sees "hellohello" stutter.
      const seq = payload.seq;
      const chunkKey = seq != null ? `${id}:${seq}` : null;
      const seen = chat._seenChunks || {};
      if (chunkKey && seen[chunkKey]) {
        return chat;
      }
      const nextSeen = chunkKey
        ? { ...seen, [chunkKey]: true }
        : seen;
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : typeof payload.content === "string"
          ? payload.content
          : ""
      // B-89: stop any prior abandoned-streaming bubble before this
      // turn starts producing chunks. Some providers skip llm_request
      // and start straight from llm_chunk, so we need this guard here
      // too.
      const cleaned = finalizeAbandoned(chat.messages, id);
      const idx = cleaned.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          _seenChunks: nextSeen,
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
            // comparable agents show: thinking-row →
            // tool-row → text-row in the order events arrived.
            events: [{ type: "text", id: id + ":t0", content: delta }],
          }),
        };
      }
      return {
        ...chat,
        _seenChunks: nextSeen,
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
      // comparable agents screenshots.
      const id = corr;
      // B-269: same cancel guard as llm_chunk — provider's reasoning
      // stream also buffers, drop late thinking-deltas for cancelled
      // turns.
      if (chat.cancelledTurnIds && chat.cancelledTurnIds.has(id)) {
        return chat;
      }
      // B-DEDUP: same seq-based dedup as llm_chunk.
      const thinkSeq = payload.seq;
      const thinkKey = thinkSeq != null ? `${id}:k:${thinkSeq}` : null;
      const seen2 = chat._seenChunks || {};
      if (thinkKey && seen2[thinkKey]) {
        return chat;
      }
      const nextSeen2 = thinkKey
        ? { ...seen2, [thinkKey]: true }
        : seen2;
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : "";
      if (!delta) return chat;
      const cleaned = finalizeAbandoned(chat.messages, id);
      const idx = cleaned.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          _seenChunks: nextSeen2,
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
        _seenChunks: nextSeen2,
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
      // Wave-32+ UX fix — DON'T clear pendingAssistantId when this
      // llm_response is mid-multi-hop (i.e. the model emitted tool
      // calls that hop_loop will dispatch + come back with another
      // llm_request shortly). Clearing here made the button flicker
      // Stop→Send→Stop on every hop boundary. Keep it set when
      // tool_calls_count > 0; only clear on the FINAL hop where no
      // more tools are coming.
      const moreHopsComing = ok && (payload.tool_calls_count || 0) > 0;
      const nextPending = moreHopsComing ? id : null;
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          pendingAssistantId: nextPending,
          messages: chat.messages.concat({
            id,
            role: "assistant",
            content: finalText || errBody,
            status: moreHopsComing ? "thinking" : finalStatus,
            phase: moreHopsComing ? "calling_llm" : null,
            ts,
            toolCalls: [],
          }),
        };
      }
      return {
        ...chat,
        pendingAssistantId: nextPending,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          // Pick whichever rendition is MORE complete, don't blindly
          // trust finalText. The canonical full text recovers from a
          // dropped chunk mid-stream — BUT when finalText arrives
          // TRUNCATED (shorter than what we already streamed), trusting
          // it overwrites a correctly-rendered reply (e.g. a finished
          // markdown table) with a broken fragment. Symptom: the bubble
          // renders perfectly while streaming, then collapses to raw
          // markdown shards the instant llm_response lands. So: keep the
          // streamed content when it's strictly longer than finalText.
          content: (() => {
            const streamed = m.content || "";
            if (!finalText) return streamed || errBody;
            // B-STREAM-FINAL: defensive guard against truncated finalText.
            // Empirical case: streamed text looks complete during chunks,
            // but the terminal llm_response carries a truncated payload
            // (provider dropped trailing chars). If streamed starts with
            // finalText and has strictly more content, streamed is the
            // more complete rendition — keep it.
            if (
              streamed.length > finalText.length
              && streamed.startsWith(finalText)
            ) {
              return streamed;
            }
            return finalText.length >= streamed.length ? finalText : streamed;
          })(),
          // Mid-multi-hop: stay in "thinking" so the bubble keeps
          // its spinner + the button stays on Stop. Final hop:
          // flip to terminal status as before.
          status: moreHopsComing ? "thinking" : finalStatus,
          phase: moreHopsComing ? "calling_llm" : null,
          ts,
        })),
      };
    }

    default:
      return null;
  }
}
