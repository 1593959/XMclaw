// XMclaw — SessionsPage sub-components (Nebula redesign v2).
//
// Rewritten to match Nebula prototype design:
//   - SessionRow uses nb-session-item / nb-session-dot / nb-session-tag /
//     nb-session-actions classes
//   - Tags are inferred from preview text + sid keywords
//   - Hover actions: archive / export / delete
//   - Expanded message list kept intact (xmc-h-msgbubble etc.)
//
// Kept existing exports for backward compatibility.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { lex, renderTokenHtml } from "../../lib/markdown.js";


// ── Inline icon SVGs (lucide-react equivalents used by the reference) ──

export function Icon({ d, className }) {
  return html`
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
      stroke-linecap="round"
      stroke-linejoin="round"
      class=${"xmc-icon " + (className || "")}
      aria-hidden="true"
    >
      <path d=${d} />
    </svg>
  `;
}

export const I_SEARCH = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
export const I_CHEVRON_DOWN = "m6 9 6 6 6-6";
export const I_CHEVRON_RIGHT = "m9 18 6-6-6-6";
export const I_TRASH = "M3 6h18 M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2";
export const I_X = "M18 6 6 18 M6 6l12 12";
export const I_PLAY = "M5 4 19 12 5 20Z";
export const I_LOADER = "M21 12a9 9 0 1 1-6.219-8.56";


// ── ToolCallBlock — collapsible tool-use record inside a message ──

function ToolCallBlock({ tc }) {
  const [open, setOpen] = useState(false);
  let argsStr;
  try {
    argsStr = JSON.stringify(tc.args, null, 2);
  } catch (_) {
    argsStr = String(tc.args);
  }
  return html`
    <div class="xmc-h-tcblock">
      <button
        type="button"
        class="xmc-h-tcblock__head"
        onClick=${() => setOpen((v) => !v)}
        aria-label=${(open ? "collapse " : "expand ") + tc.name}
      >
        <${Icon} d=${open ? I_CHEVRON_DOWN : I_CHEVRON_RIGHT} className="xmc-h-tcblock__chev" />
        <span class="xmc-h-tcblock__name">${tc.name}</span>
        <span class="xmc-h-tcblock__id">${(tc.id || "").slice(0, 12)}</span>
      </button>
      ${open ? html`<pre class="xmc-h-tcblock__args">${argsStr}</pre>` : null}
    </div>
  `;
}


// ── MessageBubble (port of SessionsPage MessageBubble) ──

function MessageBubble({ msg, highlight }) {
  const role = msg.role || "system";
  const isHit = (() => {
    if (!highlight || !msg.content) return false;
    const c = msg.content.toLowerCase();
    return highlight.toLowerCase().split(/\s+/).filter(Boolean)
      .some((t) => c.includes(t));
  })();
  const tokens = role === "system" ? null : lex(msg.content || "");

  return html`
    <div
      class=${"xmc-h-msgbubble xmc-h-msgbubble--" + role + (isHit ? " is-hit" : "")}
      data-search-hit=${isHit ? "" : null}
    >
      <div class="xmc-h-msgbubble__head">
        <span class="xmc-h-msgbubble__role">
          ${msg.tool_call_id ? `tool: ${msg.tool_call_id.slice(0, 8)}` : role}
        </span>
        ${isHit
          ? html`<span class="xmc-h-badge xmc-h-badge--warning">match</span>`
          : null}
      </div>
      ${msg.content
        ? (role === "system"
          ? html`<div class="xmc-h-msgbubble__body xmc-h-msgbubble__body--plain">${msg.content}</div>`
          : html`
            <div class="xmc-h-msgbubble__body">
              ${tokens.map((t) => html`
                <div
                  key=${t.idx}
                  data-tok-type=${t.type || "text"}
                  dangerouslySetInnerHTML=${{ __html: renderTokenHtml(t) }}
                ></div>
              `)}
            </div>
          `)
        : null}
      ${(msg.tool_calls || []).length > 0
        ? html`
          <div class="xmc-h-msgbubble__tcs">
            ${msg.tool_calls.map((tc) => html`<${ToolCallBlock} key=${tc.id} tc=${tc} />`)}
          </div>
        `
        : null}
    </div>
  `;
}


// ── Expanded message list ──

export function MessageList({ messages, highlight }) {
  return html`
    <div class="xmc-h-msglist">
      ${messages.map((m, i) => html`
        <${MessageBubble} key=${i} msg=${m} highlight=${highlight} />
      `)}
    </div>
  `;
}


// ── Source-prefix mapping + relative-time helper ──

