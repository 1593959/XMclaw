// XMclaw — MessageBubble sub-components (B-323 split).
//
// Lifted out of MessageBubble.js to keep that molecule under the
// 500-line UI budget (FRONTEND_DESIGN.md §1.4). Pure presentation
// pieces — no shared state with the parent beyond props.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// 小巧思：让组件在 active 时每秒重渲染一次，用于「正在思考」实时秒表。
function useTick(active) {
  const [, setN] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => setN((n) => (n + 1) % 100000), 1000);
    return () => clearInterval(id);
  }, [active]);
}

import { lex, renderTokenHtml } from "../../lib/markdown.js";
import { Spinner } from "../atoms/spinner.js";
import { Badge } from "../atoms/badge.js";
import { CodeBlock } from "./CodeBlock.js";
import { resolveMediaTokenInHtml, _resolveMediaUrl } from "../../lib/chat_reducer.js";
import { openLightbox } from "../../lib/lightbox.js";
import { MermaidView, ChartView, SvgView } from "./CanvasArtifact.js";


// 折叠摘要：从工具参数里挑一个最有信息量的值作单行预览（path / command /
// query / url …优先），让折叠态也能一眼看出这次调用在干嘛。
function _argSummary(call) {
  const a = call && call.args;
  if (!a || typeof a !== "object") return "";
  const PREF = ["path", "file", "filename", "command", "cmd", "query", "q",
    "url", "pattern", "text", "name", "prompt", "message", "content", "key", "id"];
  let val = null;
  for (const k of PREF) {
    if (typeof a[k] === "string" && a[k].trim()) { val = a[k]; break; }
  }
  if (val == null) {
    for (const k of Object.keys(a)) {
      const v = a[k];
      if (typeof v === "string" && v.trim()) { val = `${k}=${v}`; break; }
      if (typeof v === "number" || typeof v === "boolean") { val = `${k}=${v}`; break; }
    }
  }
  if (val == null) {
    const n = Object.keys(a).length;
    return n ? `${n} 个参数` : "";
  }
  let s = String(val).replace(/\s+/g, " ").trim();
  if (s.length > 72) s = s.slice(0, 72) + "…";
  return s;
}

// 结果摘要（折叠态显示几行 / 多少字符），点开看全文。
function _resultSummary(call) {
  if (call.result == null) return "";
  const raw = typeof call.result === "string"
    ? call.result
    : JSON.stringify(call.result);
  const lines = String(raw).split("\n").length;
  const chars = String(raw).length;
  return chars > 0 ? `${chars} 字符${lines > 1 ? ` · ${lines} 行` : ""}` : "";
}

