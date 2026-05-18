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
import { resolveMediaTokenInHtml, _resolveMediaUrl } from "../../lib/chat_reducer.js";
import { openLightbox } from "../../lib/lightbox.js";


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
  // Wave 26 fix-3: media (images/videos/audios) renders OUTSIDE the
  // collapsible <details>, so we DON'T need to auto-open the card on
  // media presence anymore — the screenshot is always visible above
  // or below the fold-button. Only auto-open on error so the user
  // sees the failure detail without a click.
  const hasMedia = (
    (Array.isArray(call.images) && call.images.length > 0)
    || (Array.isArray(call.videos) && call.videos.length > 0)
    || (Array.isArray(call.audios) && call.audios.length > 0)
  );

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
  // Wave 26 fix-3: tool-attached media (images/videos/audios) renders
  // OUTSIDE the collapsible <details> so it stays visible even when the
  // user folds away the args/result panel. Previously the user had to
  // keep the "参数" section open to see screenshots — folding hid them.
  const hasImages = Array.isArray(call.images) && call.images.length > 0;
  const hasVideos = Array.isArray(call.videos) && call.videos.length > 0;
  const hasAudios = Array.isArray(call.audios) && call.audios.length > 0;
  return html`
    <div class=${"xmc-toolcard-wrap" + (hasMedia ? " has-media" : "")}>
      <details
        class=${"xmc-toolcard xmc-toolcard--" + call.status + cardModifier}
        open=${call.status === "error"}
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
        </div>
      </details>
      ${hasImages
        ? html`<${ToolMediaImages} images=${call.images} />`
        : null}
      ${hasVideos
        ? html`
            <div class="xmc-toolcard__media">
              ${call.videos.map((src, i) => html`
                <video key=${"v" + i} src=${src} controls preload="metadata" class="xmc-toolcard__video" />
              `)}
            </div>
          `
        : null}
      ${hasAudios
        ? html`
            <div class="xmc-toolcard__media">
              ${call.audios.map((src, i) => html`
                <audio key=${"a" + i} src=${src} controls preload="metadata" class="xmc-toolcard__audio" />
              `)}
            </div>
          `
        : null}
    </div>
  `;
}

// Wave 26 fix-3: tool-screenshot gallery rendered OUTSIDE the
// collapsible card. Clicking opens the in-app lightbox (no tab
// switch, no scroll loss) and the row exposes the whole image list
// so left/right arrow keys can flip between thumbnails.
function ToolMediaImages({ images }) {
  return html`
    <div class="xmc-toolcard__media">
      ${images.map((src, i) => html`
        <button
          key=${i}
          type="button"
          class="xmc-toolcard__media-btn"
          onClick=${() => openLightbox(src, {
            alt: `tool image ${i + 1}`,
            items: images,
            index: i,
          })}
          title="点击查看大图"
          aria-label=${`tool image ${i + 1}`}
        >
          <img
            src=${src}
            alt=${"tool image " + (i + 1)}
            loading="lazy"
            class="xmc-toolcard__media-img"
          />
        </button>
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
  // Wave 26 fix-3: click delegation for inline markdown images. The
  // '<img>' lives inside a sanitized HTML blob (we can't attach an
  // onclick in the source string — DOMPurify would strip it), so we
  // listen on the wrapper and dispatch openLightbox when the click
  // target is an image.
  const onClickDelegate = (e) => {
    const t = e.target;
    if (t && t.tagName === "IMG" && t.src) {
      e.preventDefault();
      openLightbox(t.src, { alt: t.alt || "" });
    }
  };
  return html`
    <div class="xmc-msg__body xmc-md" onClick=${onClickDelegate}>
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
        // Defensive: marked@12 wraps standalone images in <p>, but a
        // future version might emit them at the top level. Render as
        // a real <img> with the URL passed through _resolveMediaUrl.
        // The delegated click handler above opens the lightbox on
        // click — no <a target="_blank">, no tab switch.
        if (tok.type === "image" && typeof tok.href === "string") {
          const src = _resolveMediaUrl(tok.href);
          const alt = tok.text || "";
          return html`
            <img
              key=${tok.idx}
              src=${src}
              alt=${alt}
              loading="lazy"
              class="xmc-md__image"
              title=${tok.title || alt || "点击查看大图"}
            />
          `;
        }
        // For all other tokens, run a post-pass on the sanitized HTML so
        // inline images (``Look: ![alt](/api/v2/media/x.png)`` inside a
        // paragraph) also get the auth token appended to their src.
        return html`
          <div
            key=${tok.idx}
            data-tok-type=${tok.type || "text"}
            dangerouslySetInnerHTML=${{
              __html: resolveMediaTokenInHtml(renderTokenHtml(tok)),
            }}
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
export function PhaseCard({ message, baseLabel, elapsedS, stalled, isWorking, currentHop }) {
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
        ${isWorking && currentHop != null && currentHop > 0
          ? html`<${Badge} tone="info">hop ${currentHop}</${Badge}>`
          : null}
        ${elapsedS != null && elapsedS >= 1
          ? html`<${Badge} tone=${tone}>${elapsedS}s</${Badge}>`
          : null}
        ${stalled
          ? html`<span class="xmc-phasecard__warn">· 可能卡住，点击展开查看</span>`
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
