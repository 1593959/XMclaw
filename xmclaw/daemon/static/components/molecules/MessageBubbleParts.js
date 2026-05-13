// XMclaw — MessageBubble sub-components (B-323 split).
//
// Lifted out of MessageBubble.js to keep that molecule under the
// 500-line UI budget (FRONTEND_DESIGN.md §1.4). Pure presentation
// pieces — no shared state with the parent beyond props.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { lex, renderTokenHtml } from "../../lib/markdown.js";
import { Spinner } from "../atoms/spinner.js";
import { Badge } from "../atoms/badge.js";
import { CodeBlock } from "./CodeBlock.js";


export function ToolCard({ call }) {
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
  // `learned_skill_*` = SKILL.md procedure (B-125); `skill_browse`
  // (B-299) is the synthesised meta-discovery tool — visually
  // distinct so the user sees "agent is browsing the catalog" vs
  // "agent invoked a real skill".
  const isBrowseMeta = call.name === "skill_browse";
  const isSkillTool = !isBrowseMeta && (call.name || "").startsWith("skill_");
  const isLearnedSkill = (call.name || "").startsWith("learned_skill_");
  const isAnySkill = isSkillTool || isLearnedSkill || isBrowseMeta;
  // B-132: detect agent-inter tools (Epic #17) so multi-agent
  // delegations are visually distinct from "bash" or "file_read".
  // The 6 tools agent_inter.py exposes are a fixed set.
  const AGENT_INTER_TOOLS = new Set([
    "list_agents", "chat_with_agent", "submit_to_agent",
    "list_agent_tasks", "stop_agent_task", "check_agent_task",
  ]);
  const isAgentTool = AGENT_INTER_TOOLS.has(call.name);
  const skillLabel = isBrowseMeta ? "🔍 技能发现"
    : isLearnedSkill ? "📖 已学技能"
    : isSkillTool ? "🎯 注册技能"
    : isAgentTool ? "🤝 子 agent 协作" : "";
  const displayName = isBrowseMeta
    ? "browse"
    : isLearnedSkill
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
          ? html`<${Spinner} size="sm" label="running" hideLabel=${true} />`
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
        ${Array.isArray(call.images) && call.images.length > 0
          ? html`
              <div class="xmc-toolcard__section">
                <div class="xmc-toolcard__label">附图 (${call.images.length})</div>
                <${ToolImageGallery} images=${call.images} />
              </div>
            `
          : null}
      </div>
    </details>
  `;
}


// B-MULTIMODAL-UI: render image attachments from tool results
// (screen_capture / image_read / camera_capture / gui_send_chat
// confirm screenshots). Each image is fetched from
// /api/v2/media/<filename>?token=<pairing> so we attach the
// pairing token from the current location.
function ToolImageGallery({ images }) {
  const token = (() => {
    try {
      const url = new URL(window.location.href);
      return url.searchParams.get("token") || "";
    } catch (_e) {
      return "";
    }
  })();
  function _url(u) {
    if (!u) return "";
    if (u.startsWith("http") || u.startsWith("data:")) return u;
    if (u.includes("?")) return u + (token ? "&token=" + encodeURIComponent(token) : "");
    return u + (token ? "?token=" + encodeURIComponent(token) : "");
  }
  return html`
    <div class="xmc-toolcard__images">
      ${images.map((src, i) => html`
        <a
          key=${i}
          href=${_url(src)}
          target="_blank"
          rel="noopener"
          class="xmc-toolcard__image-link"
          title="点击放大查看"
        >
          <img
            src=${_url(src)}
            alt="tool image ${i + 1}"
            loading="lazy"
            class="xmc-toolcard__image"
          />
        </a>
      `)}
    </div>
  `;
}


export function MarkdownBody({ content }) {
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


export function ThinkingDots({ label = "正在思考" }) {
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
export function PhaseCard({ message, baseLabel, elapsedS, stalled, isWorking }) {
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
              : isWorking
                ? "正在生成回复…"
                : "本轮模型未提供独立 reasoning 流（meta 信息见上）。"}
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
