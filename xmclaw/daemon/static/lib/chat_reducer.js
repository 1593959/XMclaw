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
];

function genId() {
  return "m_" + Math.random().toString(16).slice(2, 10);
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
//   * tool_call_emitted  → always pushes a fresh tool event (each
//     tool invocation is one row).
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
      // B-89: a new turn starting → any abandoned earlier bubble must
      // stop spinning. Same call-site exists in llm_chunk + tool_call
      // below for the cases where llm_request didn't fire first.
      const cleaned = _finalizeAbandoned(chat.messages, id);
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
      // B-219: under the flat-sibling layout, tool_use is its own
      // message keyed by callId. tool_call_emitted already sets
      // status=running so this is a no-op patch — kept around in
      // case providers emit started without emitted (some MCP
      // shims do this).
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

    case "llm_chunk": {
      // B-219: peer-pattern flat sibling layout (OpenClaw chat-log.ts +
      // free-code Messages.tsx confirmed via desktop source read).
      // Each event = independent top-level message. A turn produces
      // multiple sibling messages (text / thinking / tool_use) in
      // arrival order, NOT one mega-bubble per hop.
      //
      // For ``llm_chunk``: append delta to the trailing assistant_text
      // message IF it belongs to the same correlation_id AND no
      // tool_use / thinking sibling has interrupted; otherwise spawn a
      // fresh assistant_text message (post-tool-result narration).
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : typeof payload.content === "string"
          ? payload.content
          : "";
      const cleaned = _finalizeAbandoned(chat.messages, corr);
      const last = cleaned[cleaned.length - 1];
      const canAppend =
        last
        && last.kind === "assistant_text"
        && last.correlationId === corr
        && last.status === "streaming";
      if (canAppend) {
        return {
          ...chat,
          pendingAssistantId: last.id,
          messages: upsertById(cleaned, last.id, (m) => ({
            ...m,
            content: (m.content || "") + delta,
          })),
        };
      }
      const id = `${corr}:text:${cleaned.length}`;
      return {
        ...chat,
        pendingAssistantId: id,
        messages: cleaned.concat({
          id,
          kind: "assistant_text",
          role: "assistant",
          correlationId: corr,
          content: delta,
          status: "streaming",
          ts,
        }),
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
      const delta = typeof payload.delta === "string"
        ? payload.delta
        : "";
      if (!delta) return chat;
      // B-219: flat-sibling layout. Same logic as llm_chunk but for
      // a separate kind=assistant_thinking row. Tool / text in
      // between starts a fresh thinking block; consecutive thinking
      // chunks merge into one.
      const cleaned = _finalizeAbandoned(chat.messages, corr);
      const last = cleaned[cleaned.length - 1];
      const canAppend =
        last
        && last.kind === "assistant_thinking"
        && last.correlationId === corr
        && last.status === "streaming";
      if (canAppend) {
        return {
          ...chat,
          pendingAssistantId: last.id,
          messages: upsertById(cleaned, last.id, (m) => ({
            ...m,
            content: (m.content || "") + delta,
          })),
        };
      }
      const id = `${corr}:think:${cleaned.length}`;
      return {
        ...chat,
        pendingAssistantId: id,
        messages: cleaned.concat({
          id,
          kind: "assistant_thinking",
          role: "assistant",
          correlationId: corr,
          content: delta,
          status: "streaming",
          ts,
        }),
      };
    }

    case "agent_asked_question": {
      // B-92: agent stops mid-turn to ask a multi-choice question.
      // Lives as a system-tagged bubble in the transcript so the user
      // sees what's being asked alongside any preceding tool calls
      // and assistant text. The QuestionCard component renders an
      // interactive UI from message.question.
      const id = "q_" + (payload.question_id || corr);
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "question",
          content: "",
          status: "pending",
          ts,
          question: {
            id: payload.question_id || corr,
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
      // B-92: collapse the QuestionCard. We mark the matching bubble
      // as 'complete' and stash the answer so the card can render a
      // read-only summary ("you picked: …") instead of disappearing.
      const id = "q_" + (payload.question_id || corr);
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) return chat;
      return {
        ...chat,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          status: "complete",
          answer: payload.value,
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
      // B-219: each tool invocation is its OWN top-level message
      // (kind=tool_use). The previous bubble-aggregation pattern is
      // gone; this matches OpenClaw chat-log.ts startTool() and
      // free-code AssistantToolUseMessage layout.
      const callId = payload.tool_call_id || payload.id || genId();
      const cleanedTC = _finalizeAbandoned(chat.messages, corr);
      // Mark any open assistant_text bubble in this run as complete
      // — once a tool fires, that text run is finalised.
      const finalised = cleanedTC.map((m) =>
        m.kind === "assistant_text"
          && m.correlationId === corr
          && m.status === "streaming"
          ? { ...m, status: "complete" }
          : m,
      );
      return {
        ...chat,
        messages: finalised.concat({
          id: callId,
          kind: "tool_use",
          role: "assistant",
          correlationId: corr,
          name: payload.name || payload.tool_name || "tool",
          args: payload.args || payload.arguments || {},
          status: "running",
          result: null,
          ts,
        }),
      };
    }

    case "tool_invocation_finished": {
      // B-219: tool_use is now its own top-level message — find by
      // the tool_call_id and patch in place.
      const callId = payload.tool_call_id || payload.id;
      const status = payload.error ? "error" : "ok";
      const result = payload.error
        ? String(payload.error)
        : (typeof payload.result === "string"
            ? payload.result
            : JSON.stringify(payload.result || {}, null, 2));
      const idx = chat.messages.findIndex(
        (m) => m.kind === "tool_use" && m.id === callId,
      );
      if (idx === -1) return chat;
      return {
        ...chat,
        messages: upsertById(chat.messages, callId, (m) => ({
          ...m,
          status,
          result,
        })),
      };
    }

    case "skill_invoked": {
      // B-130: heuristic-path detection — the agent didn't go through
      // a tool-call but agent_loop._detect_skill_invocations matched
      // the skill_id / trigger / body keyword to the turn. Render as
      // an inline marker on the assistant bubble so the user SEES the
      // detection without leaving the chat.
      // Tool-call path (evidence='tool_call') already shows up via
      // toolCalls + ToolCard, so skip it here to avoid duplicate UI.
      if ((payload.evidence || "") === "tool_call") return chat;
      const aid = corr;
      const idx = chat.messages.findIndex((m) => m.id === aid);
      if (idx === -1) return chat;
      const note = {
        skill_id: payload.skill_id || "?",
        evidence: payload.evidence || "?",
        trigger_match: payload.trigger_match || null,
        verdict: null,
      };
      return {
        ...chat,
        messages: upsertById(chat.messages, aid, (m) => ({
          ...m,
          skillNotes: (m.skillNotes || []).concat(note),
        })),
      };
    }

    case "skill_outcome": {
      // B-130: pair the verdict back onto the most recent skillNote
      // for the same skill_id on this assistant turn.
      const aid = corr;
      const idx = chat.messages.findIndex((m) => m.id === aid);
      if (idx === -1) return chat;
      const sid = payload.skill_id;
      return {
        ...chat,
        messages: upsertById(chat.messages, aid, (m) => {
          const notes = (m.skillNotes || []).slice();
          // Patch the LAST note with this skill_id (most recent wins).
          for (let i = notes.length - 1; i >= 0; i--) {
            if (notes[i].skill_id === sid && !notes[i].verdict) {
              notes[i] = { ...notes[i], verdict: payload.verdict || "?" };
              break;
            }
          }
          return { ...m, skillNotes: notes };
        }),
      };
    }

    case "cost_tick": {
      // B-107: aggregate per-turn token / cost stats for the live
      // budget widget. Updates a flat ``tokenUsage`` slot on chat
      // state — the UI shows running totals across the session.
      const prev = chat.tokenUsage || {
        prompt_tokens: 0,
        completion_tokens: 0,
        spent_usd: 0,
        budget_usd: 0,
        last_model: "",
        turns: 0,
      };
      const pt = Number(payload.prompt_tokens) || 0;
      const ct = Number(payload.completion_tokens) || 0;
      // ``spent_usd`` is the daemon-side running total — replace,
      // don't sum, so we stay in sync if the daemon resets.
      return {
        ...chat,
        tokenUsage: {
          prompt_tokens: prev.prompt_tokens + pt,
          completion_tokens: prev.completion_tokens + ct,
          spent_usd: typeof payload.spent_usd === "number"
            ? payload.spent_usd : prev.spent_usd,
          budget_usd: typeof payload.budget_usd === "number"
            ? payload.budget_usd : prev.budget_usd,
          last_model: payload.model || prev.last_model,
          turns: prev.turns + 1,
        },
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
