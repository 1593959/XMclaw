// XMclaw — secondary chat-reducer cases (B-323 split).
//
// Lifted out of lib/chat_reducer.js to keep the main reducer file
// under the 500-line UI budget (FRONTEND_DESIGN.md §1.4). The cases
// here are the "non-message-flow" ones — they don't drive the
// streaming-message lifecycle (user_message / llm_request /
// llm_chunk / tool_invocation_*); they pin secondary signals onto
// chat state (cost / grader stats / skill proposals / context
// compression / todos / security alerts).
//
// Contract: ``applySecondaryEvent(chat, envelope) → chat | null``.
// Returns ``null`` when the envelope's type isn't one we handle —
// the caller (chat_reducer.js applyEvent) falls through to its
// own switch.

const HANDLED = new Set([
  "skill_invoked",
  "skill_outcome",
  "cost_tick",
  "anti_req_violation",
  "context_compressed",
  "context_compression_pending",
  "memory_put_auto",
  "todo_updated",
  "grader_verdict",
  "skill_candidate_proposed",
  "skill_promoted",
  "skill_rolled_back",
  "prompt_injection_detected",
  "canvas_artifact_created",
  "canvas_artifact_updated",
  "canvas_artifact_closed",
  "worker_started",
  "worker_completed",
  "worker_failed",
  "subagent_started",
  "subagent_completed",
]);


export function isSecondaryEventType(t) {
  return HANDLED.has(t);
}