export const SOURCE_CONFIG = {
  cli:      { glyph: "▮", label: "CLI" },
  telegram: { glyph: "✈", label: "Telegram" },
  discord:  { glyph: "#", label: "Discord" },
  slack:    { glyph: "≡", label: "Slack" },
  feishu:   { glyph: "✦", label: "Feishu" },
  wecom:    { glyph: "❖", label: "WeCom" },
  cron:     { glyph: "⏱", label: "Cron" },
  unknown:  { glyph: "○", label: "Unknown" },
};

export function inferSource(sid) {
  if (!sid) return "unknown";
  if (sid.startsWith("tg-") || sid.startsWith("telegram-")) return "telegram";
  if (sid.startsWith("discord-")) return "discord";
  if (sid.startsWith("slack-")) return "slack";
  if (sid.startsWith("feishu-")) return "feishu";
  if (sid.startsWith("wecom-")) return "wecom";
  if (sid.startsWith("cron-")) return "cron";
  if (sid.startsWith("chat-") || sid.startsWith("live-")) return "cli";
  return "unknown";
}

export function timeAgo(epoch) {
  if (!epoch) return "—";
  const ms = Math.max(0, Date.now() - epoch * 1000);
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 48) return h + "h ago";
  const d = Math.floor(h / 24);
  if (d < 30) return d + "d ago";
  const mo = Math.floor(d / 30);
  return mo + "mo ago";
}


// ── Tag inference ──

export function inferTags(preview = "", sid = "") {
  const tags = [];
  const p = (preview || "").toLowerCase();
  if (p.includes("图片") || p.includes("png") || p.includes("jpg") || p.includes("jpeg") || p.includes("压缩") || p.includes("裁剪") || p.includes("缩放")) {
    tags.push("图片处理");
  }
  if (p.includes("docker") || p.includes("部署") || p.includes("build") || p.includes("镜像") || p.includes("ci") || p.includes("cd")) {
    tags.push("部署");
  }
  if (p.includes("ui") || p.includes("设计") || p.includes("前端") || p.includes("web") || p.includes("css") || p.includes("html")) {
    tags.push("前端");
  }
  if (p.includes("调研") || p.includes("对比") || p.includes("选型") || p.includes("分析") || p.includes("研究")) {
    tags.push("调研");
  }
  if (p.includes("技能") || p.includes("自动") || p.includes("触发") || p.includes("定时") || p.includes("cron")) {
    tags.push("自动化");
  }
  if (p.includes("测试") || p.includes("验证") || p.includes("debug") || p.includes("排查")) {
    tags.push("测试");
  }
  if (p.includes("代码") || p.includes("函数") || p.includes("重构") || p.includes("优化") || p.includes("fix")) {
    tags.push("开发");
  }
  if (sid.startsWith("reflect:") || sid.startsWith("dream:") || sid.startsWith("_system")) {
    tags.push("内部");
  }
  if (tags.length === 0) {
    tags.push("对话");
  }
  return tags.slice(0, 3);
}

function tagStyle(tag) {
  if (tag === "归档") return "background:rgba(100,116,139,0.15);color:var(--nb-fg-tertiary);border-color:var(--nb-border)";
  if (tag === "图片处理") return "background:rgba(139,92,246,0.1);color:var(--nb-accent-light);border-color:var(--nb-border-accent)";
  if (tag === "自动化") return "background:rgba(6,182,212,0.1);color:var(--nb-cyan-light);border-color:rgba(6,182,212,0.2)";
  if (tag === "调研") return "background:rgba(16,185,129,0.1);color:var(--nb-success);border-color:rgba(16,185,129,0.2)";
  if (tag === "测试") return "background:rgba(245,158,11,0.1);color:var(--nb-amber-light);border-color:rgba(245,158,11,0.2)";
  if (tag === "部署") return "background:rgba(59,130,246,0.1);color:var(--nb-info);border-color:rgba(59,130,246,0.2)";
  if (tag === "前端") return "background:rgba(236,72,153,0.1);color:#ec4899;border-color:rgba(236,72,153,0.2)";
  if (tag === "开发") return "background:rgba(139,92,246,0.1);color:var(--nb-accent-light);border-color:var(--nb-border-accent)";
  if (tag === "内部") return "background:rgba(100,116,139,0.15);color:var(--nb-fg-tertiary);border-color:var(--nb-border)";
  return "background:rgba(139,92,246,0.1);color:var(--nb-accent-light);border-color:var(--nb-border-accent)";
}


// ── SessionRow — one enhanced row per session (Nebula design) ──

