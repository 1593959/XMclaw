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

  // B-130: detect skill tool-calls so the user can SEE in-chat when
  // the agent autonomously picked a skill (vs reaching for a generic
  // bash / file_read). `skill_*` = registered Skill subclass (B-124);
  // `learned_skill_*` = SKILL.md procedure (B-125).
  const isSkillTool = (call.name || "").startsWith("skill_");
  const isLearnedSkill = (call.name || "").startsWith("learned_skill_");
  const isAnySkill = isSkillTool || isLearnedSkill;
  // B-132: detect agent-inter tools (Epic #17) so multi-agent
  // delegations are visually distinct from "bash" or "file_read".
  // The 6 tools agent_inter.py exposes are a fixed set.
  const AGENT_INTER_TOOLS = new Set([
    "list_agents", "chat_with_agent", "submit_to_agent",
    "list_agent_tasks", "stop_agent_task", "check_agent_task",
  ]);
  const isAgentTool = AGENT_INTER_TOOLS.has(call.name);
  const skillLabel = isLearnedSkill ? "📖 已学技能"
    : isSkillTool ? "🎯 注册技能"
    : isAgentTool ? "🤝 子 agent 协作" : "";
  const displayName = isLearnedSkill
    ? call.name.slice("learned_skill_".length)
    : isSkillTool
      ? call.name.slice("skill_".length)
      : call.name;
  // Pull the target agent_id out of args for chat/submit/check/stop
  // so the user sees "→ code_reviewer" inline rather than having to
  // expand the card to read JSON.
  const targetAgent = isAgentTool
    ? (call.args?.agent_id || (call.args?.task_id ? "(by task)" : null))
    : null;

  const cardModifier = isAnySkill ? " xmc-toolcard--skill"
    : isAgentTool ? " xmc-toolcard--agent" : "";
  const bullet = isAnySkill ? "⚡" : isAgentTool ? "🤝" : "●";
  return html`
    <details
      class=${"xmc-toolcard xmc-toolcard--" + call.status + cardModifier}
      open=${openByDefault}
    >
      <summary
        class=${"xmc-toolcard__summary" + (call.status === "running" ? " is-running" : "")}
      >
        <span class="xmc-toolcard__bullet" aria-hidden="true">${bullet}</span>
        ${(isAnySkill || isAgentTool)
          ? html`<${Badge} tone=${isAgentTool ? "warn" : "success"} title=${`${skillLabel} — agent 自主选取的`}>${skillLabel}</${Badge}>`
          : null}
        <code class="xmc-toolcard__name">${displayName}</code>
        ${targetAgent
          ? html`<small style="color:var(--xmc-fg-muted)">→ <code style="font-family:var(--xmc-font-mono)">${targetAgent}</code></small>`
          : null}
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

// B-90: PhaseCard — same <details>-based collapse pattern as ToolCard,
// but for the assistant's "thinking / calling LLM" stages. The dots
// belong on the summary line so the visual signal is consistent
// whether the card is collapsed or expanded. Body shows whatever the
// reducer captured (model / hop / message_count / tools_count from
// LLM_REQUEST) plus thinking content if a future LLM_THINKING_CHUNK
// stream lands one (placeholder slot today).
function PhaseCard({ message, baseLabel, elapsedS, stalled, isWorking }) {
  const phase = message.phase;
  const hasThinkingHistory = !!(message.thinking && message.thinking.length > 0);
  // Card shows in two cases:
  //   1. The turn is still active (isWorking) — live status with dots.
  //   2. The turn has finished BUT a thinking trace was captured —
  //      so the user can click open and review what the model
  //      reasoned through, even after the answer is on screen.
  if (!isWorking && !hasThinkingHistory) return null;
  const meta = message.phaseMeta || null;
  const history = message.phaseHistory || [];
  const tone = stalled ? "warn" : "muted";
  // Auto-expand when stalled — show the user what the call is doing
  // when the spinner has been running uncomfortably long. Don't auto-
  // expand the post-turn review card; that should stay folded by
  // default to keep the transcript readable.
  return html`
    <details class=${"xmc-phasecard xmc-phasecard--" + (phase || "review")} open=${isWorking && stalled}>
      <summary class=${"xmc-phasecard__summary" + (stalled ? " is-stalled" : "")}>
        ${isWorking ? html`
          <span class="xmc-thinking__dot"></span>
          <span class="xmc-thinking__dot"></span>
          <span class="xmc-thinking__dot"></span>
        ` : html`
          <span class="xmc-phasecard__check" aria-hidden="true">▸</span>
        `}
        <span class="xmc-phasecard__label">
          ${isWorking ? baseLabel : "思考过程（已完成）"}
        </span>
        ${elapsedS != null && elapsedS >= 1
          ? html`<${Badge} tone=${tone}>${elapsedS}s</${Badge}>`
          : null}
        ${stalled
          ? html`<span class="xmc-phasecard__warn">· 可能卡住</span>`
          : null}
      </summary>
      <div class="xmc-phasecard__body">
        ${meta ? html`
          <dl class="xmc-phasecard__meta">
            ${meta.model ? html`
              <div class="xmc-phasecard__row">
                <dt>model</dt>
                <dd><code>${meta.model}</code></dd>
              </div>
            ` : null}
            ${meta.llm_profile_id && meta.llm_profile_id !== "default" ? html`
              <div class="xmc-phasecard__row">
                <dt>profile</dt>
                <dd><code>${meta.llm_profile_id}</code></dd>
              </div>
            ` : null}
            ${meta.hop != null ? html`
              <div class="xmc-phasecard__row">
                <dt>hop</dt>
                <dd>第 ${meta.hop} 跳（工具循环里第几次回 LLM）</dd>
              </div>
            ` : null}
            ${meta.messages_count != null ? html`
              <div class="xmc-phasecard__row">
                <dt>历史</dt>
                <dd>${meta.messages_count} 条消息</dd>
              </div>
            ` : null}
            ${meta.tools_count != null ? html`
              <div class="xmc-phasecard__row">
                <dt>可用工具</dt>
                <dd>${meta.tools_count} 个</dd>
              </div>
            ` : null}
          </dl>
        ` : null}
        ${message.thinking ? html`
          <div class="xmc-phasecard__thinking">
            <div class="xmc-phasecard__thinking-label">思考过程</div>
            <pre class="xmc-phasecard__thinking-body">${message.thinking}</pre>
          </div>
        ` : html`
          <div class="xmc-phasecard__hint">
            ${stalled
              ? "若一直卡在这里，去 Trace 页看是否后端真的还在调用，或者 Stop 后重发。"
              : "等待 LLM 第一个 token —— 完整 thinking 内容尚未在事件流里。"}
          </div>
        `}
        ${history.length > 1 ? html`
          <div class="xmc-phasecard__history">
            <div class="xmc-phasecard__thinking-label">本轮 LLM 调用历史</div>
            <ol class="xmc-phasecard__history-list">
              ${history.map((h, i) => html`
                <li key=${i}>
                  hop ${h.hop ?? i} ·
                  <code>${h.model || "?"}</code> ·
                  ${h.messages_count ?? "?"} msgs ·
                  ${h.tools_count ?? "?"} tools
                </li>
              `)}
            </ol>
          </div>
        ` : null}
      </div>
    </details>
  `;
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
      <!-- B-90: phase status now renders as a collapsible PhaseCard with model / hop / depth metadata, replacing the old single-line status header label. -->
      <${PhaseCard}
        message=${message}
        baseLabel=${baseLabel || "正在思考"}
        elapsedS=${elapsedS}
        stalled=${stalled}
        isWorking=${isWorking}
      />
      ${message.content
        ? html`<${MarkdownBody} content=${message.content} />`
        : null}
      ${(message.toolCalls || []).map(
        (call) => html`<${ToolCard} key=${call.id} call=${call} />`
      )}
      ${(message.skillNotes || []).map((note, i) => html`
        <${SkillNote} key=${"sn_" + i} note=${note} />
      `)}
    </article>
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
