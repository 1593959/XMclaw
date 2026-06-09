// XMclaw — MessageBubble
//
// One row in the chat transcript. Renders user / assistant / system / tool
// content with a stable id, role-based visual treatment, and streaming
// indicator when status === "streaming".
//
// Markdown is rendered token-by-token via lib/markdown.js. The keyed map
// over `tokens` lets Preact's reconciler update only the trailing token
// when a new LLM_CHUNK arrives — no full bubble repaint, no cursor jump.
// This mirrors open-webui's `Markdown.svelte` + cline's `MarkdownBlock.tsx`
// memoization pattern.
//
// Tool cards are user-controllable `<details>` with a live shimmer on the
// summary line while running (open-webui `ToolCallDisplay.svelte:127-138`).
// Auto-collapsing on completion (the prior behavior) hid the result the
// moment it arrived — opposite of what the user wants.

const { h } = window.__xmc.preact;
const { useEffect, useRef, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { lex, renderTokenHtml } from "../../lib/markdown.js";
import { Spinner } from "../atoms/spinner.js";
import { Badge } from "../atoms/badge.js";
import { CodeBlock } from "./CodeBlock.js";
import { QuestionCard } from "./QuestionCard.js";
// B-323: ToolCard / MarkdownBody / ThinkingDots / PhaseCard split out
// to MessageBubbleParts.js so this file stays under the 500-line UI
// budget (FRONTEND_DESIGN.md §1.4). Same render contract — pure
// presentation pieces, no shared mutable state.
import {
  ToolCard,
  WorkerCard,
  SubagentCard,
  MarkdownBody,
  ThinkingDots,
  PhaseCard,
  MediaToolStatus,
  MediaAttachments,
} from "./MessageBubbleParts.js";
import { CanvasArtifact } from "./CanvasArtifact.js";
import {
  speak,
  stopSpeaking,
  isSpeaking,
  ttsSupported,
  plainTextForTts,
  getAudioPrefs,
} from "../../lib/audio.js";
import { openLightbox } from "../../lib/lightbox.js";

// B-218: per-block Thinking row. One row per continuous thinking
// segment (terminated by a tool call or text emission). Collapses
// the body by default like the ToolCard does — match peer (comparable agents /
// comparable agents) layout where 💡 Thinking shows as a compact
// row alongside 🔧 tool rows.
function ThinkingRow({ ev }) {
  const body = (ev && ev.content) || "";
  const hasBody = body.trim().length > 0;
  return html`
    <div class="nb-toolcard" style="position:relative;">
      <div class="nb-toolcard__header">
        <span aria-hidden="true">💡</span>
        <b>Thinking</b>
        ${hasBody
          ? html`<span style="color:var(--nb-fg-muted);margin-left:.3em;font-size:11px;">${body.length} chars</span>`
          : null}
        <span class="ok" style="margin-left:auto;color:var(--nb-success);font-weight:600;font-size:11px;">✓</span>
      </div>
      ${hasBody
        ? html`
            <div class="nb-toolcard__body">
              <pre style="white-space:pre-wrap;font-size:.85em;line-height:1.5;margin:0;">${body}</pre>
            </div>
          `
        : null}
    </div>
  `;
}


// B-207: silent-bubble placeholder. When MiniMax / Qwen / GLM emit a
// tool-call response with empty content (the model variant for
// intermediate hops doesn't narrate between tool calls — we ship a
// system-prompt rule about this in B-206 but the model isn't always
// compliant), the assistant bubble would render with NOTHING above
// the tool cards. That visually breaks the conversation flow.
//
// This soft fallback synthesises a one-line italic "调用了 X" label
// so the UI is never silent — but only AFTER the bubble is settled
// (status=complete + tools no longer running). While streaming /
// running we let the spinner / tool card phase do the work.
function _shouldShowSilentBubblePlaceholder(message) {
  if (message.status !== "complete") return false;
  if (message.content) return false;
  const tcs = message.toolCalls || [];
  if (tcs.length === 0) return false;
  // Don't show during the brief moment a tool is still running —
  // the tool card itself already has a spinner.
  if (tcs.some((c) => c.status === "running")) return false;
  return true;
}

function _silentBubbleLabel(toolCalls) {
  const names = (toolCalls || [])
    .map((c) => c.name || "tool")
    .filter((n, i, arr) => arr.indexOf(n) === i);  // dedup
  if (names.length === 0) return "(调用了工具)";
  if (names.length === 1) return `(直接调用了 ${names[0]})`;
  if (names.length <= 3) return `(直接调用了 ${names.join(" / ")})`;
  return `(直接调用了 ${names.slice(0, 3).join(" / ")} 等 ${names.length} 个工具)`;
}

// Wave-33: character threshold beyond which an assistant message is
// initially truncated with an "expand" affordance.
const _MSG_TRUNCATE_CHARS = 3000;

function _truncateAtBoundary(text, maxChars) {
  if (!text || text.length <= maxChars) return { truncated: text, wasCut: false };
  // Try to find a paragraph break (double newline) before the limit.
  let cut = text.lastIndexOf("\n\n", maxChars);
  if (cut < maxChars * 0.5) {
    // No good paragraph break nearby — try a single newline.
    cut = text.lastIndexOf("\n", maxChars);
  }
  if (cut < 0 || cut < maxChars * 0.3) {
    // Hard break at maxChars.
    cut = maxChars;
  }
  return { truncated: text.slice(0, cut), wasCut: true };
}

export function MessageBubble({ message, onAnswerQuestion }) {
  // B-92: question-kind bubbles render as QuestionCard, not as
  // markdown text. The reducer creates these on AGENT_ASKED_QUESTION
  // events. They sit in the transcript like any other message so the
  // back-scroll history shows what was asked + the answer.
  if (message.kind === "question") {
    return html`
      <article class="xmc-msg xmc-msg--system" data-msg-id=${message.id}>
        <${QuestionCard}
          message=${message}
          onAnswerQuestion=${onAnswerQuestion}
        />
      </article>
    `;
  }

  // B-220: tool_use is a top-level sibling row (peer pattern from
  // the standard chat-log). The reducer emits each tool invocation
  // as its own message keyed by callId; we just render ToolCard
  // directly. Wrapping <article> keeps it on the same vertical
  // flow as user / assistant bubbles.
  if (message.kind === "tool_use") {
    // 2026-06-09: media-first rendering. Tools that produce images/videos/
    // audios (send_media, screen_capture, etc.) should showcase the media
    // independently — not buried inside a collapsible card.
    const hasMedia = (
      (Array.isArray(message.images) && message.images.length > 0) ||
      (Array.isArray(message.videos) && message.videos.length > 0) ||
      (Array.isArray(message.audios) && message.audios.length > 0)
    );
    if (hasMedia) {
      return html`
        <article class="xmc-msg xmc-msg--assistant xmc-msg--media-row" data-msg-id=${message.id}>
          <${MediaToolStatus} call=${message} />
          <${MediaAttachments} call=${message} />
        </article>
      `;
    }
    return html`
      <article class="xmc-msg xmc-msg--assistant xmc-msg--tool-row" data-msg-id=${message.id}>
        <${ToolCard} call=${message} />
      </article>
    `;
  }

  // Phase 6.4: worker execution card rendered inline in the parent
  // session transcript so the user sees parallel SWARM progress.
  if (message.kind === "worker") {
    return html`
      <article class="xmc-msg xmc-msg--system xmc-msg--tool-row" data-msg-id=${message.id}>
        <${WorkerCard} call=${message} />
      </article>
    `;
  }

  // 2026-05-25: subagent card — ephemeral parallel_subagents fanout leaf.
  // Auto-expanded so the user sees the per-leaf output without clicking.
  if (message.kind === "subagent") {
    return html`
      <article class="xmc-msg xmc-msg--system xmc-msg--tool-row" data-msg-id=${message.id}>
        <${SubagentCard} call=${message} />
      </article>
    `;
  }

  // B-224: hide empty assistant bubbles. When a hop is tool-only
  // (no text content streamed, B-220 moved toolCalls into sibling
  // tool_use messages), the placeholder bubble created by
  // llm_request → llm_response was left with content="" + empty
  // toolCalls + no thinking — but its header still rendered
  // "assistant", so a 50-hop turn produced 50 stacked "assistant"
  // labels in the transcript with nothing under them. Now we
  // collapse such ghost bubbles entirely. Streaming bubbles
  // (in-flight) still render so the user sees the spinner.
  if (
    (message.role === "assistant" || !message.role)
    && !message.kind
    && message.status !== "streaming"
    && message.status !== "thinking"
    && !(message.content && String(message.content).trim())
    && !(message.thinking && String(message.thinking).trim())
    && !((message.toolCalls || []).length)
  ) {
    return null;
  }
  const role = message.role || "system";
  const isUser = role === "user";
  const isSystem = role === "system";
  const thinking = message.status === "thinking";
  const streaming = message.status === "streaming";
  const errored = message.status === "error";
  // Wave-32+ UX fix: when the user clicks Stop the bubble's status
  // flips to "cancelled" — render same as errored but with its own
  // marker so the user sees WHY this turn stopped early.
  const cancelled = message.status === "cancelled";

  // ── TTS auto-speak (B-20) ────────────────────────────────────────
  // When the assistant's turn finalizes (status=complete) and the
  // user has enabled auto-speak, read the message aloud. Per-message
  // 🔊 button lets them replay/start manually too. We track a local
  // playing state purely for the button's visual feedback.
  const [playing, setPlaying] = useState(false);
  const [msgExpanded, setMsgExpanded] = useState(false);
  const spokenRef = useRef(false);

  useEffect(() => {
    if (!ttsSupported) return;
    if (role !== "assistant") return;
    if (message.status !== "complete") return;
    if (spokenRef.current) return;
    const prefs = getAudioPrefs();
    if (!prefs.autoSpeak) return;
    const txt = plainTextForTts(message.content);
    if (!txt) return;
    spokenRef.current = true;
    setPlaying(true);
    speak(txt, {
      onEnd: () => setPlaying(false),
      onError: () => setPlaying(false),
    });
  }, [role, message.status, message.content]);

  const onTogglePlay = () => {
    if (playing || isSpeaking()) {
      stopSpeaking();
      setPlaying(false);
      return;
    }
    const txt = plainTextForTts(message.content);
    if (!txt) return;
    setPlaying(true);
    speak(txt, {
      onEnd: () => setPlaying(false),
      onError: () => setPlaying(false),
    });
  };
  // A streaming bubble that has tool calls running but no LLM text yet
  // counts as "working" — show the thinking dots even if the reducer
  // already moved it past the thinking phase.
  const hasToolsRunning = (message.toolCalls || []).some(
    (c) => c.status === "running",
  );
  const showThinking =
    thinking ||
    (streaming && !message.content && !hasToolsRunning);

  const cls =
    "xmc-msg xmc-msg--" +
    role +
    (thinking ? " is-thinking" : "") +
    (streaming ? " is-streaming" : "") +
    (errored ? " is-error" : "") +
    (cancelled ? " is-cancelled" : "");

  // B-43: phase-aware label. The reducer now sets message.phase to
  // 'calling_llm' on llm_request and 'tool_running' on
  // tool_invocation_started, so a slow LLM call no longer looks like a
  // hung "正在思考". Plus a live elapsed-second counter for any bubble
  // still working (re-renders every 500ms via the tick state below).
  const phase = message.phase;
  // Wave-32+ status clarity: surface the current running tool name
  // in the label so "正在执行工具" becomes "browser_fill · 4s" — the
  // user can tell WHAT is taking time, not just THAT something is.
  const runningTool = (message.toolCalls || []).find(
    (c) => c.status === "running",
  );
  const runningToolName = runningTool ? runningTool.name : null;
  const baseLabel = thinking
    ? (phase === "calling_llm" ? "正在调用 LLM" : "正在思考")
    : streaming
    ? hasToolsRunning
      ? runningToolName
        ? `正在执行 ${runningToolName}`
        : "正在执行工具"
      : "正在回复"
    : null;

  const isWorking = thinking || streaming;
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!isWorking) return;
    const id = setInterval(() => setTick((n) => n + 1), 500);
    return () => clearInterval(id);
  }, [isWorking]);

  const elapsedS = isWorking && message.ts
    ? Math.max(0, Math.floor(Date.now() / 1000 - message.ts))
    : null;
  // B-46 / Wave-32+ status clarity: stall detector.
  // 2026-05-24: bumped 30 s → 90 s. User report — 30 s was triggering
  // "可能卡住" mid-hop for normal reasoning models (Kimi K2.6 on hop
  // 9 with deep context regularly takes 60-80 s for a single LLM
  // call; that's NOT stalled). 30 s came from "typical LLM under
  // 30 s" assumption which doesn't hold for reasoning models +
  // long context. 90 s lets normal long-thought hops through but
  // still surfaces a true freeze before the user gives up. The
  // warning is non-destructive (auto-opens PhaseCard + hint, no
  // cancel) so eagerness costs only attention.
  const stalled = elapsedS != null && elapsedS > 90;
  // Hop badge — pulled from the most recent phase meta. When the
  // agent is mid-tool-loop, this jumps from 1 → 2 → 3 so the user
  // can SEE progress even when individual hops fail.
  const currentHop = message.phaseMeta && message.phaseMeta.hop != null
    ? message.phaseMeta.hop
    : null;

  return html`
    <article
      class=${cls}
      data-msg-id=${message.id}
      data-role=${role}
      role=${isSystem ? "alert" : "article"}
      aria-busy=${thinking || streaming ? "true" : "false"}
    >
      <header class="xmc-msg__header">
        <span class="xmc-msg__role">${isUser ? "you" : isSystem ? "system" : "assistant"}</span>
        ${message.proactive
          ? html`<${Badge} tone="warn">主动 · ${message.proactiveTrigger || "trigger"}</${Badge}>`
          : null}
        ${message.ultrathink
          ? html`<${Badge} tone="info">ultrathink</${Badge}>`
          : null}
        ${streaming && message.content
          ? html`<${Spinner} size="sm" label="streaming" />`
          : null}
        ${cancelled
          ? html`<${Badge} tone="warn">已停止</${Badge}>`
          : null}
        ${role === "assistant" && message.status === "complete" && ttsSupported && message.content
          ? html`
              <button
                type="button"
                class="xmc-msg__tts"
                onClick=${onTogglePlay}
                aria-pressed=${playing ? "true" : "false"}
                title=${playing ? "停止朗读" : "朗读这条回复"}
                style="margin-left:auto;background:none;border:none;cursor:pointer;font-size:.9rem;color:var(--nb-fg-muted);padding:.2rem .4rem;border-radius:4px"
              >${playing ? "⏹" : "🔊"}</button>
            `
          : null}
      </header>
      <!-- B-90: phase status now renders as a collapsible PhaseCard with model / hop / depth metadata, replacing the old single-line status header label. -->
      <${PhaseCard}
        message=${message}
        baseLabel=${baseLabel || "正在思考"}
        elapsedS=${elapsedS}
        stalled=${stalled}
        isWorking=${isWorking}
        currentHop=${currentHop}
      />
      ${role === "assistant" && (message.events || []).length > 0
        ? html`
            <!-- B-218: linear event-stream rendering. Each thinking
                 block + each tool call + each text segment renders
                 as its own row in chronological order, matching the
                 layout in comparable agents screenshots. -->
            ${(message.events || []).map((ev) => {
              if (ev.type === "thinking") {
                return html`<${ThinkingRow} key=${ev.id} ev=${ev} />`;
              }
              if (ev.type === "tool") {
                return html`<${ToolCard} key=${ev.id} call=${ev} />`;
              }
              // text
              return html`<${MarkdownBody} key=${ev.id} content=${ev.content || ""} />`;
            })}
          `
        : html`
            <!-- legacy back-compat path: content + toolCalls aggregates -->
            ${message.content
              ? (() => {
                  const isAssistant = role === "assistant";
                  const shouldTrunc = isAssistant && !streaming && !msgExpanded;
                  const { truncated, wasCut } = shouldTrunc
                    ? _truncateAtBoundary(message.content, _MSG_TRUNCATE_CHARS)
                    : { truncated: message.content, wasCut: false };
                  return html`
                    <${MarkdownBody} content=${truncated} />
                    ${wasCut
                      ? html`
                          <button
                            type="button"
                            class="xmc-msg__expand"
                            onClick=${() => setMsgExpanded(true)}
                          >… 展开剩余内容 (${message.content.length - truncated.length} 字符)</button>
                        `
                      : null}
                  `;
                })()
              : (role === "assistant" && thinking
                  ? html`<div class="xmc-msg__placeholder" style="opacity:.65;font-size:.85em">🌸 收到啦，正在思考中...</div>`
                  : (role === "assistant"
                      && (message.toolCalls || []).length > 0
                      && _shouldShowSilentBubblePlaceholder(message)
                      ? html`<div class="xmc-msg__placeholder xmc-msg__placeholder--silent" style="opacity:.55;font-size:.85em;font-style:italic">${_silentBubbleLabel(message.toolCalls)}</div>`
                      : null))}
            ${(message.toolCalls || []).map(
              (call) => html`<${ToolCard} key=${call.id} call=${call} />`
            )}
            ${Array.isArray(message.images) && message.images.length > 0
              ? html`
                  <div class="nb-attachment-grid">
                    ${message.images.map((src, i) => html`
                      <button
                        key=${i}
                        type="button"
                        class="nb-attachment-item"
                        onClick=${() => openLightbox(src, {
                          alt: `attachment ${i + 1}`,
                          items: message.images,
                          index: i,
                        })}
                        title="点击查看大图"
                      >
                        <div class="nb-attachment-item__type">IMG</div>
                        <img src=${src} alt=${"attachment " + (i + 1)} loading="lazy" style="width:100%;height:120px;object-fit:cover;display:block;" />
                        <div class="nb-attachment-item__name">attachment ${i + 1}</div>
                      </button>
                    `)}
                  </div>
                `
              : null}
            ${Array.isArray(message.videos) && message.videos.length > 0
              ? html`
                  <div class="nb-attachment-grid">
                    ${message.videos.map((src, i) => html`
                      <video key=${"v" + i} src=${src} controls preload="metadata" style="width:100%;height:120px;object-fit:cover;border-radius:var(--nb-radius-md);" />
                    `)}
                  </div>
                `
              : null}
            ${Array.isArray(message.audios) && message.audios.length > 0
              ? html`
                  <div style="display:flex;flex-direction:column;gap:8px;margin:10px 0;">
                    ${message.audios.map((src, i) => html`
                      <audio key=${"a" + i} src=${src} controls preload="metadata" style="width:100%;border-radius:var(--nb-radius-md);" />
                    `)}
                  </div>
                `
              : null}
            ${(message.canvasArtifacts || []).map(
              (art) => html`<${CanvasArtifact} key=${art.artifact_id} artifact=${art} />`
            )}
          `}
      ${(message.skillNotes || []).map((note, i) => html`
        <${SkillNote} key=${"sn_" + i} note=${note} />
      `)}
      ${(message.memoryMemos || []).map((memo) => html`
        <${MemoryMemo} key=${memo.id} memo=${memo} />
      `)}
    </article>
  `;
}

