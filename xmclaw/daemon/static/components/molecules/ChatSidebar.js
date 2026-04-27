// XMclaw — ChatSidebar (Hermes ChatPage right-rail)
//
// Hermes ChatSidebar is gateway-heavy because it doubles as the
// session-info panel for an embedded TUI. Our equivalent surfaces:
//   1. Active session metadata (sid + ws state + tool count)
//   2. Recent sessions list (last 10) — click resume / delete
//   3. New-chat button
//
// Ports the visual structure (right-rail card with sectioned panels)
// without the JSON-RPC plumbing — we already have everything we need
// in /api/v2/sessions + /api/v2/status.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_PLUS  = "M12 5v14 M5 12h14";
const I_TRASH = "M3 6h18 M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2";
const I_PLAY  = "M5 4 19 12 5 20Z";
const I_REFRESH = "M3 12a9 9 0 0 1 15-6.7L21 8 M21 3v5h-5 M21 12a9 9 0 0 1-15 6.7L3 16 M3 21v-5h5";

function timeAgo(epoch) {
  if (!epoch) return "—";
  const ms = Math.max(0, Date.now() - epoch * 1000);
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 48) return h + "h";
  return Math.floor(h / 24) + "d";
}

export function ChatSidebar({
  token,
  activeSid,
  connectionStatus,
  toolsCount,
  onNewSession,
}) {
  const [sessions, setSessions] = useState(null);
  const [busy, setBusy] = useState(null);

  const load = useCallback(() => {
    apiGet("/api/v2/sessions?limit=10", token)
      .then((d) => setSessions(d.sessions || []))
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [load]);

  const onResume = (sid) => {
    if (sid === activeSid) {
      toast.info("已经是当前会话");
      return;
    }
    try { localStorage.setItem("xmc.active_sid", sid); } catch (_) {}
    window.location.reload();
  };

  const onDelete = async (sid, e) => {
    e.stopPropagation();
    if (!confirm("删除这个会话？")) return;
    setBusy(sid);
    try {
      const res = await fetch(
        `/api/v2/sessions/${encodeURIComponent(sid)}` +
          (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      load();
      toast.success("已删除");
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const wsTone =
    connectionStatus === "connected"   ? "success"
  : connectionStatus === "reconnecting" ? "warning"
  : connectionStatus === "auth_failed"  ? "destructive"
  : "muted";

  return html`
    <aside class="xmc-h-chatside" aria-label="chat sidebar">

      <section class="xmc-h-chatside__section">
        <header class="xmc-h-chatside__head">
          <span>当前会话</span>
        </header>
        <div class="xmc-h-chatside__active">
          <div class="xmc-h-chatside__row">
            <span class="xmc-h-chatside__row-key">sid</span>
            <code class="xmc-h-chatside__row-val" title=${activeSid || ""}>
              ${activeSid || "—"}
            </code>
          </div>
          <div class="xmc-h-chatside__row">
            <span class="xmc-h-chatside__row-key">ws</span>
            <span class=${"xmc-h-badge xmc-h-badge--" + wsTone}>
              ${connectionStatus || "—"}
            </span>
          </div>
          <div class="xmc-h-chatside__row">
            <span class="xmc-h-chatside__row-key">工具</span>
            <span class="xmc-h-chatside__row-val">${toolsCount || 0}</span>
          </div>
        </div>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--primary xmc-h-chatside__newbtn"
          onClick=${onNewSession}
        >
          <${Icon} d=${I_PLUS} />
          新建会话
        </button>
      </section>

      <section class="xmc-h-chatside__section">
        <header class="xmc-h-chatside__head">
          <span>最近 10 条会话</span>
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--ghost xmc-h-chatside__refresh"
            onClick=${load}
            title="刷新"
          ><${Icon} d=${I_REFRESH} /></button>
        </header>
        <ul class="xmc-h-chatside__list">
          ${sessions === null
            ? html`<li class="xmc-h-loading">载入中…</li>`
            : sessions.length === 0
              ? html`<li class="xmc-h-empty">还没有保存的会话</li>`
              : sessions.map((s) => html`
                <li
                  key=${s.session_id}
                  class=${"xmc-h-chatside__item " + (s.session_id === activeSid ? "is-active" : "")}
                  onClick=${() => onResume(s.session_id)}
                >
                  <code class="xmc-h-chatside__item-sid" title=${s.session_id}>
                    ${(s.session_id || "").slice(0, 18)}
                  </code>
                  <span class="xmc-h-chatside__item-meta">
                    ${s.message_count || 0}m · ${timeAgo(s.updated_at)}
                  </span>
                  <button
                    type="button"
                    class="xmc-h-chatside__item-trash"
                    title="删除"
                    onClick=${(e) => onDelete(s.session_id, e)}
                    disabled=${busy === s.session_id}
                  ><${Icon} d=${I_TRASH} /></button>
                </li>
              `)}
        </ul>
      </section>
    </aside>
  `;
}