export function ToolCard({ call }) {
  // the reference ToolCall.tsx pattern: status-tinted card with bullet ●
  // (running/done/error tones), auto-expand on error, user can override.
  // Nebula v2: flat .nb-toolcard with shimmer on running state.
  const _st = call.status || "running";
  const tone =
    _st === "ok" ? "success" : _st === "error" ? "error" : "muted";
  // 2026-06-06: running 之外的非 ok/error 终态(done — 漏了 finish 事件被
  // 收尾的工具)渲染成中性「已结束」，不再误显示成永久 running。
  const label =
    _st === "ok" ? "ok" : _st === "error" ? "error" : _st === "running" ? "running" : "已结束";
  const statusIcon = _st === "ok" ? "✓"
    : _st === "error" ? "✗"
    : _st === "running" ? null
    : "·";
  const argsPreview = (() => {
    try {
      // B-Canvas: if content is a JSON string (canvas_create/update),
      // pretty-print it inline so the user sees a formatted table spec
      // instead of one escaped wall of text.
      const prettyArgs = { ...(call.args || {}) };
      if (
        (call.name === "canvas_create" || call.name === "canvas_update")
        && typeof prettyArgs.content === "string"
      ) {
        try {
          prettyArgs.content = JSON.parse(prettyArgs.content);
        } catch (_) {
          /* not valid JSON, keep raw */
        }
      }
      // JSON.stringify escapes newlines as \n literals; replace them
      // back so multi-line strings inside JSON values render with
      // actual line breaks in the <pre> block.
      return JSON.stringify(prettyArgs, null, 2).replace(/\\n/g, "\n");
    } catch (_) {
      return String(call.args);
    }
  })();

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

  const hasImages = Array.isArray(call.images) && call.images.length > 0;
  const hasVideos = Array.isArray(call.videos) && call.videos.length > 0;
  const hasAudios = Array.isArray(call.audios) && call.audios.length > 0;
  const hasMedia = hasImages || hasVideos || hasAudios;

  // Shimmer animation bar for running state
  const shimmer = call.status === "running"
    ? html`<div style="position:absolute;top:0;left:0;height:2px;width:30%;background:linear-gradient(90deg,transparent,var(--nb-cyan),transparent);animation:shimmer 1.3s linear infinite;"></div>`
    : null;

  const argSummary = _argSummary(call);
  const resultSummary = _resultSummary(call);
  const durTxt = call.elapsedSeconds != null ? `${call.elapsedSeconds}s`
    : (call.duration_ms != null ? `${(call.duration_ms / 1000).toFixed(1)}s` : null);
  const hasDetail = !!(argsPreview || call.result != null);
  // 默认折叠；出错时自动展开（错误最需要被看到）。
  return html`
    <div class="nb-toolcard-wrap">
      <details class="nb-toolcard" data-kind=${isAgentTool ? "subagent" : "tool"} data-status=${call.status || "running"} open=${call.status === "error"}>
        <summary class="nb-toolcard__summary">
          <span class="nb-toolcard__glyph" aria-hidden="true">${isAnySkill ? "✦" : isAgentTool ? "⤳" : "⌁"}</span>
          <b class="nb-toolcard__name">${displayName}</b>
          ${targetAgent
            ? html`<span class="nb-toolcard__to">→ <code>${targetAgent}</code></span>`
            : null}
          ${(isAnySkill || isAgentTool)
            ? html`<${Badge} tone=${isAgentTool ? "warn" : "success"} title=${`${skillLabel} — agent 自主选取的`}>${skillLabel}</${Badge}>`
            : null}
          ${argSummary ? html`<span class="nb-toolcard__arg">${argSummary}</span>` : null}
          <span class="nb-toolcard__spacer"></span>
          ${call.status === "running"
            ? html`<${Spinner} size="sm" label="running" hideLabel=${true} />`
            : null}
          ${durTxt ? html`<span class="nb-toolcard__dur">${durTxt}</span>` : null}
          <span class="nb-toolcard__state" data-s=${call.status || "running"}>
            ${statusIcon ? html`${statusIcon} ` : null}${label}
          </span>
          ${hasDetail ? html`<span class="nb-toolcard__chev" aria-hidden="true">▸</span>` : null}
        </summary>
        ${hasDetail ? html`
          <div class="nb-toolcard__body">
            ${argsPreview
              ? html`
                  <div class="nb-toolcard__seg">
                    <div class="nb-toolcard__seglabel">参数</div>
                    <${CodeBlock} code=${argsPreview} lang="json" />
                  </div>
                `
              : null}
            ${call.result != null
              ? html`
                  <div class="nb-toolcard__seg">
                    <div class="nb-toolcard__seglabel">${call.status === "error" ? "错误" : "结果"}${resultSummary ? html` <span class="nb-toolcard__segmeta">${resultSummary}</span>` : null}</div>
                    <${CodeBlock}
                      code=${(() => {
                        const raw = typeof call.result === "string"
                          ? call.result
                          : JSON.stringify(call.result, null, 2);
                        return raw.replace(/\\n/g, "\n");
                      })()}
                      lang=${call.status === "error" ? "" : "text"}
                    />
                  </div>
                `
              : null}
          </div>
        ` : null}
      </details>
      ${hasImages
        ? html`<${AttachmentGrid} images=${call.images} />`
        : null}
      ${hasVideos
        ? html`
            <div class="nb-attachment-grid">
              ${call.videos.map((src, i) => html`
                <video key=${"v" + i} src=${src} controls preload="metadata" style="width:100%;height:120px;object-fit:cover;border-radius:var(--nb-radius-md);" />
              `)}
            </div>
          `
        : null}
      ${hasAudios
        ? html`
            <div class="nb-attachment-grid">
              ${call.audios.map((src, i) => html`
                <audio key=${"a" + i} src=${src} controls preload="metadata" style="width:100%;border-radius:var(--nb-radius-md);" />
              `)}
            </div>
          `
        : null}
    </div>
  `;
}

