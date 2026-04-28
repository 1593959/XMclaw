// XMclaw — SessionsPage 1:1 port of hermes-agent/web/src/pages/SessionsPage.tsx
//
// Hermes default route. Layout:
//   - Page title + search input + count chip
//   - Scrollable list of SessionRow cards
//       * Source icon (we map session-id-prefix → icon since our store
//         doesn't carry a `source` column yet)
//       * Title / session id (mono)
//       * Turn count + relative timestamp
//       * Expand toggle (chevron)
//       * Delete button (trash; opens confirm dialog)
//   - Expanded card body shows message list with role-styled bubbles
//     (user / assistant / system / tool). Each tool_call collapses
//     into a chevron-toggleable block.
//
// Data wiring: hits the new /api/v2/sessions surface
// (xmclaw/daemon/routers/sessions.py). FTS5 search is stubbed —
// `query` filters client-side by substring against session_id +
// loaded message bodies. Phase B-9 will add a real FTS5 search route.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { lex, renderTokenHtml } from "../lib/markdown.js";
import { toast } from "../lib/toast.js";

// Map session-id prefix → source-config (icon glyph + color class).
// Mirrors hermes SOURCE_CONFIG (SessionsPage.tsx:52-60). Our prefixes:
//   chat-…   → cli (default Web UI session)
//   live-…   → cli
//   tg-…     → telegram
//   feishu-… → feishu
//   wecom-…  → wecom
const SOURCE_CONFIG = {
  cli:      { glyph: "▮", label: "CLI" },
  telegram: { glyph: "✈", label: "Telegram" },
  discord:  { glyph: "#", label: "Discord" },
  slack:    { glyph: "≡", label: "Slack" },
  feishu:   { glyph: "✦", label: "Feishu" },
  wecom:    { glyph: "❖", label: "WeCom" },
  cron:     { glyph: "⏱", label: "Cron" },
  unknown:  { glyph: "○", label: "Unknown" },
};

function inferSource(sid) {
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

function timeAgo(epoch) {
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

// ── Inline icon SVGs (lucide-react equivalents used by Hermes) ────

function Icon({ d, className }) {
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

const I_SEARCH = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
const I_CHEVRON_DOWN = "m6 9 6 6 6-6";
const I_CHEVRON_RIGHT = "m9 18 6-6-6-6";
const I_TRASH = "M3 6h18 M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2";
const I_X = "M18 6 6 18 M6 6l12 12";
const I_PLAY = "M5 4 19 12 5 20Z";
const I_LOADER = "M21 12a9 9 0 1 1-6.219-8.56";

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

// ── MessageBubble (port of SessionsPage MessageBubble) ────────────

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

// ── Expanded message list ─────────────────────────────────────────

function MessageList({ messages, highlight }) {
  return html`
    <div class="xmc-h-msglist">
      ${messages.map((m, i) => html`
        <${MessageBubble} key=${i} msg=${m} highlight=${highlight} />
      `)}
    </div>
  `;
}

// ── SessionRow (one card per session) ─────────────────────────────

function SessionRow({ session, query, expanded, onToggle, onDelete, onResume, token }) {
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
    <div class="xmc-h-srow" key=${sid}>
      <button
        type="button"
        class="xmc-h-srow__head"
        onClick=${onToggle}
        aria-expanded=${expanded ? "true" : "false"}
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
          <div class="xmc-h-srow__body">
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
    </div>
  `;
}

// ── DeleteConfirmDialog (port of components/DeleteConfirmDialog.tsx) ─

function DeleteConfirmDialog({ sid, onCancel, onConfirm, busy }) {
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

// ── SessionsPage main ────────────────────────────────────────────

export function SessionsPage({ token }) {
  const [sessions, setSessions] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(new Set());
  const [pendingDelete, setPendingDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/sessions?limit=200", token)
      .then((d) => { if (!cancelled) setSessions(d.sessions || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  const filtered = useMemo(() => {
    if (!sessions) return [];
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) => {
      const sid = (s.session_id || "").toLowerCase();
      const preview = (s.preview || "").toLowerCase();
      return sid.includes(q) || preview.includes(q);
    });
  }, [sessions, query]);

  const onToggle = (sid) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid); else next.add(sid);
      return next;
    });
  };

  const onResume = (sid) => {
    try {
      // Match the store's localStorage key (store.js:62) so app.js
      // boot()'s readActiveSid picks it up on the next mount.
      localStorage.setItem("xmc.active_sid", sid);
    } catch (_) {}
    toast.info(`正在切换到会话 ${sid.slice(0, 12)}…`);
    // Hard reload so the WS reconnects with the new sid; navigate
    // to /chat at the same time so the user lands on the right page.
    window.location.assign("/ui/chat");
  };

  const onDeleteConfirm = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      const res = await fetch(
        `/api/v2/sessions/${encodeURIComponent(pendingDelete)}`
        + (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSessions((prev) => (prev || []).filter((s) => s.session_id !== pendingDelete));
      toast.success("会话已删除");
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  };

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="sessions-title">
        <header class="xmc-h-page__header">
          <h2 id="sessions-title" class="xmc-h-page__title">会话</h2>
        </header>
        <div class="xmc-h-page__body">
          <div class="xmc-h-error">${error}</div>
        </div>
      </section>
    `;
  }

  return html`
    <section class="xmc-h-page" aria-labelledby="sessions-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="sessions-title" class="xmc-h-page__title">会话</h2>
          <p class="xmc-h-page__subtitle">
            历史会话保存在 <code>~/.xmclaw/v2/sessions.db</code>。点击展开消息，回放 / 删除均不可逆。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <span class="xmc-h-badge">${(sessions || []).length} 个</span>
        </div>
      </header>

      <div class="xmc-h-page__body">
        <div class="xmc-h-srow__searchbar">
          <span class="xmc-h-srow__searchicon">
            <${Icon} d=${I_SEARCH} />
          </span>
          <input
            type="search"
            class="xmc-h-input"
            placeholder="搜索会话 id…"
            value=${query}
            onInput=${(e) => setQuery(e.target.value)}
          />
        </div>

        ${sessions === null
          ? html`<div class="xmc-h-loading">载入中…</div>`
          : filtered.length === 0
            ? html`<div class="xmc-h-empty">${query ? "没有匹配的会话。" : "还没有保存的会话 — 在 Chat 页发条消息试试。"}</div>`
            : html`
              <div class="xmc-h-srow__list">
                ${filtered.map((s) => html`
                  <${SessionRow}
                    key=${s.session_id}
                    session=${s}
                    query=${query}
                    expanded=${expanded.has(s.session_id)}
                    onToggle=${() => onToggle(s.session_id)}
                    onDelete=${(sid) => setPendingDelete(sid)}
                    onResume=${onResume}
                    token=${token}
                  />
                `)}
              </div>
            `}
      </div>

      <${DeleteConfirmDialog}
        sid=${pendingDelete}
        busy=${deleting}
        onCancel=${() => !deleting && setPendingDelete(null)}
        onConfirm=${onDeleteConfirm}
      />
    </section>
  `;
}
