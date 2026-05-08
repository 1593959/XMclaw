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
// Data wiring: hits the /api/v2/sessions surface
// (xmclaw/daemon/routers/sessions.py).
//
// Search:
//   * /api/v2/sessions/search?q=… (B-339) — substring scan over the
//     stored history JSON. Returns the same shape as list + a
//     `match_snippet` field so the UI can show a context window
//     around the hit. This is a SQL LIKE scan, not FTS5; for
//     personal-scale daemons (hundreds of sessions, KB-MB each)
//     latency is low-hundreds-ms. FTS5 with triggers is a future
//     optimization.
//   * Local-only filtering still happens for the in-memory
//     already-loaded message bodies so typing in the search box
//     gives instant feedback before the server round-trip lands.
// (Pre-B-339 only the local filter existed; sessions you hadn't
// expanded weren't searchable at all.)

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";
// B-323 + B-341: SessionRow / SOURCE_CONFIG / inferSource / timeAgo
// + Icon / SVG paths / MessageList / DeleteConfirmDialog all live in
// pages/_panels/sessions_parts.js. Keeps this page under the
// 500-line UI budget (FRONTEND_DESIGN.md §1.4).
import {
  Icon, I_SEARCH,
  SessionRow,
  DeleteConfirmDialog,
} from "./_panels/sessions_parts.js";


// ── SessionsPage main ────────────────────────────────────────────