function AttachmentGrid({ images }) {
  return html`
    <div class="nb-attachment-grid">
      ${images.map((src, i) => html`
        <button
          key=${i}
          type="button"
          class="nb-attachment-item"
          onClick=${() => openLightbox(src, {
            alt: `attachment ${i + 1}`,
            items: images,
            index: i,
          })}
          title="点击查看大图"
          aria-label=${`attachment ${i + 1}`}
        >
          <div class="nb-attachment-item__type">IMG</div>
          <img
            src=${src}
            alt=${"attachment " + (i + 1)}
            loading="lazy"
            style="width:100%;height:120px;object-fit:cover;display:block;"
          />
          <div class="nb-attachment-item__name">attachment ${i + 1}</div>
        </button>
      `)}
    </div>
  `;
}

// 2026-06-09: media-first tool rendering. When a tool (send_media,
// screen_capture, etc.) produces images/videos/audios, the media should
// be showcased independently — not buried inside a collapsible tool card.
// Users want to see the video/photo immediately, like a direct message.

export function MediaToolStatus({ call }) {
  const _st = call.status || "running";
  const statusIcon = _st === "ok" ? "✓"
    : _st === "error" ? "✗"
    : _st === "running" ? "◌"
    : "·";
  const durTxt = call.elapsedSeconds != null ? `${call.elapsedSeconds}s`
    : (call.duration_ms != null ? `${(call.duration_ms / 1000).toFixed(1)}s` : null);
  const displayName = call.name || "tool";
  return html`
    <div class="nb-media-status" data-status=${_st}>
      <span class="nb-media-status__icon">${statusIcon}</span>
      <span class="nb-media-status__name">${displayName}</span>
      ${durTxt ? html`<span class="nb-media-status__dur">${durTxt}</span>` : null}
    </div>
  `;
}

export function MediaAttachments({ call }) {
  const hasImages = Array.isArray(call.images) && call.images.length > 0;
  const hasVideos = Array.isArray(call.videos) && call.videos.length > 0;
  const hasAudios = Array.isArray(call.audios) && call.audios.length > 0;
  if (!hasImages && !hasVideos && !hasAudios) return null;

  return html`
    <div class="nb-media-attachments">
      ${hasImages
        ? html`
            <div class="nb-media-images">
              ${call.images.map((src, i) => html`
                <button
                  key=${"mi_" + i}
                  type="button"
                  class="nb-media-image-item"
                  onClick=${() => openLightbox(src, {
                    alt: `image ${i + 1}`,
                    items: call.images,
                    index: i,
                  })}
                >
                  <img src=${src} alt=${"image " + (i + 1)} loading="lazy" />
                </button>
              `)}
            </div>
          `
        : null}
      ${hasVideos
        ? html`
            <div class="nb-media-videos">
              ${call.videos.map((src, i) => html`
                <video
                  key=${"mv_" + i}
                  src=${src}
                  controls
                  preload="metadata"
                  playsinline
                />
              `)}
            </div>
          `
        : null}
      ${hasAudios
        ? html`
            <div class="nb-media-audios">
              ${call.audios.map((src, i) => html`
                <audio
                  key=${"ma_" + i}
                  src=${src}
                  controls
                  preload="metadata"
                />
              `)}
            </div>
          `
        : null}
    </div>
  `;
}