export function applySecondaryEvent(chat, envelope, helpers) {
  if (!envelope || typeof envelope !== "object") return null;
  const t = envelope.type;
  if (!HANDLED.has(t)) return null;

  const { upsertById } = helpers;
  const payload = envelope.payload || {};
  const ts = envelope.ts || Date.now() / 1000;
  const corr = envelope.correlation_id || envelope.id || "";

  switch (t) {
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

    case "context_compressed": {
      // B-266: agent_loop's ContextCompressor (proactive or reactive)
      // just shrank the message history. Surface as a thin system
      // bubble so the user knows the LLM saw a summary, not the raw
      // earlier turns. Doesn't change any other state.
      const trigger = payload.trigger || "compressed";
      const id = "ctxcomp_" + corr + "_" + (payload.hop ?? 0);
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "context_compressed",
          content: `🗜️ 上下文被压缩 (${trigger}) — 已尝试将旧内容中的事实持久化到记忆`,
          status: "complete",
          ts,
          collapsed: true,
        }),
      };
    }

    case "context_compression_pending": {
      // Wave 26 fix-4: emitted BEFORE compressor discards content.
      // Memory subsystems subscribe on the backend to extract facts
      // from the doomed slice. On the UI side we don't render a
      // separate bubble (would be noise — context_compressed already
      // shows a bubble right after). Pure no-op for transcript;
      // surfaced in the Trace page via the always-on event log.
      return chat;
    }

    case "memory_put_auto": {
      // Wave 26 fix-4: surface MemoryExtractor writes as an inline
      // 📝 badge attached to the closest preceding assistant bubble
      // (the turn whose user_message+assistant_response triggered the
      // extraction). The user sees "📝 已记忆: <text>" right under
      // the response so they know the agent's claim of memorisation
      // actually persisted.
      const text = (payload.text || "").trim();
      if (!text) return chat;
      const layer = payload.layer || "long_term";
      // Find the most-recent assistant message and append a memo to it.
      const msgs = chat.messages.slice();
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role !== "assistant") continue;
        const memos = Array.isArray(msgs[i].memoryMemos)
          ? msgs[i].memoryMemos
          : [];
        msgs[i] = {
          ...msgs[i],
          memoryMemos: memos.concat({
            id: corr + "_mem_" + memos.length,
            text: text.slice(0, 240),
            layer,
            reason: (payload.reason || "").slice(0, 120),
            ts,
          }),
        };
        return { ...chat, messages: msgs };
      }
      return chat;
    }

    case "todo_updated": {
      // B-266: todo_write tool just updated the task list. Mirror
      // count + items into chat state so a TodoPanel (or inline
      // checklist) can render them without re-fetching.
      const items = Array.isArray(payload.items) ? payload.items : [];
      return {
        ...chat,
        todos: {
          items,
          count: typeof payload.count === "number"
            ? payload.count
            : items.length,
          ts,
        },
      };
    }

    case "grader_verdict": {
      // B-266 + B-294: HonestGrader fired on a tool result. We
      // already surface the per-skill verdict via skill_outcome;
      // this handler aggregates the LATEST verdict score into a
      // session-level slot for diagnostic widgets (e.g. "average
      // grader score this session"). No bubble created — too noisy
      // for chat (every tool call fires one).
      const score = typeof payload.score === "number"
        ? payload.score
        : null;
      const prev = chat.graderStats || { count: 0, sum: 0, lastScore: null };
      return {
        ...chat,
        graderStats: {
          count: prev.count + 1,
          sum: prev.sum + (score == null ? 0 : score),
          lastScore: score,
          lastSkillId: payload.skill_id || null,
          lastTs: ts,
        },
      };
    }

    case "skill_candidate_proposed": {
      // B-266 + B-294: EvolutionAgent.evaluate (now actually firing
      // post-B-294) has emitted a promote/rollback proposal. Attach
      // to a session-level proposals queue so the Evolution page can
      // poll without round-tripping events.db every render. Cap at
      // 50 entries (most-recent first) to keep the slot bounded.
      const decision = payload.decision || "promote";
      const next = {
        ts,
        decision,
        skill_id: payload.skill_id || payload.candidate_id || "?",
        from_version: payload.from_version,
        to_version: payload.to_version,
        evidence: payload.evidence || [],
      };
      const prev = chat.skillProposals || [];
      return {
        ...chat,
        skillProposals: [next, ...prev].slice(0, 50),
      };
    }

    case "skill_promoted":
    case "skill_rolled_back": {
      // B-266: SkillRegistry actually moved HEAD. Strong UI signal —
      // surface as a celebration / warning system bubble so the user
      // sees their agent just learned (or rolled back).
      const promoted = t === "skill_promoted";
      const sid = payload.skill_id || "?";
      const to = payload.to_version ?? "?";
      const id = (promoted ? "promo_" : "roll_") + corr;
      const text = promoted
        ? `🌱 技能 ${sid} 升级到 v${to}`
        : `🔁 技能 ${sid} 回滚到 v${to}`;
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "skill_lifecycle",
          content: text,
          status: "complete",
          ts,
        }),
        // Drop matching proposal from the queue (it's resolved).
        skillProposals: (chat.skillProposals || []).filter(
          (p) => p.skill_id !== sid,
        ),
      };
    }

    case "prompt_injection_detected": {
      // B-266 + B-273: scanner caught injection-shaped content in
      // a tool result / sub-agent reply / channel inbound / skill body.
      // Surface as a warning system bubble. Severity drives the
      // visual emphasis the UI applies (not implemented here — just
      // pass through).
      const severity = payload.severity || "low";
      const source = payload.source || "?";
      const id = "inj_" + corr + "_" + (payload.tool_call_id || "x");
      const findings = Array.isArray(payload.findings)
        ? payload.findings.map((f) => f.pattern_id || "?").join(", ")
        : "";
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "security_alert",
          severity,
          content: `🛡️ Prompt 注入检测 (源: ${source})${findings ? " — " + findings : ""}`,
          status: "complete",
          ts,
        }),
      };
    }

    case "canvas_artifact_created": {
      const turnId = payload.turn_id || corr;
      const idx = chat.messages.findIndex((m) => m.id === turnId);
      if (idx === -1) return chat;
      const artifact = {
        artifact_id: payload.artifact_id,
        kind: payload.kind,
        title: payload.title || "Artifact",
        content: payload.content || "",
        open: true,
      };
      return {
        ...chat,
        messages: upsertById(chat.messages, turnId, (m) => ({
          ...m,
          canvasArtifacts: (m.canvasArtifacts || []).concat(artifact),
        })),
      };
    }

    case "canvas_artifact_updated": {
      const aid = payload.artifact_id;
      return {
        ...chat,
        messages: chat.messages.map((m) => {
          if (!m.canvasArtifacts) return m;
          const arts = m.canvasArtifacts.map((art) =>
            art.artifact_id === aid
              ? { ...art, content: payload.content || art.content }
              : art
          );
          return { ...m, canvasArtifacts: arts };
        }),
      };
    }

    case "canvas_artifact_closed": {
      const aid = payload.artifact_id;
      return {
        ...chat,
        messages: chat.messages.map((m) => {
          if (!m.canvasArtifacts) return m;
          const arts = m.canvasArtifacts.filter((art) => art.artifact_id !== aid);
          return { ...m, canvasArtifacts: arts };
        }),
      };
    }

    case "worker_started": {
      const wid = payload.worker_id || "?";
      const tid = payload.task_id || "?";
      const id = `w_${wid}_${tid}`;
      const exists = chat.messages.some((m) => m.id === id);
      if (exists) return chat;
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "worker",
          content: "",
          status: "running",
          ts,
          workerId: wid,
          taskId: tid,
          promptPreview: (payload.prompt_preview || "").slice(0, 240),
        }),
      };
    }

    case "worker_completed": {
      const wid = payload.worker_id || "?";
      const tid = payload.task_id || "?";
      const id = `w_${wid}_${tid}`;
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        // arrived before started — synthesise finished bubble
        return {
          ...chat,
          messages: chat.messages.concat({
            id,
            role: "system",
            kind: "worker",
            content: "",
            status: "ok",
            ts,
            workerId: wid,
            taskId: tid,
            outputPreview: (payload.output_preview || "").slice(0, 500),
            elapsedSeconds: payload.elapsed_seconds || null,
          }),
        };
      }
      return {
        ...chat,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          status: "ok",
          outputPreview: (payload.output_preview || "").slice(0, 500),
          elapsedSeconds: payload.elapsed_seconds || null,
        })),
      };
    }

    case "subagent_started": {
      const idx = payload.index ?? "?";
      const id = `sub_${ts}_${idx}`;
      const exists = chat.messages.some((m) => m.id === id);
      if (exists) return chat;
      return {
        ...chat,
        messages: chat.messages.concat({
          id,
          role: "system",
          kind: "subagent",
          content: "",
          status: "running",
          ts,
          subagentIndex: idx,
          role_hint: payload.role || "general",
          promptPreview: (payload.subtask || "").slice(0, 240),
          expanded: true,
        }),
      };
    }

    case "subagent_completed": {
      const idx = payload.index ?? "?";
      // Match the most recent running subagent card with this index.
      const candidates = chat.messages
        .filter((m) => m.kind === "subagent" && m.subagentIndex === idx)
        .sort((a, b) => (b.ts || 0) - (a.ts || 0));
      const target = candidates[0];
      const newFields = {
        status: payload.ok ? "ok" : "error",
        outputPreview: (payload.output || "").slice(0, 2000),
        error: (payload.error || "").slice(0, 500),
        hops: payload.hops || 0,
        elapsedSeconds: payload.elapsed_s || null,
        expanded: true,
      };
      if (!target) {
        const id = `sub_${ts}_${idx}`;
        return {
          ...chat,
          messages: chat.messages.concat({
            id,
            role: "system",
            kind: "subagent",
            content: "",
            ts,
            subagentIndex: idx,
            role_hint: payload.role || "general",
            promptPreview: (payload.subtask || "").slice(0, 240),
            ...newFields,
          }),
        };
      }
      return {
        ...chat,
        messages: upsertById(chat.messages, target.id, (m) => ({
          ...m,
          ...newFields,
        })),
      };
    }

    case "worker_failed": {
      const wid = payload.worker_id || "?";
      const tid = payload.task_id || "?";
      const id = `w_${wid}_${tid}`;
      const idx = chat.messages.findIndex((m) => m.id === id);
      if (idx === -1) {
        return {
          ...chat,
          messages: chat.messages.concat({
            id,
            role: "system",
            kind: "worker",
            content: "",
            status: "error",
            ts,
            workerId: wid,
            taskId: tid,
            error: (payload.error || "").slice(0, 500),
          }),
        };
      }
      return {
        ...chat,
        messages: upsertById(chat.messages, id, (m) => ({
          ...m,
          status: "error",
          error: (payload.error || "").slice(0, 500),
        })),
      };
    }

    default:
      return null;
  }
}
