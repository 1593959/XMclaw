// Worker A — Nebula UI migration (2026-06-05)
// Replaced xmc-* classes with nb-* prefix per Nebula Design System v2.
// Message stream, bubble structure, and interactions synced from nebula-prototype.html.
// Data flow (props / store / API) unchanged; pure UI rendering update.

const { h, Component } = window.__xmc.preact;
const { useState, useEffect, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import {
  MarkdownBody,
  ToolCard,
  PhaseCard,
  WorkerCard,
  SubagentCard,
} from "./MessageBubbleParts.js";
import { QuestionCard } from "./QuestionCard.js";
// Phase 9 M1: canvas artifact 渲染原来只活在没人 import 的 MessageBubble.js
// (nebula 改版时漏迁=断链,canvas_create 的产物从未在现役 UI 显示过),
// 这里接回,并带上 postMessage 回传桥的 onCanvasAction。
import { CanvasArtifact } from "./CanvasArtifact.js";
import { openLightbox } from "../../lib/lightbox.js";

// B-220: per-bubble error boundary.
class BubbleBoundary extends Component {
  constructor() {
    super();
    this.state = { err: null };
  }
  componentDidCatch(err) {
    // eslint-disable-next-line no-console
    console.error("[xmc] MessageBubble crash:", err);
    this.setState({ err });
  }
  render() {
    if (this.state.err) {
      const msg = String((this.state.err && this.state.err.message) || this.state.err);
      return html`
        <article class="nb-msg nb-msg--system" style="color:#c66;font-size:.78rem;font-family:var(--nb-font-mono);padding:.4rem .8rem;border-left:2px solid #c66">
          [bubble render error] ${msg.slice(0, 200)}
        </article>
      `;
    }
    return this.props.children;
  }
}

const SCROLL_PIN_THRESHOLD = 32; // px from bottom

function ensureScrollState(node, lastLen) {
  if (!node) return;
  const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
  if (distanceFromBottom > SCROLL_PIN_THRESHOLD) {
    node.dataset.xmcPinned = "1";
  } else {
    delete node.dataset.xmcPinned;
  }
  if (node.dataset.xmcPinned !== "1") {
    requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
  }
  node.dataset.xmcLen = String(lastLen);
}

function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(typeof ts === "number" && ts < 1e12 ? ts * 1000 : ts);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

// 小巧思：跨天日期分隔。
function _toDate(ts) {
  if (!ts) return null;
  return new Date(typeof ts === "number" && ts < 1e12 ? ts * 1000 : ts);
}
function _dayKey(d) {
  return d ? `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}` : null;
}
function shouldShowDateDivider(prev, cur) {
  const cd = _toDate(cur && (cur.ts || cur.created_at));
  if (!cd) return false;
  if (!prev) return true; // 第一条 → 顶部日期，兼作「开场」标记
  const pd = _toDate(prev.ts || prev.created_at);
  if (!pd) return false;
  return _dayKey(pd) !== _dayKey(cd);
}
function friendlyDay(ts) {
  const d = _toDate(ts);
  if (!d) return "";
  const now = new Date();
  const today = _dayKey(now);
  const y = new Date(now.getTime() - 86400000);
  if (_dayKey(d) === today) return "今天";
  if (_dayKey(d) === _dayKey(y)) return "昨天";
  return d.toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric" });
}

// Phase M3：记忆写入卡（assistant 消息下）。当前 nebula 渲染器是
// MessageList，原 MemoryMemo 在没人 import 的 MessageBubble.js 里=死代码，
// 所以这里重新接上。样式 .nb-memory-memo 在 instrument.css。
const _MEMO_META = {
  long_term:  { label: "长期记忆", icon: "🧠" },
  short_term: { label: "短期记忆", icon: "⏳" },
  working:    { label: "工作记忆", icon: "✦" },
  procedural: { label: "程序记忆", icon: "⚙" },
};
function MemoryMemo({ memo }) {
  const m = _MEMO_META[memo.layer] || { label: memo.layer || "记忆", icon: "📝" };
  return html`
    <div class="nb-memory-memo" data-layer=${memo.layer || "other"} role="note"
         title=${memo.reason ? `为什么记: ${memo.reason}` : "已写入记忆 — 下次还记得"}>
      <span class="nb-memory-memo__spark" aria-hidden="true">${m.icon}</span>
      <span class="nb-memory-memo__layer">${m.label}</span>
      <span class="nb-memory-memo__text">${memo.text}</span>
      <span class="nb-memory-memo__tick" aria-hidden="true">✓ 已记住</span>
    </div>
  `;
}

// Phase M3：召回卡（user 消息下）— 写/读对称。「💭 想起 N 条」可展开看命中。
function RecallMemo({ hits, query }) {
  if (!Array.isArray(hits) || hits.length === 0) return null;
  return html`
    <details class="nb-recall-memo">
      <summary class="nb-recall-memo__head">
        <span class="nb-recall-memo__spark" aria-hidden="true">💭</span>
        <span class="nb-recall-memo__title">想起 ${hits.length} 条相关记忆</span>
        ${query ? html`<span class="nb-recall-memo__q">「${query}」</span>` : null}
        <span class="nb-recall-memo__chev" aria-hidden="true">▸</span>
      </summary>
      <div class="nb-recall-memo__body">
        ${hits.map((h, i) => html`
          <div class="nb-recall-memo__item" key=${h.id || i}>
            <span class="nb-recall-memo__k">${h.kind || "fact"}</span>
            <span class="nb-recall-memo__t">${h.text}</span>
            ${h.distance != null ? html`<span class="nb-recall-memo__d">d=${Number(h.distance).toFixed(2)}</span>` : null}
          </div>
        `)}
      </div>
    </details>
  `;
}

// 小巧思：复制回执 — 点一下按钮，原地变「✓ 已复制」一秒再恢复。
function CopyButton({ text, onCopy }) {
  const [done, setDone] = useState(false);
  return html`
    <button
      class=${"nb-msg-action" + (done ? " is-done" : "")}
      title=${done ? "已复制" : "复制"}
      onClick=${async () => { try { await onCopy(text); } catch (_) {} setDone(true); setTimeout(() => setDone(false), 1100); }}
    >${done ? "✓" : "📋"}</button>
  `;
}

function getTextContent(message) {
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content.map((b) => b?.text || "").join(" ");
  }
  return "";
}