export function SessionsPage({ token }) {
  const [sessions, setSessions] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(new Set());
  const [pendingDelete, setPendingDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
  // B-156: 批量选择状态 + 内部 session 过滤开关
  const [selected, setSelected] = useState(new Set());
  const [showInternal, setShowInternal] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  // B-341 (audit pass-2 #7): server-side hits from
  // /api/v2/sessions/search?q=… keyed by session_id. The local
  // filter still runs against `sessions` for instant feedback on
  // already-loaded preview/sid; this map adds sessions whose
  // *message bodies* (not loaded into the page) contain the query.
  // Pre-B-341 the search box only filtered the 200-row recent list
  // by sid + preview, so older or message-body-only matches were
  // invisible — the B-339 endpoint shipped but had no caller.
  const [serverHits, setServerHits] = useState({});
  const [searchBusy, setSearchBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/sessions?limit=200", token)
      .then((d) => { if (!cancelled) setSessions(d.sessions || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  // B-341 (audit pass-2 #7): debounced server-side message-body search.
  // Skip queries < 2 chars (too noisy + wastes a round-trip for what
  // the local filter already covers). 300ms debounce keeps typing
  // smooth while still feeling immediate by the time the user stops.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setServerHits({});
      setSearchBusy(false);
      return undefined;
    }
    let cancelled = false;
    setSearchBusy(true);
    const timer = setTimeout(() => {
      apiGet(
        `/api/v2/sessions/search?q=${encodeURIComponent(q)}&limit=50`,
        token,
      )
        .then((d) => {
          if (cancelled) return;
          const next = {};
          for (const row of d.sessions || []) {
            if (row.session_id) next[row.session_id] = row;
          }
          setServerHits(next);
        })
        .catch(() => { if (!cancelled) setServerHits({}); })
        .finally(() => { if (!cancelled) setSearchBusy(false); });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, token]);

  // B-156: identify internal sessions (reflection / dream / etc.) the
  // daemon spawns for self-bookkeeping. Filtering them out by default
  // closes the "same conversation appears 4 times" bug — reflections
  // copy the user's history, so each spawned reflect:<sid>:<ts> shows
  // the same first user message and looks like a duplicate chat.
  const isInternalSid = (sid) => {
    if (!sid) return false;
    return (
      sid.startsWith("reflect:")
      || sid.startsWith("dream:")
      || sid.startsWith("_system:")
      || sid.startsWith("evolution:")
    );
  };

  const filtered = useMemo(() => {
    if (!sessions) return [];
    const q = query.trim().toLowerCase();
    // B-341 (audit pass-2 #7): merge server-side hits with the
    // already-loaded recent list. A row counts as "matching" if:
    //   1. local filter matches (sid / preview), OR
    //   2. server search returned it (message-body match).
    // Server hits not in the recent-200 list are appended at the
    // end so the user can find conversations that aged out of the
    // initial fetch but still contain their query in the body.
    const seen = new Set();
    const out = [];
    for (const s of sessions) {
      const sid = s.session_id || "";
      if (!showInternal && isInternalSid(sid)) continue;
      const preview = (s.preview || "").toLowerCase();
      const localMatch = !q
        || sid.toLowerCase().includes(q)
        || preview.includes(q);
      const serverMatch = q && serverHits[sid] !== undefined;
      if (localMatch || serverMatch) {
        seen.add(sid);
        out.push(s);
      }
    }
    if (q) {
      for (const sid of Object.keys(serverHits)) {
        if (seen.has(sid)) continue;
        if (!showInternal && isInternalSid(sid)) continue;
        out.push(serverHits[sid]);
      }
    }
    return out;
  }, [sessions, query, showInternal, serverHits]);

  // B-156: how many internal sessions are hidden right now (for the
  // toggle's count badge).
  const internalCount = useMemo(() => {
    return (sessions || []).filter((s) => isInternalSid(s.session_id)).length;
  }, [sessions]);

  // B-156: keep selected set in sync with filtered list — when
  // filter changes, drop ids no longer visible.
  const onToggleSelect = (sid) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid); else next.add(sid);
      return next;
    });
  };
  const onSelectAllVisible = () => {
    const visibleIds = filtered.map((s) => s.session_id);
    const allSelected = visibleIds.every((id) => selected.has(id));
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(visibleIds));
    }
  };
  const onClearSelection = () => setSelected(new Set());

  const onBulkDelete = async () => {
    if (selected.size === 0 || bulkBusy) return;
    // B-160: 用项目内置 confirmDialog 替换 window.confirm —
    // 后者样式跟主题完全不搭、位置在浏览器顶部突兀
    const ok = await confirmDialog({
      title: `批量删除 ${selected.size} 个会话`,
      body: "操作不可撤销。所有勾选的会话历史会从 sessions.db 永久移除。",
      confirmLabel: "删除",
      confirmTone: "danger",
    });
    if (!ok) return;
    setBulkBusy(true);
    let okCount = 0;
    let failCount = 0;
    const ids = [...selected];
    for (const sid of ids) {
      try {
        const res = await fetch(
          `/api/v2/sessions/${encodeURIComponent(sid)}`
          + (token ? `?token=${encodeURIComponent(token)}` : ""),
          { method: "DELETE" },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        okCount++;
      } catch (_e) {
        failCount++;
      }
    }
    setSessions((prev) => (prev || []).filter((s) => !selected.has(s.session_id)));
    setSelected(new Set());
    setBulkBusy(false);
    // B-160: 跨页失效广播 — ChatSidebar 等监听者立即刷新
    try {
      window.dispatchEvent(new CustomEvent("xmc:sessions:changed"));
    } catch (_) { /* old browsers */ }
    if (failCount === 0) {
      toast.success(`已删除 ${okCount} 个会话`);
    } else {
      toast.error(`删除完成：成功 ${okCount}，失败 ${failCount}`);
    }
  };

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
      // B-160: cross-page broadcast (ChatSidebar listens)
      try {
        window.dispatchEvent(new CustomEvent("xmc:sessions:changed"));
      } catch (_) { /* old browsers */ }
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

  const allVisibleSelected =
    filtered.length > 0 && filtered.every((s) => selected.has(s.session_id));

  return html`
    <section class="xmc-h-page" aria-labelledby="sessions-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="sessions-title" class="xmc-h-page__title">会话</h2>
          <p class="xmc-h-page__subtitle">
            历史会话保存在 <code>~/.xmclaw/v2/sessions.db</code>。
            <strong>B-156</strong>：默认隐藏 <code>reflect:</code> /
            <code>dream:</code> 等内部 session（agent 自反思临时产物）；可勾选批量删除。
          </p>
        </div>
        <div class="xmc-h-page__actions" style="display:flex;gap:.4rem;align-items:center">
          <span class="xmc-h-badge">${filtered.length} 个</span>
          ${internalCount > 0
            ? html`<label style="display:flex;align-items:center;gap:.3rem;font-size:.75rem;cursor:pointer">
                <input type="checkbox" checked=${showInternal} onChange=${(e) => setShowInternal(e.target.checked)} />
                显示内部 (${internalCount})
              </label>`
            : null}
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
            placeholder="搜索会话 id / 内容…"
            value=${query}
            onInput=${(e) => setQuery(e.target.value)}
          />
        </div>

        <!-- B-156: 批量选择工具栏 — 仅在有可见 session 时显示 -->
        ${filtered.length > 0
          ? html`<div style="display:flex;align-items:center;gap:.6rem;padding:.4rem .6rem;margin:.4rem 0;border:1px solid var(--color-border);border-radius:6px;background:color-mix(in srgb, var(--midground) 4%, transparent);font-size:.8rem">
              <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer">
                <input
                  type="checkbox"
                  checked=${allVisibleSelected}
                  onChange=${onSelectAllVisible}
                  title=${allVisibleSelected ? "取消全选" : "全选当前可见"}
                />
                <span>${allVisibleSelected ? "全部已选" : "全选可见"}</span>
              </label>
              ${selected.size > 0
                ? html`<span style="display:contents">
                    <span style="opacity:.7">已选 <strong>${selected.size}</strong> 个</span>
                    <button
                      class="xmc-h-btn xmc-h-btn--danger"
                      style="padding:.25rem .7rem;font-size:.75rem;background:color-mix(in srgb,var(--color-destructive) 20%,transparent);color:var(--color-destructive);border:1px solid color-mix(in srgb,var(--color-destructive) 40%,transparent)"
                      onClick=${onBulkDelete}
                      disabled=${bulkBusy}
                    >${bulkBusy ? "删除中…" : `🗑 批量删除 (${selected.size})`}</button>
                    <button
                      class="xmc-h-btn xmc-h-btn--ghost"
                      style="padding:.2rem .55rem;font-size:.72rem;margin-left:auto"
                      onClick=${onClearSelection}
                      disabled=${bulkBusy}
                    >清除选择</button>
                  </span>`
                : html`<span style="opacity:.55">勾选行批量操作</span>`}
            </div>`
          : null}

        ${sessions === null
          ? html`<div class="xmc-h-loading">载入中…</div>`
          : filtered.length === 0
            ? html`<div class="xmc-h-empty">${
                query ? "没有匹配的会话。"
                : (sessions.length > 0 && !showInternal
                    ? "可见会话已过滤完。点击右上角 '显示内部' 看 reflect:/dream: 等。"
                    : "还没有保存的会话 — 在 Chat 页发条消息试试。")
              }</div>`
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
                    isSelected=${selected.has(s.session_id)}
                    onToggleSelect=${() => onToggleSelect(s.session_id)}
                    matchSnippet=${(serverHits[s.session_id] || s).match_snippet || null}
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