// Wave 26 fix-4: inline 📝 badge that surfaces a MEMORY_PUT_AUTO write
// next to the assistant bubble that triggered it. Closes the
// "他说他记住了，结果一压缩啥都不知道" pain point — the persistence
// now happens AND is visible.
function MemoryMemo({ memo }) {
  const meta = {
    long_term:  { label: "长期记忆", icon: "🧠" },
    short_term: { label: "短期记忆", icon: "⏳" },
    working:    { label: "工作记忆", icon: "✦" },
    procedural: { label: "程序记忆", icon: "⚙" },
  }[memo.layer] || { label: memo.layer || "记忆", icon: "📝" };
  return html`
    <div
      class="nb-memory-memo"
      data-layer=${memo.layer || "other"}
      role="note"
      title=${memo.reason ? `为什么记: ${memo.reason}` : "已写入记忆 — 下次还记得"}
    >
      <span class="nb-memory-memo__spark" aria-hidden="true">${meta.icon}</span>
      <span class="nb-memory-memo__layer">${meta.label}</span>
      <span class="nb-memory-memo__text">${memo.text}</span>
      <span class="nb-memory-memo__tick" aria-hidden="true">✓ 已记住</span>
    </div>
  `;
}

// B-130: inline marker for heuristic-path SKILL_INVOKED events that
// did NOT go through a tool-call. Tool-call invocations already render
// via ToolCard (golden tint); this is the substring-match fallback so
// the user still sees in-chat that a skill was detected.
function SkillNote({ note }) {
  const verdict = note.verdict === "auto_disabled" ? "error" : (note.verdict || "pending");
  const verdictLabel = note.verdict || "pending";
  return html`
    <div
      class="nb-skill-note"
      data-verdict=${verdict}
      title=${`heuristic detection (B-122) · evidence=${note.evidence}`}
    >
      <span class="nb-skill-note__spark" aria-hidden="true">⚡</span>
      <span class="nb-skill-note__label">触发技能</span>
      <code class="nb-skill-note__id">${note.skill_id}</code>
      <span class="nb-skill-note__ev">~ ${note.evidence}</span>
      <span class="nb-skill-note__verdict">${verdictLabel}</span>
      ${note.trigger_match
        ? html`<small class="nb-skill-note__trig">trigger: <code>${note.trigger_match}</code></small>`
        : null}
    </div>
  `;
}