// 助手「空鬼泡」判定：内容/事件/工具/思考/媒体全空。thinking 占位消息在真正
// 内容落到另一条消息后会变成这种空壳，旧逻辑仍渲染出一个空灰泡 → 这里判空后
// 不渲染（仅当它不处于工作/错误态时）。
function assistantIsEmpty(m) {
  if (getTextContent(m)) return false;
  if (Array.isArray(m.events) && m.events.length) return false;
  if (Array.isArray(m.toolCalls) && m.toolCalls.length) return false;
  if (Array.isArray(m.thinking) ? m.thinking.length : m.thinking) return false;
  if ((Array.isArray(m.images) && m.images.length)
    || (Array.isArray(m.videos) && m.videos.length)
    || (Array.isArray(m.audios) && m.audios.length)) return false;
  // Phase 9 M1: 只带 canvas artifact、无文字的助手消息不是空鬼泡。
  if (Array.isArray(m.canvasArtifacts) && m.canvasArtifacts.length) return false;
  return true;
}

const MSG_TRUNCATE_CHARS = 3000;

function truncateAtBoundary(text, maxChars) {
  if (!text || text.length <= maxChars) return { truncated: text, wasCut: false };
  let cut = text.lastIndexOf("\n\n", maxChars);
  if (cut < maxChars * 0.5) {
    cut = text.lastIndexOf("\n", maxChars);
  }
  if (cut < 0 || cut < maxChars * 0.3) {
    cut = maxChars;
  }
  return { truncated: text.slice(0, cut), wasCut: true };
}

