// XMclaw — SessionsPage (Nebula redesign v2)
// Replaced with Nebula prototype design: stats cards, toolbar, enhanced session rows.
// Kept existing API wiring (/api/v2/sessions) and data flow.
// Delete confirmation now uses the shared confirmDialog.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { Skeleton } from "../components/atoms/skeleton.js";
import { confirmDialog } from "../lib/dialog.js";
import { Vitals, VitalsCell, Readout } from "../components/molecules/Instrument.js";
import {
  SessionRow,
} from "./_panels/sessions_parts.js";

function isToday(epoch) {
  if (!epoch) return false;
  const d = new Date(epoch * 1000);
  const now = new Date();
  return (
    d.getDate() === now.getDate() &&
    d.getMonth() === now.getMonth() &&
    d.getFullYear() === now.getFullYear()
  );
}

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

export function SessionsPage({ token }) {
  const [sessions, setSessions] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(new Set());
  const [filter, setFilter] = useState("all"); // 'all' | 'active' | 'archived'
  const [archived, setArchived] = useState(new Set());
  const [deletingId, setDeletingId] = useState(null);
  const [serverHits, setServerHits] = useState({});
  const [searchBusy, setSearchBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/sessions?limit=200", token)
      .then((d) => { if (!cancelled) setSessions(d.sessions || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

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

  const stats = useMemo(() => {
    const list = sessions || [];
    const total = list.length;
    const todayActive = list.filter((s) => isToday(s.updated_at)).length;
    const totalMessages = list.reduce((sum, s) => sum + (s.message_count || 0), 0);
    const archivedCount = archived.size;
    return { total, todayActive, totalMessages, archivedCount };
  }, [sessions, archived]);

  const filtered = useMemo(() => {
    if (!sessions) return [];
    const q = query.trim().toLowerCase();
    const seen = new Set();
    const out = [];
    for (const s of sessions) {
      const sid = s.session_id || "";
      if (isInternalSid(sid)) continue;
      const preview = (s.preview || "").toLowerCase();
      const localMatch = !q || sid.toLowerCase().includes(q) || preview.includes(q);
      const serverMatch = q && serverHits[sid] !== undefined;
      if (localMatch || serverMatch) {
        seen.add(sid);
        out.push(s);
      }
    }
    if (q) {
      for (const sid of Object.keys(serverHits)) {
        if (seen.has(sid)) continue;
        if (isInternalSid(sid)) continue;
        out.push(serverHits[sid]);
      }
    }
    if (filter === "active") return out.filter((s) => !archived.has(s.session_id));
    if (filter === "archived") return out.filter((s) => archived.has(s.session_id));
    return out;
  }, [sessions, query, serverHits, filter, archived]);

  const onToggle = (sid) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid); else next.add(sid);
      return next;
    });
  };

  const onResume = (sid) => {
    try {
      localStorage.setItem("xmc.active_sid", sid);
    } catch (_) {}
    toast.info(`正在切换到会话 ${sid.slice(0, 12)}…`);
    window.location.assign("/ui/chat");
  };

  const onArchive = (sid) => {
    const willArchive = !archived.has(sid);
    setArchived((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid); else next.add(sid);
      return next;
    });
    toast.success(willArchive ? "已归档" : "已取消归档");
  };

  const onExport = (sid) => {
    toast.info(`导出 ${sid.slice(0, 12)}…（功能开发中）`);
  };

  const onDelete = async (sid) => {
    const ok = await confirmDialog({
      title: "确认删除会话",
      body: `这将永久删除会话历史 ${sid} 及其所有消息。此操作不可撤销。`,
      confirmLabel: "删除",
      confirmTone: "danger",
    });
    if (!ok) return;
    setDeletingId(sid);
    try {
      const res = await fetch(
        `/api/v2/sessions/${encodeURIComponent(sid)}`
        + (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSessions((prev) => (prev || []).filter((s) => s.session_id !== sid));
      setArchived((prev) => {
        const next = new Set(prev);
        next.delete(sid);
        return next;
      });
      try {
        window.dispatchEvent(new CustomEvent("xmc:sessions:changed"));
      } catch (_) {}
      toast.success("会话已删除");
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    } finally {
      setDeletingId(null);
    }
  };

  if (error) {
    return html`
      <section class="xmc-datapage" aria-labelledby="sessions-title">
        <header class="xmc-datapage__header">
          <h2 id="sessions-title">会话管理</h2>
          <p class="xmc-datapage__subtitle">加载失败：${error}</p>
        </header>
      </section>
    `;
  }

  return html`
    <section class="xmc-datapage" aria-labelledby="sessions-title">
      <header class="xmc-datapage__header">
        <h2 id="sessions-title">会话管理</h2>
        <p class="xmc-datapage__subtitle">查看和管理所有历史会话</p>
      </header>

      <!-- Vitals 读数条（统一仪表台形态） -->
      <${Vitals}>
        <${VitalsCell}><${Readout} label="总会话" value=${stats.total} unit="sessions" /></${VitalsCell}>
        <${VitalsCell}><${Readout} label="今日活跃" value=${stats.todayActive} unit="today" /></${VitalsCell}>
        <${VitalsCell}><${Readout} label="消息总数" value=${stats.totalMessages} unit="msgs" /></${VitalsCell}>
        <${VitalsCell}><${Readout} label="已归档" value=${stats.archivedCount} unit="archived" /></${VitalsCell}>
      </${Vitals}>

      <!-- Toolbar -->
      <div class="nb-session-toolbar">
        <div class="nb-session-search">
          <input
            type="search"
            placeholder="搜索会话..."
            value=${query}
            onInput=${(e) => setQuery(e.target.value)}
          />
        </div>
        <div class="nb-session-filter">
          <button class=${filter === "all" ? "active" : ""} onClick=${() => setFilter("all")}>全部</button>
          <button class=${filter === "active" ? "active" : ""} onClick=${() => setFilter("active")}>活跃</button>
          <button class=${filter === "archived" ? "active" : ""} onClick=${() => setFilter("archived")}>归档</button>
        </div>
      </div>

      <!-- List -->
      ${sessions === null
        ? html`<div style="padding:1rem"><${Skeleton} lines=${5} /></div>`
        : filtered.length === 0
          ? html`<div class="xmc-h-empty">${
              query
                ? "没有匹配的会话。"
                : html`<div style="text-align:center;padding:2rem"><div style="font-size:1.1rem;margin-bottom:.5rem">💬 还没有会话</div><div style="font-size:.85rem;opacity:.7;margin-bottom:1rem">在 Chat 页发条消息，会话会自动保存到这里。</div><a href="/ui/chat" style="display:inline-block;padding:.5rem 1rem;background:var(--xmc-accent);color:var(--xmc-accent-fg);border-radius:6px;text-decoration:none;font-size:.85rem">去对话 →</a></div>`
            }</div>`
          : html`
            <div class="nb-session-list">
              ${filtered.map((s) => html`
                <${SessionRow}
                  key=${s.session_id}
                  session=${s}
                  query=${query}
                  expanded=${expanded.has(s.session_id)}
                  onToggle=${() => onToggle(s.session_id)}
                  onDelete=${onDelete}
                  onResume=${onResume}
                  onArchive=${onArchive}
                  onExport=${onExport}
                  token=${token}
                  isArchived=${archived.has(s.session_id)}
                  deletingId=${deletingId}
                  matchSnippet=${(serverHits[s.session_id] || s).match_snippet || null}
                />
              `)}
            </div>
          `}
    </section>
  `;
}
