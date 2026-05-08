// XMclaw — SessionsPage sub-components (B-323 split).
//
// Lifted out of pages/Sessions.js to keep that page under the 500-line
// UI budget (FRONTEND_DESIGN.md §1.4). Pure presentation pieces —
// the parent wires the dataflow (search, expand, delete).

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { lex, renderTokenHtml } from "../../lib/markdown.js";


// ── Inline icon SVGs (lucide-react equivalents used by Hermes) ──

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
//
// B-341 (audit pass-2 #7): moved out of Sessions.js along with
// SessionRow so the page stays under the 500-line UI budget after
// the search-endpoint wiring landed. Pure helpers, no imports.

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


// ── SessionRow — one collapsible card per session ──
//
// B-341 (audit pass-2 #7): extracted from Sessions.js. Adds
// ``matchSnippet`` prop — when the parent's server-side search
// returned this session, the snippet is rendered as a muted
// monospace preview under the row so the user sees WHERE the hit
// landed without expanding the row first. Pre-B-341 the row
// rendered no snippet; the search box only filtered already-loaded
// previews and the B-339 endpoint had no caller.

export function SessionRow({
  session, query, expanded, onToggle, onDelete, onResume,
  token, isSelected, onToggleSelect, matchSnippet,
}) {
  const [messages, setMessages] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const sid = session.session_id;
  const source = inferSource(sid);
  const sCfg = SOURCE_CONFIG[source] || SOURCE_CONFIG.unknown;

  useEffect(() => {
    if (!expanded || messages !== null || loading) return;
    setLoading(true);
    apiGet(`/api/v2/sessions/${encodeURIComponent(sid)}`, token)
      .then((d) => setMessages(d.messages || []))
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [expanded, sid, token, messages, loading]);

  return html`
    <div class=${"xmc-h-srow" + (isSelected ? " is-selected" : "")} key=${sid}
         style=${"display:flex;align-items:stretch;flex-wrap:wrap;" + (isSelected ? "background:color-mix(in srgb,var(--color-primary,#6aa3f0) 8%,transparent);border-color:color-mix(in srgb,var(--color-primary,#6aa3f0) 50%,transparent);" : "")}>
      <!-- B-156: 行首 checkbox 触发批量选择，stopPropagation 防止误展开 -->
      ${onToggleSelect
        ? html`<label
            style="display:flex;align-items:center;padding:0 .4rem 0 .6rem;cursor:pointer"
            onClick=${(e) => e.stopPropagation()}
            title="勾选用于批量删除"
          >
            <input
              type="checkbox"
              checked=${!!isSelected}
              onChange=${onToggleSelect}
            />
          </label>`
        : null}
      <button
        type="button"
        class="xmc-h-srow__head"
        onClick=${onToggle}
        aria-expanded=${expanded ? "true" : "false"}
        style="flex:1 1 auto;min-width:0;width:auto"
      >
        <${Icon} d=${expanded ? I_CHEVRON_DOWN : I_CHEVRON_RIGHT} className="xmc-h-srow__chev" />
        <span class="xmc-h-srow__source" title=${sCfg.label}>${sCfg.glyph}</span>
        ${session.preview
          ? html`
              <span class="xmc-h-srow__preview" title=${sid} style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500;color:var(--color-fg)">${session.preview}</span>
              <code class="xmc-h-srow__sid" style="opacity:.5;font-size:.75em">${sid.slice(0, 12)}</code>
            `
          : html`<code class="xmc-h-srow__sid">${sid}</code>`}
        <span class="xmc-h-srow__count">${session.message_count || 0} 轮</span>
        <span class="xmc-h-srow__time">${timeAgo(session.updated_at)}</span>
        <span class="xmc-h-srow__actions">
          ${onResume
            ? html`
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--ghost"
                onClick=${(e) => { e.stopPropagation(); onResume(sid); }}
                title="在 Chat 中恢复"
              >
                <${Icon} d=${I_PLAY} />
              </button>
            `
            : null}
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--ghost"
            onClick=${(e) => { e.stopPropagation(); onDelete(sid); }}
            title="删除会话"
          >
            <${Icon} d=${I_TRASH} />
          </button>
        </span>
      </button>
      ${expanded
        ? html`
          <div class="xmc-h-srow__body" style="flex:0 0 100%;width:100%">
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
            style=${"flex:0 0 100%;width:100%;padding:.25rem .8rem .5rem 2.4rem;"
              + "color:var(--color-fg-muted, rgba(127,127,127,.85));"
              + "font-family:var(--xmc-mono, monospace);"
              + "font-size:.78em;line-height:1.45;"
              + "white-space:pre-wrap;word-break:break-word"}
          >…${matchSnippet}…</div>
        `
        : null}
    </div>
  `;
}


// ── DeleteConfirmDialog (port of components/DeleteConfirmDialog.tsx) ──

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