export function MessageList({ messages, onAnswerQuestion, onCanvasAction, pendingAssistantId }) {
  const empty = messages.length === 0;
  const [expandedMsgs, setExpandedMsgs] = useState(new Set());
  const [hiddenMsgs, setHiddenMsgs] = useState(new Set());
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState("");

  const toggleExpand = (id) => {
    setExpandedMsgs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const onCopy = async (text) => {
    try { await navigator.clipboard.writeText(text); } catch (_) {}
  };

  // 双气泡修复（2026-06-05）：独立的「正在输入」指示器只在「已有 pending
  // 回复、但其助手气泡尚未出现在列表里」的短暂空档显示。一旦助手占位消息
  // 已渲染（它内部 PhaseCard 显示「正在思考/正在回复」），就不再额外渲染这个
  // "..." 气泡，否则同一时刻出现两个助手气泡。
  const pendingRendered = !!pendingAssistantId && messages.some((m) => m.id === pendingAssistantId);
  const showTyping = !!pendingAssistantId && !pendingRendered;

  return html`
    <div
      class="nb-stream"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      ref=${(node) => ensureScrollState(node, messages.length)}
    >
      ${empty
        ? html`
            <div class="nb-empty">
              <div class="nb-empty__icon">✦</div>
              <h3>开始对话</h3>
              <p>在下方输入消息后回车发送。</p>
              <p style="font-size:12px;color:var(--nb-fg-tertiary);max-width:400px;margin:0 auto 24px;">
                Plan 模式下助手会先列出步骤，再等待你确认。Ultrathink 触发更深的推理（消耗更多 token）。
              </p>
            </div>
          `
        : messages.map((m, i) => {
            if (hiddenMsgs.has(m.id)) return null;
            // 小巧思：跨天插入日期分隔（今天/昨天/日期 + 微光）。
            const prev = messages[i - 1];
            const divider = shouldShowDateDivider(prev, m)
              ? html`<div class="nb-date-divider" key=${"dd-" + m.id}><span>${friendlyDay(m.ts || m.created_at)}</span></div>`
              : null;
            return html`
              ${divider}
              <${BubbleBoundary} key=${m.id}>
                <${MessageRow}
                  message=${m}
                  onAnswerQuestion=${onAnswerQuestion}
                  onCanvasAction=${onCanvasAction}
                  isExpanded=${expandedMsgs.has(m.id)}
                  onToggleExpand=${() => toggleExpand(m.id)}
                  onCopy=${onCopy}
                  isEditing=${editingId === m.id}
                  onStartEdit=${() => { setEditingId(m.id); setEditDraft(getTextContent(m)); }}
                  onCancelEdit=${() => setEditingId(null)}
                  editDraft=${editDraft}
                  onChangeEditDraft=${setEditDraft}
                  onHide=${() => setHiddenMsgs((prev) => new Set(prev).add(m.id))}
                />
              </${BubbleBoundary}>
            `;
          })}
      ${showTyping ? html`
        <div class="nb-msg nb-msg--assistant" key="typing">
          <div class="nb-msg__avatar" style="animation:pulse-dot 1.2s infinite;">✦</div>
          <div class="nb-msg__body">
            <div class="nb-typing-indicator"><span></span><span></span><span></span></div>
          </div>
        </div>
      ` : null}
    </div>
  `;
}

function MessageRow({
  message,
  onAnswerQuestion,
  onCanvasAction,
  isExpanded,
  onToggleExpand,
  onCopy,
  isEditing,
  onStartEdit,
  onCancelEdit,
  editDraft,
  onChangeEditDraft,
  onHide,
}) {
  const role = message.role || "system";
  const isUser = role === "user";
  const isSystem = role === "system";
  const isAssistant = role === "assistant";
  const streaming = message.status === "streaming";
  const thinking = message.status === "thinking";
  const errored = message.status === "error";
  const cancelled = message.status === "cancelled";
  const warning = message.status === "warning";

  const contentText = getTextContent(message);
  const isLong = isAssistant && contentText.length > MSG_TRUNCATE_CHARS && !streaming && !isExpanded;

  let msgClass = "nb-msg";
  if (isUser) msgClass += " nb-msg--user";
  else if (isSystem) msgClass += " nb-msg--system";
  else msgClass += " nb-msg--assistant";
  if (errored || cancelled) msgClass += " nb-msg--error";
  if (warning) msgClass += " nb-msg--warning";
  if (isLong) msgClass += " is-collapsed";

  const avatar = isUser
    ? (message.userName ? message.userName.charAt(0).toUpperCase() : "U")
    : isSystem
    ? null
    : errored || cancelled
    ? "!"
    : warning
    ? "⚠"
    : "✦";

  const name = isUser
    ? null
    : isSystem
    ? null
    : errored || cancelled
    ? "XMCLAW · 错误"
    : warning
    ? "XMCLAW · 警告"
    : thinking
    ? "XMCLAW · thinking..."
    : "XMCLAW";

  const timeStr = formatTime(message.ts || message.created_at);

  // ── Special message kinds ──
  if (message.kind === "question") {
    return html`
      <article class=${msgClass} data-msg-id=${message.id}>
        ${avatar ? html`<div class="nb-msg__avatar">${avatar}</div>` : null}
        <div class="nb-msg__body">
          ${name ? html`<div class="nb-msg__name">${name}</div>` : null}
          <div class="nb-msg__bubble">
            <${QuestionCard} message=${message} onAnswerQuestion=${onAnswerQuestion} />
          </div>
          ${timeStr ? html`<div class="nb-msg__time">${timeStr}</div>` : null}
        </div>
      </article>
    `;
  }

  if (message.kind === "tool_use") {
    return html`
      <article class=${msgClass} data-msg-id=${message.id}>
        ${avatar ? html`<div class="nb-msg__avatar">${avatar}</div>` : null}
        <div class="nb-msg__body">
          ${name ? html`<div class="nb-msg__name">${name}</div>` : null}
          <div class="nb-msg__bubble">
            <${ToolCard} call=${message} />
          </div>
          ${timeStr ? html`<div class="nb-msg__time">${timeStr}</div>` : null}
        </div>
      </article>
    `;
  }

  if (message.kind === "worker") {
    return html`
      <article class=${msgClass} data-msg-id=${message.id}>
        ${avatar ? html`<div class="nb-msg__avatar">${avatar}</div>` : null}
        <div class="nb-msg__body">
          ${name ? html`<div class="nb-msg__name">${name}</div>` : null}
          <div class="nb-msg__bubble">
            <${WorkerCard} call=${message} />
          </div>
          ${timeStr ? html`<div class="nb-msg__time">${timeStr}</div>` : null}
        </div>
      </article>
    `;
  }

  if (message.kind === "subagent") {
    return html`
      <article class=${msgClass} data-msg-id=${message.id}>
        ${avatar ? html`<div class="nb-msg__avatar">${avatar}</div>` : null}
        <div class="nb-msg__body">
          ${name ? html`<div class="nb-msg__name">${name}</div>` : null}
          <div class="nb-msg__bubble">
            <${SubagentCard} call=${message} />
          </div>
          ${timeStr ? html`<div class="nb-msg__time">${timeStr}</div>` : null}
        </div>
      </article>
    `;
  }

  // 空鬼泡守卫：助手消息既不工作也不报错，且内容全空 → 不渲染（消除回复后
  // 残留的空灰泡 / thinking 占位空壳）。特殊 kind（question/tool_use/worker/
  // subagent）已在上面提前 return，不会走到这里。
  if (isAssistant && !streaming && !thinking && !errored && !cancelled && !warning && assistantIsEmpty(message)) {
    return null;
  }

  // ── Quote / Reply reference ──
  const quoteRef = message.replyTo || message.quote
    ? html`
        <div class="nb-quote-ref" onClick=${() => {
          const el = document.querySelector(`[data-msg-id="${message.replyTo?.id || message.replyTo}"]`);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        }}>
          <div class="nb-quote-ref__author">${message.replyToName || message.replyTo?.name || "XMCLAW"}</div>
          ${message.replyToText || message.replyTo?.text || message.quote || ""}
        </div>
      `
    : null;

  // ── Bubble content ──
  let bubbleContent = null;

  if (isEditing && isUser) {
    bubbleContent = html`
      <div>
        <textarea
          style="width:100%;min-height:60px;background:transparent;color:var(--nb-fg-primary);border:1px solid var(--nb-border-accent);border-radius:var(--nb-radius-sm);padding:8px;font-family:inherit;font-size:14px;line-height:1.6;resize:vertical;outline:none;"
          value=${editDraft}
          onInput=${(e) => onChangeEditDraft(e.target.value)}
        />
        <div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end;">
          <button class="nb-msg-action" style="width:auto;padding:0 10px;" onClick=${onCancelEdit}>取消</button>
          <button class="nb-msg-action" style="width:auto;padding:0 10px;" onClick=${() => { onCancelEdit(); }}>保存</button>
        </div>
      </div>
    `;
  } else if (isSystem) {
    bubbleContent = html`<div class="nb-md">${contentText}</div>`;
  } else if (isUser) {
    bubbleContent = html`
      <div class="nb-md">${contentText}</div>
      ${Array.isArray(message.images) && message.images.length > 0
        ? html`
            <div class="nb-attachment-grid" style="margin-top:8px;">
              ${message.images.map((src, i) => html`
                <div key=${i} class="nb-attachment-item" onClick=${() => openLightbox(src, { alt: `attachment ${i + 1}`, items: message.images, index: i })}>
                  <img src=${src} alt=${"attachment " + (i + 1)} loading="lazy" />
                  <div class="nb-attachment-item__name">附件 ${i + 1}</div>
                </div>
              `)}
            </div>
          `
        : null}
    `;
  } else if (isAssistant) {
    const parts = [];

    // Phase / thinking status card
    if (thinking || streaming || (message.thinking && message.thinking.length > 0)) {
      const baseLabel = thinking
        ? "正在思考"
        : streaming
        ? "正在回复"
        : "思考过程";
      const isWorking = thinking || streaming;
      const elapsedS = isWorking && message.ts
        ? Math.max(0, Math.floor(Date.now() / 1000 - message.ts))
        : null;
      parts.push(html`
        <${PhaseCard}
          key="phase"
          message=${message}
          baseLabel=${baseLabel}
          elapsedS=${elapsedS}
          stalled=${elapsedS != null && elapsedS > 90}
          isWorking=${isWorking}
          currentHop=${message.phaseMeta?.hop ?? null}
        />
      `);
    }

    // Events stream (B-218 chronological)
    if (message.events && message.events.length > 0) {
      message.events.forEach((ev) => {
        if (ev.type === "thinking") {
          parts.push(html`
            <details key=${ev.id} class="xmc-toolcard xmc-toolcard--ok xmc-toolcard--thinking" style="margin:8px 0;">
              <summary style="cursor:pointer;padding:6px 10px;font-size:12px;color:var(--nb-fg-secondary);font-family:var(--nb-font-mono);">
                💡 Thinking ${ev.content ? html`<small style="color:var(--nb-fg-muted);margin-left:.3em">${ev.content.length} chars</small>` : null}
              </summary>
              ${ev.content ? html`<pre style="white-space:pre-wrap;font-size:.85em;line-height:1.5;padding:8px 10px;color:var(--nb-fg-secondary);">${ev.content}</pre>` : null}
            </details>
          `);
        } else if (ev.type === "tool") {
          parts.push(html`<${ToolCard} key=${ev.id} call=${ev} />`);
        } else {
          parts.push(html`<${MarkdownBody} key=${ev.id} content=${ev.content || ""} />`);
        }
      });
    } else if (contentText) {
      const { truncated, wasCut } = isLong
        ? truncateAtBoundary(contentText, MSG_TRUNCATE_CHARS)
        : { truncated: contentText, wasCut: false };
      parts.push(html`<${MarkdownBody} key="md" content=${truncated} />`);
      if (wasCut) {
        parts.push(html`
          <button key="expand" class="nb-msg-collapse-btn" onClick=${onToggleExpand}>
            … 展开剩余内容 (${contentText.length - truncated.length} 字符)
          </button>
        `);
      }
    }

    // Legacy toolCalls
    if (message.toolCalls && message.toolCalls.length > 0) {
      message.toolCalls.forEach((call) => {
        parts.push(html`<${ToolCard} key=${call.id} call=${call} />`);
      });
    }

    // Media attachments
    if (Array.isArray(message.images) && message.images.length > 0) {
      parts.push(html`
        <div class="nb-attachment-grid" key="images" style="margin-top:8px;">
          ${message.images.map((src, i) => html`
            <div key=${i} class="nb-attachment-item" onClick=${() => openLightbox(src, { alt: `attachment ${i + 1}`, items: message.images, index: i })}>
              <img src=${src} alt=${"attachment " + (i + 1)} loading="lazy" />
              <div class="nb-attachment-item__name">附件 ${i + 1}</div>
            </div>
          `)}
        </div>
      `);
    }
    if (Array.isArray(message.videos) && message.videos.length > 0) {
      parts.push(html`
        <div key="videos" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">
          ${message.videos.map((src, i) => html`
            <video key=${"v" + i} src=${src} controls preload="metadata" style="max-width:280px;border-radius:var(--nb-radius-md);border:1px solid var(--nb-border);" />
          `)}
        </div>
      `);
    }
    if (Array.isArray(message.audios) && message.audios.length > 0) {
      parts.push(html`
        <div key="audios" style="display:flex;flex-direction:column;gap:8px;margin-top:8px;">
          ${message.audios.map((src, i) => html`
            <audio key=${"a" + i} src=${src} controls preload="metadata" style="width:100%;" />
          `)}
        </div>
      `);
    }

    // Phase 9 M1: agent 创建的 canvas artifacts(canvas_create/update)。
    if (Array.isArray(message.canvasArtifacts) && message.canvasArtifacts.length > 0) {
      message.canvasArtifacts.forEach((art) => {
        parts.push(html`
          <${CanvasArtifact}
            key=${art.artifact_id}
            artifact=${art}
            onCanvasAction=${onCanvasAction}
          />
        `);
      });
    }

    // Streaming cursor
    if (streaming) {
      parts.push(html`<span key="cursor" class="nb-streaming-cursor"></span>`);
    }

    bubbleContent = parts;
  }

  // ── Actions ──
  const userActions = html`
    <button class="nb-msg-action" title="编辑" onClick=${onStartEdit}>✎</button>
    <button class="nb-msg-action" title="删除" onClick=${onHide}>🗑</button>
  `;
  const assistantActions = html`
    <${CopyButton} text=${contentText} onCopy=${onCopy} />
    <button class="nb-msg-action" title="重新生成" onClick=${() => {}}>↻</button>
    <button class="nb-msg-action" title="点赞" onClick=${() => {}}>👍</button>
    <button class="nb-msg-action" title="点踩" onClick=${() => {}}>👎</button>
  `;
  const errorActions = html`
    <button class="nb-msg-action" title="重试" onClick=${() => {}}>↻</button>
    <${CopyButton} text=${contentText} onCopy=${onCopy} />
  `;

  return html`
    <article class=${msgClass} data-msg-id=${message.id} data-role=${role}>
      ${avatar ? html`<div class=${"nb-msg__avatar" + ((streaming || thinking) ? " nb-msg__avatar--live" : "")}>${avatar}</div>` : null}
      <div class="nb-msg__body">
        ${name ? html`<div class="nb-msg__name">${name}</div>` : null}
        ${quoteRef}
        <div class="nb-msg__bubble">
          ${bubbleContent}
        </div>
        ${isUser && Array.isArray(message.memoryRecalls) && message.memoryRecalls.length
          ? html`<${RecallMemo} hits=${message.memoryRecalls} query=${message.memoryRecallQuery} />`
          : null}
        ${isAssistant && Array.isArray(message.memoryMemos) && message.memoryMemos.length
          ? message.memoryMemos.map((memo) => html`<${MemoryMemo} key=${memo.id} memo=${memo} />`)
          : null}
        ${timeStr ? html`<div class="nb-msg__time">${timeStr}</div>` : null}
        <div class="nb-msg-actions">
          ${isUser ? userActions : isAssistant && (errored || cancelled) ? errorActions : isAssistant ? assistantActions : null}
        </div>
      </div>
    </article>
  `;
}
