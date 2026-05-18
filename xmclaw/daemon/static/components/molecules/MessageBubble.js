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
  MarkdownBody,
  ThinkingDots,
  PhaseCard,
} from "./MessageBubbleParts.js";
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
// the body by default like the ToolCard does — match peer (CoPaw /
// OpenClaw / Hermes) layout where 💡 Thinking shows as a compact
// row alongside 🔧 tool rows.
function ThinkingRow({ ev }) {
  const body = (ev && ev.content) || "";
  const hasBody = body.trim().length > 0;
  return html`
    <details class="xmc-toolcard xmc-toolcard--ok xmc-toolcard--thinking">
      <summary class="xmc-toolcard__summary">
        <span class="xmc-toolcard__bullet" aria-hidden="true">💡</span>
        <code class="xmc-toolcard__name">Thinking</code>
        ${hasBody
          ? html`<small style="color:var(--xmc-fg-muted);margin-left:.3em">${body.length} chars</small>`
          : null}
      </summary>
      ${hasBody
        ? html`
            <div class="xmc-toolcard__body">
              <div class="xmc-toolcard__section">
                <pre class="xmc-phasecard__thinking-body" style="white-space:pre-wrap;font-size:.85em;line-height:1.5">${body}</pre>
              </div>
            </div>
          `
        : null}
    </details>
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
  // OpenClaw chat-log.ts). The reducer emits each tool invocation
  // as its own message keyed by callId; we just render ToolCard
  // directly. Wrapping <article> keeps it on the same vertical
  // flow as user / assistant bubbles.
  if (message.kind === "tool_use") {
    return html`
      <article class="xmc-msg xmc-msg--assistant xmc-msg--tool-row" data-msg-id=${message.id}>
        <${ToolCard} call=${message} />
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
  const baseLabel = thinking
    ? (phase === "calling_llm" ? "正在调用 LLM" : "正在思考")
    : streaming
    ? hasToolsRunning ? "正在执行工具" : "正在回复"
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
  // B-46: stall detector. A genuine LLM call takes < 60 s for any
  // production model. Past 120 s we're almost certainly stuck (network
  // drop, provider 504, daemon crashed mid-stream). Hint the user.
  const stalled = elapsedS != null && elapsedS > 120;

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
                style="margin-left:auto;background:none;border:none;cursor:pointer;font-size:.9rem;color:var(--xmc-fg-muted);padding:.2rem .4rem;border-radius:4px"
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
      />
      ${role === "assistant" && (message.events || []).length > 0
        ? html`
            <!-- B-218: linear event-stream rendering. Each thinking
                 block + each tool call + each text segment renders
                 as its own row in chronological order, matching the
                 layout in CoPaw / OpenClaw / Hermes screenshots. -->
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
              ? html`<${MarkdownBody} content=${message.content} />`
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
                  <div class="xmc-msg__images">
                    ${message.images.map((src, i) => html`
                      <button
                        key=${i}
                        type="button"
                        class="xmc-msg__image-btn"
                        onClick=${() => openLightbox(src, {
                          alt: `attachment ${i + 1}`,
                          items: message.images,
                          index: i,
                        })}
                        title="点击查看大图"
                      >
                        <img src=${src} alt=${"attachment " + (i + 1)} loading="lazy" class="xmc-msg__image" />
                      </button>
                    `)}
                  </div>
                `
              : null}
            ${Array.isArray(message.videos) && message.videos.length > 0
              ? html`
                  <div class="xmc-msg__videos">
                    ${message.videos.map((src, i) => html`
                      <video key=${"v" + i} src=${src} controls preload="metadata" class="xmc-msg__video" />
                    `)}
                  </div>
                `
              : null}
            ${Array.isArray(message.audios) && message.audios.length > 0
              ? html`
                  <div class="xmc-msg__audios">
                    ${message.audios.map((src, i) => html`
                      <audio key=${"a" + i} src=${src} controls preload="metadata" class="xmc-msg__audio" />
                    `)}
                  </div>
                `
              : null}
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
  const layerLabel = memo.layer === "long_term" ? "长期记忆"
    : memo.layer === "short_term" ? "短期记忆"
    : memo.layer === "working" ? "工作记忆"
    : memo.layer === "procedural" ? "程序记忆"
    : memo.layer || "记忆";
  return html`
    <div
      class="xmc-memory-memo"
      role="note"
      title=${memo.reason ? `为什么记: ${memo.reason}` : "已写入记忆"}
    >
      <span class="xmc-memory-memo__icon" aria-hidden="true">📝</span>
      <${Badge} tone="success">${layerLabel}</${Badge}>
      <span class="xmc-memory-memo__text">${memo.text}</span>
    </div>
  `;
}

// B-130: inline marker for heuristic-path SKILL_INVOKED events that
// did NOT go through a tool-call. Tool-call invocations already render
// via ToolCard (golden tint); this is the substring-match fallback so
// the user still sees in-chat that a skill was detected.
function SkillNote({ note }) {
  const tone = note.verdict === "success" ? "success"
    : note.verdict === "error" ? "error"
    : note.verdict === "partial" ? "warn"
    : note.verdict === "auto_disabled" ? "error"
    : "muted";
  const verdictLabel = note.verdict || "pending";
  return html`
    <div class="xmc-skill-note" title=${`heuristic detection (B-122) · evidence=${note.evidence}`}>
      <span class="xmc-skill-note__icon" aria-hidden="true">⚡</span>
      <span class="xmc-skill-note__label">触发已学技能</span>
      <code class="xmc-skill-note__id">${note.skill_id}</code>
      <${Badge} tone="muted">~ ${note.evidence}</${Badge}>
      <${Badge} tone=${tone}>${verdictLabel}</${Badge}>
      ${note.trigger_match
        ? html`<small style="opacity:.7">trigger: <code>${note.trigger_match}</code></small>`
        : null}
    </div>
  `;
}
