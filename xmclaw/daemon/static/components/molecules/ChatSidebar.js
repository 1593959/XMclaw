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
import { confirmDialog } from "../../lib/dialog.js";

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
  onResumeSession,
}) {
  const [sessions, setSessions] = useState(null);
  const [busy, setBusy] = useState(null);

  // B-160: filter out internal sessions (reflect:/dream:/etc) — same
  // rule as Sessions page (B-156). Prevents the sidebar from showing
  // 4 "从现在开始..." rows because each WS disconnect spawns one.
  // Keep this list in sync with Sessions.js's isInternalSid and the
  // backend's INTERNAL_SESSION_PREFIXES in session_store.py.
  const isInternalSid = (sid) => {
    if (!sid) return false;
    return (
      sid.startsWith("reflect:")
      || sid.startsWith("dream:")
      || sid.startsWith("_system")
      || sid.startsWith("evolution:")
      || sid.startsWith("autonomous:")
      || sid.startsWith("skill-dream")
      || sid.startsWith("step_")
      || sid.startsWith("smoke-")
      || sid.startsWith("selfmod-")
      || sid.startsWith("time-fullb20")
    );
  };

  const load = useCallback(() => {
    // Pull more rows so post-filter we still have ~10 to show.
    apiGet("/api/v2/sessions?limit=40", token)
      .then((d) => {
        const all = d.sessions || [];
        setSessions(all.filter((s) => !isInternalSid(s.session_id)).slice(0, 10));
      })
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    load();
    // B-221: poll bumped 5s→30s. Real-data audit showed 12 sessions
    // requests per port in rapid succession (poll + WS-driven
    // xmc:sessions:changed bursts); the 5s cadence was the main
    // culprit behind "every page loads forever". The apiGet
    // in-flight cache (lib/api.js B-221) absorbs the redundancy
    // for tighter intervals, but cutting the base poll itself
    // keeps the daemon log uncluttered too.
    const id = setInterval(load, 30_000);
    // B-160: cross-page invalidation. Sessions page (or any page) can
    // dispatch ``xmc:sessions:changed`` to force every listener to
    // reload immediately instead of waiting for the next poll tick.
    // B-221: debounce — bursts of changed events (rapid
    // create/delete) collapse to one load.
    let debounceId = null;
    const onChanged = () => {
      if (debounceId) clearTimeout(debounceId);
      debounceId = setTimeout(() => {
        debounceId = null;
        load();
      }, 250);
    };
    window.addEventListener("xmc:sessions:changed", onChanged);
    return () => {
      clearInterval(id);
      if (debounceId) clearTimeout(debounceId);
      window.removeEventListener("xmc:sessions:changed", onChanged);
    };
  }, [load]);

  const onResume = (sid) => {
    if (sid === activeSid) {
      toast.info("已经是当前会话");
      return;
    }
    // Wave-32+ UX fix: prefer the prop-injected in-app resume
    // (no page reload, no black screen). Fall back to the old
    // localStorage+reload path only when the parent didn't wire
    // a handler — e.g. older callers that haven't been updated.
    if (typeof onResumeSession === "function") {
      onResumeSession(sid);
      return;
    }
    try { localStorage.setItem("xmc.active_sid", sid); } catch (_) {}
    window.location.reload();
  };

  const onDelete = async (sid, e) => {
    e.stopPropagation();
    const ok = await confirmDialog({
      title: "删除会话",
      body: "对话历史一同清除，操作不可撤销。",
      confirmLabel: "删除",
      confirmTone: "danger",
    });
    if (!ok) return;
    setBusy(sid);
    try {
      const res = await fetch(
        `/api/v2/sessions/${encodeURIComponent(sid)}` +
          (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      load();
      // B-160: cross-page broadcast so /sessions auto-refreshes too
      try {
        window.dispatchEvent(new CustomEvent("xmc:sessions:changed"));
      } catch (_) { /* old browsers */ }
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
        ${/* 「新建会话」按钮已移除：全站统一用左栏「新对话」作唯一入口（2026-06-05）。 */ null}
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
                  ${s.preview
                    ? html`
                        <span class="xmc-h-chatside__item-title" title=${s.session_id}
                              style="display:block;font-size:.78rem;color:var(--color-fg);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                          ${s.preview}
                        </span>
                      `
                    : html`
                        <code class="xmc-h-chatside__item-sid" title=${s.session_id}>
                          ${(s.session_id || "").slice(0, 18)}
                        </code>
                      `}
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

      <${BackgroundTasksPanel} token=${token} onResumeSession=${onResumeSession} />
    </aside>
  `;
}


// Sprint 1 Wave 3: Background tasks panel.
//
// Polls /api/v2/agent_tasks every 5s. Renders one row per
// submit_to_agent task: status badge, content preview (≤80 chars),
// agent + elapsed. Reuses the same chat sidebar styling.
//
// Polling discipline (added after the user observed every page
// load taking forever):
//   1. In-flight gate — don't fire a new request until the previous
//      one returns. The old setInterval-without-tracking fired a
//      new request every 3s regardless of state, stacking 6+
//      simultaneous calls and exhausting the browser's HTTP/1.1
//      6-connection budget. That made EVERY other API call on the
//      page (status, facts list, etc.) queue 15+ seconds.
//   2. Pause when tab hidden — saves the daemon work AND keeps the
//      connection pool clean for the active tab.
//   3. Pause when collapsed — already there; kept.
function BackgroundTasksPanel({ token, onResumeSession }) {
  const [tasks, setTasks] = useState(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    if (!token || collapsed) return;
    let cancelled = false;
    let inFlight = false;

    const load = async () => {
      if (cancelled || inFlight) return;
      if (typeof document !== "undefined" && document.hidden) return;
      inFlight = true;
      try {
        const r = await apiGet("/api/v2/agent_tasks", token);
        if (!cancelled && Array.isArray(r?.tasks)) setTasks(r.tasks);
      } catch (_e) { /* network blip — keep last list */ }
      finally { inFlight = false; }
    };

    load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token, collapsed]);

  const inFlight = (tasks || []).filter(
    (t) => t.status === "pending" || t.status === "running",
  ).length;

  // 2026-06-06：把内容近乎相同的后台任务折叠成一条 + ×N。反思/诚实自检
  // 每轮都跑、跑完保留 10 分钟，会堆出一排几乎一样的条目（"reflection —
  // the user just…" / "honesty check"）。按 agent+preview 归并，保留最新。
  const shownTasks = (() => {
    const list = tasks || [];
    const norm = (s) => (s || "").trim().toLowerCase().replace(/\s+/g, " ").slice(0, 60);
    const map = new Map();
    for (const t of list) {
      const key = (t.agent_id || "") + "|" + norm(t.preview);
      const ex = map.get(key);
      if (!ex) { map.set(key, { ...t, _count: 1 }); continue; }
      ex._count += 1;
      if ((t.created_at || 0) >= (ex.created_at || 0)) {
        const c = ex._count;
        Object.assign(ex, t, { _count: c });
      }
    }
    return [...map.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  })();

  return html`
    <section class="xmc-h-chatside__section xmc-bgtasks">
      <header class="xmc-h-chatside__head"
              onClick=${() => setCollapsed(!collapsed)}
              style="cursor:pointer;user-select:none">
        <span>后台任务 ${inFlight > 0
          ? html`<span class="xmc-h-pill" style="background:#3a823a;color:#fff;font-size:.6rem;padding:1px 5px;border-radius:8px;margin-left:4px">${inFlight} 跑中</span>`
          : null}</span>
        <span style="font-size:.7rem;opacity:.5">${collapsed ? "▸" : "▾"}</span>
      </header>
      ${collapsed ? null : html`
        <ul class="xmc-h-chatside__list">
          ${tasks === null
            ? html`<li class="xmc-h-loading">载入中…</li>`
            : shownTasks.length === 0
            ? html`<li class="xmc-h-empty">无后台任务</li>`
            : shownTasks.slice(0, 12).map((t) => {
              const canOpen = !!(t.session_id && typeof onResumeSession === "function");
              const reply = t.reply_preview;
              return html`
              <li
                key=${t.task_id}
                class="xmc-h-chatside__item xmc-bgtask__item"
                title=${(t.preview || "") + (reply ? "\n→ " + reply : "")}
                onClick=${canOpen ? () => onResumeSession(t.session_id) : undefined}
                style=${"display:flex;flex-direction:column;gap:2px;cursor:" + (canOpen ? "pointer" : "default")}
              >
                <span style="display:flex;align-items:center;gap:4px;font-size:.72rem">
                  <span class=${"xmc-bgtask__status xmc-bgtask__status--" + t.status}
                        style=${"display:inline-block;width:6px;height:6px;border-radius:50%;background:" +
                          (t.status === "running" ? "#4a90e2"
                           : t.status === "done" ? "#3a823a"
                           : t.status === "error" ? "#c84a4a"
                           : "#888")
                        }></span>
                  <span style="color:var(--color-fg-muted)">${t.agent_id || "main"}</span>
                  ${typeof t.hops === "number" && t.hops > 0
                    ? html`<span style="color:var(--color-fg-muted);font-size:.65rem">·${t.hops}hop</span>`
                    : null}
                  ${t._count > 1
                    ? html`<span title=${`折叠了 ${t._count} 条相同任务`} style="font-size:.6rem;padding:0 5px;border-radius:8px;background:rgba(139,92,246,.22);color:var(--nb-accent-light,#A78BFA)">×${t._count}</span>`
                    : null}
                  <span style="margin-left:auto;color:var(--color-fg-muted);font-size:.65rem">
                    ${t.elapsed_s ? Math.round(t.elapsed_s) + "s" : "—"}
                  </span>
                </span>
                <span style="font-size:.78rem;color:var(--color-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px"
                      title=${t.preview}>
                  ${t.preview || "(no preview)"}
                </span>
                ${t.status === "done" && reply
                  ? html`<span style="font-size:.7rem;color:var(--color-fg-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px;font-style:italic" title=${reply}>
                      → ${reply.length > 50 ? reply.slice(0, 50) + "…" : reply}
                    </span>`
                  : null}
                ${t.status === "error" && t.error
                  ? html`<span style="font-size:.65rem;color:#c84a4a">${String(t.error).slice(0, 60)}</span>`
                  : null}
              </li>
              `;
            })}
        </ul>
      `}
    </section>
  `;
}