// 2026-05-19: strip server-side fences that agent_loop splices into the
// user message content (memory_ctx, unified_recall, curriculum hint /
// strategies). Designed-out: the daemon attaches these AS the user
// message (not the system prompt) on purpose — keeps the system prompt
// prompt-cache stable. But the chat UI then renders that user message
// to the user, exposing what was meant to be LLM-only side-channel
// context (the "[lesson]…[preference]…" wall the user just hit).
//
// Strip on display, NOT in the reducer / store, so the round-trip with
// the LLM still receives the full context — only the human-facing
// render is filtered.
const _SYSTEM_FENCES = [
  // Audited 2026-05-24 against xmclaw/daemon/agent_loop.py +
  // xmclaw/memory/v2/service.py + xmclaw/cognition/* — these are
  // ALL the XML-ish fences the backend opens in the user message
  // body. Keep this list strictly aligned with the producer side
  // or "[System note: ...]" walls leak into the chat UI again.
  "memory-context",         // legacy memory_manager prefetch
  "memory-recall",          // L1 facts matching current query (agent_loop:1581)
  "memory-v2-facts",        // MemoryService.render_for_prompt durable facts
  "unified-recall",         // cross-session memory recall (legacy)
  "curriculum-hint",        // in-prompt skill nudge
  "curriculum-strategies",  // strategy-bank inject
  "recalled-memory-files",  // relevant file picker (agent_loop:1519)
];
// 2026-06-07: backend nudge prompts (narration_enforcer, B-302 honesty
// guard) are injected as synthetic user messages for the LLM. They
// carry no XML fence, so _FENCE_RE won't catch them. Strip by prefix.
const _HIDDEN_PREFIXES = [
  "已连续 ",                 // narration enforcer (中文)
  "**本回合禁止调用工具**", // narration enforcer strict mode
];
function _stripHiddenPrefixes(s) {
  if (!s || typeof s !== "string") return s || "";
  for (const p of _HIDDEN_PREFIXES) {
    if (s.startsWith(p)) return "";
  }
  // B-302 honesty guard (no prefix, but begins with this specific phrase)
  if (s.startsWith("你刚才说记住了/记下了，但我没有检测到")) return "";
  return s;
}
const _FENCE_RE = new RegExp(
  "\\n*(" + _SYSTEM_FENCES.join("|") + ")\\b[^>]*>[\\s\\S]*?<\\/\\1>\\n*",
  "g",
);
function _stripSystemFences(s) {
  if (!s || typeof s !== "string") return s || "";
  if (s.indexOf("<") === -1) return s;
  return s.replace(_FENCE_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

export function MarkdownBody({ content }) {
  // Lex once per render; lex itself memoises by source string identity, so
  // re-renders with the same content are O(1). When a new chunk arrives,
  // only the LAST token's html string changes, so Preact's keyed diff
  // touches a single child node — no flicker, no cursor jump.
  let cleaned = _stripSystemFences(content || "");
  cleaned = _stripHiddenPrefixes(cleaned);
  const tokens = lex(cleaned);
  if (!tokens.length) {
    return html`<div class="nb-md"></div>`;
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
    <div class="nb-md" onClick=${onClickDelegate}>
      ${tokens.map((tok) => {
        // Intercept code tokens so we can render them through CodeBlock
        // (lang badge + copy button). marked@12 emits {type:"code",
        // text, lang}. Fallback path emits {type:"text"} with raw HTML
        // — let those through unchanged.
        if (tok.type === "code" && typeof tok.text === "string") {
          // Inline visualisation: a fenced block whose language is a
          // visual kind renders AS the visual, right inside the message
          // — no canvas_create tool call, no separate artifact card.
          // The agent just writes ```mermaid / ```chart / ```svg in its
          // normal reply and it shows up rendered.
          const _lang = (tok.lang || "").toLowerCase().trim();
          if (_lang === "mermaid") {
            return html`<${MermaidView} key=${tok.idx} content=${tok.text} />`;
          }
          if (_lang === "chart") {
            return html`<${ChartView} key=${tok.idx} content=${tok.text} />`;
          }
          if (_lang === "svg") {
            return html`<${SvgView} key=${tok.idx} content=${tok.text} />`;
          }
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
              class="nb-md__image"
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
    <div style="display:flex;align-items:center;gap:8px;padding:10px 0;" role="status" aria-live="polite">
      <span style="font-size:12px;color:var(--nb-fg-muted);">${label}</span>
      <div class="nb-typing-indicator" style="padding:0;">
        <span></span>
        <span></span>
        <span></span>
      </div>
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
// Phase 6.4: WorkerCard — renders a parallel worker execution row
// inside the parent session transcript. Compact like ToolCard but
// visually distinct (🐝) so the user sees SWARM parallelism.
export function WorkerCard({ call }) {
  const tone =
    call.status === "ok" ? "success"
    : call.status === "error" ? "error"
    : "muted";
  const label =
    call.status === "ok" ? "完成"
    : call.status === "error" ? "失败"
    : "执行中";
  const bullet = "🐝";
  const hasDetail = !!(call.promptPreview || call.outputPreview || call.error);
  return html`
    <div class="nb-toolcard-wrap">
      <details class="nb-toolcard" data-kind="worker" data-status=${call.status || "running"} open=${call.status === "error"}>
        <summary class="nb-toolcard__summary">
          <span class="nb-toolcard__glyph" aria-hidden="true">${bullet}</span>
          <b class="nb-toolcard__name">worker ${call.workerId || "?"}</b>
          <span class="nb-toolcard__to">task <code>${call.taskId || "?"}</code></span>
          ${call.promptPreview ? html`<span class="nb-toolcard__arg">${call.promptPreview}</span>` : null}
          <span class="nb-toolcard__spacer"></span>
          ${call.status === "running" ? html`<${Spinner} size="sm" label="running" hideLabel=${true} />` : null}
          ${call.elapsedSeconds != null ? html`<span class="nb-toolcard__dur">${call.elapsedSeconds}s</span>` : null}
          <span class="nb-toolcard__state" data-s=${call.status || "running"}>${label}</span>
          ${hasDetail ? html`<span class="nb-toolcard__chev" aria-hidden="true">▸</span>` : null}
        </summary>
        ${hasDetail ? html`
          <div class="nb-toolcard__body">
            ${call.promptPreview ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">任务提示</div><pre class="nb-toolcard__pre">${call.promptPreview}</pre></div>` : null}
            ${call.outputPreview ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">输出预览</div><pre class="nb-toolcard__pre">${call.outputPreview}</pre></div>` : null}
            ${call.error ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">错误</div><${CodeBlock} code=${call.error} lang="" /></div>` : null}
          </div>
        ` : null}
      </details>
    </div>
  `;
}

// 2026-05-25: SubagentCard — renders one ephemeral ``parallel_subagents``
// leaf as an auto-expanded inline card. Sibling of WorkerCard but
// visually distinguished (⚡ ephemeral vs 🐝 long-lived swarm) so the
// user can tell which fanout system fired even after the WorkerSwarm
// auto-dispatch was retired.
export function SubagentCard({ call }) {
  const tone =
    call.status === "ok" ? "success"
    : call.status === "error" ? "error"
    : "muted";
  const label =
    call.status === "ok" ? "完成"
    : call.status === "error" ? "失败"
    : "执行中";
  const bullet = "⚡";
  const idx = call.subagentIndex ?? "?";
  const role = call.role_hint || "general";
  // subagent 产出最有价值 → 完成/出错时默认展开产出；运行中折叠。
  const hasDetail = !!(call.promptPreview || call.outputPreview || call.error);
  const autoOpen = call.status === "error" || (call.status === "ok" && !!call.outputPreview);
  return html`
    <div class="nb-toolcard-wrap">
      <details class="nb-toolcard" data-kind="subagent" data-status=${call.status || "running"} open=${autoOpen}>
        <summary class="nb-toolcard__summary">
          <span class="nb-toolcard__glyph" aria-hidden="true">${bullet}</span>
          <b class="nb-toolcard__name">subagent #${idx}</b>
          <span class="nb-toolcard__to">${role}</span>
          ${call.promptPreview ? html`<span class="nb-toolcard__arg">${call.promptPreview}</span>` : null}
          <span class="nb-toolcard__spacer"></span>
          ${call.status === "running" ? html`<${Spinner} size="sm" label="running" hideLabel=${true} />` : null}
          ${call.hops ? html`<span class="nb-toolcard__dur">${call.hops} hops</span>` : null}
          ${call.elapsedSeconds != null ? html`<span class="nb-toolcard__dur">${call.elapsedSeconds}s</span>` : null}
          <span class="nb-toolcard__state" data-s=${call.status || "running"}>${label}</span>
          ${hasDetail ? html`<span class="nb-toolcard__chev" aria-hidden="true">▸</span>` : null}
        </summary>
        ${hasDetail ? html`
          <div class="nb-toolcard__body">
            ${call.promptPreview ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">子任务</div><pre class="nb-toolcard__pre">${call.promptPreview}</pre></div>` : null}
            ${call.outputPreview ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">产出</div><${MarkdownBody} content=${call.outputPreview} /></div>` : null}
            ${call.error ? html`<div class="nb-toolcard__seg"><div class="nb-toolcard__seglabel">错误</div><${CodeBlock} code=${call.error} lang="" /></div>` : null}
          </div>
        ` : null}
      </details>
    </div>
  `;
}

export function PhaseCard({ message, baseLabel, elapsedS, stalled, isWorking, currentHop }) {
  // 实时秒表：工作中每秒重算，秒数自己往上跳，而不是卡在事件到达的瞬间。
  useTick(isWorking);
  const liveElapsed = isWorking && message.ts
    ? Math.max(0, Math.floor(Date.now() / 1000 - message.ts))
    : elapsedS;
  elapsedS = liveElapsed;
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