export function SessionRow({
  session, query, expanded, onToggle, onDelete, onResume,
  token, isArchived, onArchive, onExport, deletingId, matchSnippet,
}) {
  const [messages, setMessages] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const sid = session.session_id;
  const tags = inferTags(session.preview, sid);

  useEffect(() => {
    if (!expanded || messages !== null || loading) return;
    setLoading(true);
    apiGet(`/api/v2/sessions/${encodeURIComponent(sid)}`, token)
      .then((d) => setMessages(d.messages || []))
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [expanded, sid, token, messages, loading]);

  return html`
    <div>
      <div
        class=${"nb-session-item" + (expanded ? " active" : "")}
        onClick=${onToggle}
        style=${isArchived ? "opacity:0.85;" : ""}
      >
        <div
          class="nb-session-dot"
          style=${isArchived
            ? "background:var(--nb-fg-muted)"
            : "background:var(--nb-accent);box-shadow:0 0 8px var(--nb-accent-glow)"}
        ></div>
        <div class="nb-session-info">
          <div class="nb-session-title">${session.preview ? session.preview.split("\n")[0] : sid.slice(0, 16)}</div>
          <div class="nb-session-preview">${session.preview || "无预览"}</div>
          <div class="nb-session-meta">
            ${isArchived
              ? html`<span class="nb-session-tag" style="background:rgba(100,116,139,0.15);color:var(--nb-fg-tertiary);border-color:var(--nb-border)">归档</span>`
              : null}
            ${tags.map((tag) => html`
              <span class="nb-session-tag" style=${tagStyle(tag)}>${tag}</span>
            `)}
            <span style="font-size:11px;color:var(--nb-fg-muted)">${session.message_count || 0} 消息 · ${timeAgo(session.updated_at)}</span>
          </div>
        </div>
        <div class="nb-session-actions" onClick=${(e) => e.stopPropagation()}>
          ${onResume
            ? html`
              <button
                class="nb-session-action"
                title="在 Chat 中恢复"
                onClick=${(e) => { e.stopPropagation(); onResume(sid); }}
              >▶</button>
            `
            : null}
          <button
            class="nb-session-action"
            title=${isArchived ? "取消归档" : "归档"}
            onClick=${(e) => { e.stopPropagation(); onArchive(sid); }}
          >📦</button>
          <button
            class="nb-session-action"
            title="导出"
            onClick=${(e) => { e.stopPropagation(); onExport(sid); }}
          >⬇</button>
          <button
            class="nb-session-action"
            title="删除"
            onClick=${(e) => { e.stopPropagation(); onDelete(sid); }}
            disabled=${deletingId === sid}
          >${deletingId === sid ? "⏳" : "🗑"}</button>
        </div>
      </div>
      ${expanded
        ? html`
          <div
            class="xmc-h-srow__body"
            style="margin-top:8px;border-radius:var(--nb-radius-md);border:1px solid var(--nb-border);background:var(--nb-bg-glass);padding:12px 16px;"
          >
            ${error
              ? html`<div class="xmc-h-error">${error}</div>`
              : loading
                ? html`<div class="xmc-h-loading">载入中…</div>`
                : messages && messages.length === 0
                  ? html`<div class="xmc-h-empty">这个会话还没消息。</div>`
                  : messages
                    ? html`<${MessageList} messages=${messages} highlight=${query} />`
                    : null}
          </div>
        `
        : null}
      ${!expanded && matchSnippet
        ? html`
          <div
            class="xmc-h-srow__snippet"
            title="服务端搜索命中片段"
            style="padding:4px 16px 8px 16px;color:var(--nb-fg-muted);font-family:var(--nb-font-mono,monospace);font-size:0.78em;line-height:1.45;white-space:pre-wrap;word-break:break-word;"
          >…${matchSnippet}…</div>
        `
        : null}
    </div>
  `;
}


// ── DeleteConfirmDialog (kept for backward compatibility) ──

export function DeleteConfirmDialog({ sid, onCancel, onConfirm, busy }) {
  if (!sid) return null;
  return html`
    <div class="xmc-h-dialog__backdrop" onClick=${onCancel}>
      <div
        class="xmc-h-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="del-title"
        onClick=${(e) => e.stopPropagation()}
      >
        <header class="xmc-h-dialog__head">
          <h3 id="del-title" class="xmc-h-dialog__title">确认删除会话</h3>
          <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onCancel} aria-label="close">
            <${Icon} d=${I_X} />
          </button>
        </header>
        <div class="xmc-h-dialog__body">
          这将永久删除会话历史 <code>${sid}</code> 及其所有消息。此操作不可撤销。
        </div>
        <div class="xmc-h-dialog__foot">
          <button type="button" class="xmc-h-btn" onClick=${onCancel} disabled=${busy}>取消</button>
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--danger"
            onClick=${onConfirm}
            disabled=${busy}
          >${busy ? "删除中…" : "确认删除"}</button>
        </div>
      </div>
    </div>
  `;
}
