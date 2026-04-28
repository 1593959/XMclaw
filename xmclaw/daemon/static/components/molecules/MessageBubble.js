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
import {
  speak,
  stopSpeaking,
  isSpeaking,
  ttsSupported,
  plainTextForTts,
  getAudioPrefs,
} from "../../lib/audio.js";

function ToolCard({ call }) {
  // Hermes ToolCall.tsx pattern: status-tinted card with bullet ●
  // (running/done/error tones), auto-expand on error, user can override.
  // We use <details open> bound to a derived default so error rows
  // open automatically the moment the result arrives.
  const tone =
    call.status === "ok" ? "success" : call.status === "error" ? "error" : "muted";
  const label =
    call.status === "ok" ? "ok" : call.status === "error" ? "error" : "running";
  const argsPreview = (() => {
    try {
      return JSON.stringify(call.args, null, 2);
    } catch (_) {
      return String(call.args);
    }
  })();
  const openByDefault = call.status === "error";
  return html`
    <details
      class=${"xmc-toolcard xmc-toolcard--" + call.status}
      open=${openByDefault}
    >
      <summary
        class=${"xmc-toolcard__summary" + (call.status === "running" ? " is-running" : "")}
      >
        <span class="xmc-toolcard__bullet" aria-hidden="true">●</span>
        <code class="xmc-toolcard__name">${call.name}</code>
        <${Badge} tone=${tone}>${label}</${Badge}>
        ${call.status === "running"
          ? html`<${Spinner} size="sm" label="running" />`
          : null}
      </summary>
      <div class="xmc-toolcard__body">
        <div class="xmc-toolcard__section">
          <div class="xmc-toolcard__label">参数</div>
          <${CodeBlock} code=${argsPreview} lang="json" />
        </div>
        ${call.result != null
          ? html`
              <div class="xmc-toolcard__section">
                <div class="xmc-toolcard__label">${call.status === "error" ? "错误" : "结果"}</div>
                <${CodeBlock}
                  code=${typeof call.result === "string" ? call.result : JSON.stringify(call.result, null, 2)}
                  lang=${call.status === "error" ? "" : "text"}
                />
              </div>
            `
          : null}
      </div>
    </details>
  `;
}

function MarkdownBody({ content }) {
  // Lex once per render; lex itself memoises by source string identity, so
  // re-renders with the same content are O(1). When a new chunk arrives,
  // only the LAST token's html string changes, so Preact's keyed diff
  // touches a single child node — no flicker, no cursor jump.
  const tokens = lex(content || "");
  if (!tokens.length) {
    return html`<div class="xmc-msg__body xmc-md"></div>`;
  }
  return html`
    <div class="xmc-msg__body xmc-md">
      ${tokens.map((tok) => {
        // Intercept code tokens so we can render them through CodeBlock
        // (lang badge + copy button). marked@12 emits {type:"code",
        // text, lang}. Fallback path emits {type:"text"} with raw HTML
        // — let those through unchanged.
        if (tok.type === "code" && typeof tok.text === "string") {
          return html`
            <${CodeBlock}
              key=${tok.idx}
              code=${tok.text}
              lang=${tok.lang || ""}
            />
          `;
        }
        return html`
          <div
            key=${tok.idx}
            data-tok-type=${tok.type || "text"}
            dangerouslySetInnerHTML=${{ __html: renderTokenHtml(tok) }}
          ></div>
        `;
      })}
    </div>
  `;
}

function ThinkingDots({ label = "正在思考" }) {
  return html`
    <div class="xmc-thinking" role="status" aria-live="polite">
      <span class="xmc-thinking__label">${label}</span>
      <span class="xmc-thinking__dot"></span>
      <span class="xmc-thinking__dot"></span>
      <span class="xmc-thinking__dot"></span>
    </div>
  `;
}

export function MessageBubble({ message }) {
  const role = message.role || "system";
  const isUser = role === "user";
  const isSystem = role === "system";
  const thinking = message.status === "thinking";
  const streaming = message.status === "streaming";
  const errored = message.status === "error";

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
    (errored ? " is-error" : "");

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
  const statusLabel = baseLabel
    ? (stalled
        ? `${baseLabel} · ${elapsedS}s · 可能卡住，看 Trace 页`
        : (elapsedS != null && elapsedS >= 1 ? `${baseLabel} · ${elapsedS}s` : baseLabel))
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
        ${message.ultrathink
          ? html`<${Badge} tone="info">ultrathink</${Badge}>`
          : null}
        ${statusLabel
          ? html`<span class="xmc-msg__status">${statusLabel}</span>`
          : null}
        ${streaming && message.content
          ? html`<${Spinner} size="sm" label="streaming" />`
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
      ${showThinking ? html`<${ThinkingDots} label=${statusLabel || "正在思考"} />` : null}
      ${message.content
        ? html`<${MarkdownBody} content=${message.content} />`
        : null}
      ${(message.toolCalls || []).map(
        (call) => html`<${ToolCard} key=${call.id} call=${call} />`
      )}
    </article>
  `;
}
