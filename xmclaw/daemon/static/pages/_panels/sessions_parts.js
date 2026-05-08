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
