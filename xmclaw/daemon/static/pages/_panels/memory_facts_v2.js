// Memory Panel — L1 Facts v2 tab.
//
// Phase 5b. Consumes the /api/v2/memory/v2/* router built in Phase 5a.
// Shows the live L1 facts (Fact / Relation) the daemon writes via
// KeyInfoExtractor + future memorize tool. Three sections:
//
//   1. Filter chips: kind / scope / layer + free-text search
//   2. List view: each row = one fact with its source-event link +
//      delete button + confidence + evidence_count
//   3. Manual add form: lets the user inject a fact directly (kind
//      + scope + text). Useful when something the daemon missed
//      needs to land in memory immediately.
//
// Graph viz lives in a separate tab (Phase 5c, vis-network).
//
// When /api/v2/memory/v2/status returns enabled=false, the panel
// renders a banner with instructions for flipping the config flag
// rather than crashing — same defensive posture as ChannelsPage
// when no channel adapter is wired.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost, apiDelete } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";


// ── Constants ─────────────────────────────────────────────────────


const KIND_OPTIONS = [
  { id: "", label: "全部 kind" },
  { id: "preference", label: "preference" },
  { id: "decision", label: "decision" },
  { id: "identity", label: "identity" },
  { id: "commitment", label: "commitment" },
  { id: "correction", label: "correction" },
  { id: "project", label: "project" },
  { id: "episode", label: "episode" },
];

const SCOPE_OPTIONS = [
  { id: "", label: "全部 scope" },
  { id: "user", label: "user" },
  { id: "project", label: "project" },
  { id: "session", label: "session" },
];

const KIND_COLOR = {
  preference: "#5a8ed6",
  decision: "#9e6ed4",
  identity: "#5cb37d",
  commitment: "#d4a35c",
  correction: "#d05a5a",
  project: "#d68b3f",
  episode: "#8d8d8d",
};


// ── Sub-component: status banner ──────────────────────────────────


function StatusBanner({ status }) {
  if (!status) return null;
  if (status.enabled === false) {
    return html`
      <div
        class="xmc-h-warn"
        role="alert"
        style="margin:.6rem 0;padding:.7rem .9rem;border:1px dashed var(--color-warning,#c98a3a);border-radius:6px;background:color-mix(in srgb, var(--color-warning, #c98a3a) 8%, transparent)"
      >
        <strong>Memory v2 未启用</strong>
        <div style="font-size:.85rem;margin-top:.3rem;line-height:1.5">
          在 <code>daemon/config.json</code> 设置
          <code>cognition.memory_v2.enabled = true</code>，重启 daemon。
          需要安装 <code>pip install 'xmclaw[memory-v2]'</code>
          (lancedb + pyarrow)。
        </div>
      </div>
    `;
  }
  if (!status.healthy) {
    return html`
      <div
        class="xmc-h-error"
        role="alert"
        style="margin:.6rem 0;padding:.7rem .9rem;border:1px solid var(--color-destructive);border-radius:6px"
      >
        <strong>Memory v2 异常</strong>
        <div style="font-size:.85rem;margin-top:.3rem">${status.error || "（未知错误）"}</div>
      </div>
    `;
  }
  return html`
    <div
      style="margin:.4rem 0;padding:.5rem .8rem;background:color-mix(in srgb, var(--color-success) 8%, transparent);border-radius:6px;display:flex;gap:1rem;align-items:center;flex-wrap:wrap;font-size:.85rem"
    >
      <span><strong>${status.fact_count}</strong> facts</span>
      <span>embedder: <code>${status.embedder_name}</code> (dim ${status.embedder_dim})</span>
    </div>
  `;
}


// ── Sub-component: filter bar ─────────────────────────────────────


function FilterBar({ kind, scope, q, onChange }) {
  return html`
    <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.6rem 0">
      <select
        value=${kind}
        onChange=${(e) => onChange({ kind: e.target.value, scope, q })}
        style="padding:.35rem .5rem"
      >
        ${KIND_OPTIONS.map((opt) => html`
          <option key=${opt.id} value=${opt.id}>${opt.label}</option>
        `)}
      </select>
      <select
        value=${scope}
        onChange=${(e) => onChange({ kind, scope: e.target.value, q })}
        style="padding:.35rem .5rem"
      >
        ${SCOPE_OPTIONS.map((opt) => html`
          <option key=${opt.id} value=${opt.id}>${opt.label}</option>
        `)}
      </select>
      <input
        type="text"
        placeholder="关键字搜索…"
        value=${q}
        onInput=${(e) => onChange({ kind, scope, q: e.target.value })}
        style="flex:1;min-width:200px;padding:.35rem .55rem"
      />
    </div>
  `;
}


// ── Sub-component: fact row ───────────────────────────────────────


function FactRow({ fact, onDelete, onSelect }) {
  const color = KIND_COLOR[fact.kind] || "#888";
  const tsStr = new Date(fact.ts_last * 1000).toISOString().replace("T", " ").slice(0, 19);
  return html`
    <article
      class="xmc-mem-fact"
      style="display:flex;gap:.6rem;align-items:flex-start;padding:.6rem .8rem;border:1px solid var(--color-border);border-radius:6px;margin-bottom:.4rem;background:var(--xmc-bg-elev)"
    >
      <span
        class="xmc-mem-fact__kind-dot"
        style=${`width:.5rem;height:.5rem;border-radius:50%;background:${color};flex-shrink:0;margin-top:.4rem`}
        title=${fact.kind}
      ></span>
      <div style="flex:1;min-width:0">
        <div style="font-size:.92rem;line-height:1.5;word-break:break-word">${fact.text}</div>
        <div style="margin-top:.3rem;display:flex;gap:.8rem;font-size:.72rem;color:var(--xmc-fg-muted);flex-wrap:wrap">
          <span><code>${fact.kind}</code> · <code>${fact.scope}</code></span>
          <span>conf <strong>${fact.confidence.toFixed(2)}</strong></span>
          <span>evidence <strong>${fact.evidence_count}</strong></span>
          <span>layer ${fact.layer}</span>
          <span>${tsStr}</span>
          ${fact.source_event_id
            ? html`<span title=${fact.source_event_id}>📍 来源 ${fact.source_event_id.slice(0, 18)}…</span>`
            : null}
        </div>
        ${fact.contradicts && fact.contradicts.length > 0
          ? html`
              <div style="margin-top:.25rem;font-size:.72rem;color:var(--color-destructive)">
                ⚠ 与 ${fact.contradicts.length} 条事实矛盾
              </div>
            `
          : null}
      </div>
      <div style="display:flex;gap:.3rem;flex-shrink:0">
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${() => onSelect(fact.id)}
          style="font-size:.72rem;padding:.25rem .55rem"
          title="查看关系图"
        >🔗</button>
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${() => onDelete(fact)}
          style="font-size:.72rem;padding:.25rem .55rem"
          title="删除"
        >🗑</button>
      </div>
    </article>
  `;
}


// ── Sub-component: add-fact form ──────────────────────────────────


function AddFactForm({ token, onCreated }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("project");
  const [scope, setScope] = useState("project");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await apiPost("/api/v2/memory/v2/facts", { text: text.trim(), kind, scope }, token);
      toast.success("已添加");
      setText("");
      setOpen(false);
      onCreated();
    } catch (e) {
      toast.error("添加失败: " + (e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return html`
      <button
        type="button"
        class="xmc-h-btn"
        onClick=${() => setOpen(true)}
        style="margin:.4rem 0"
      >+ 手动添加事实</button>
    `;
  }

  return html`
    <div style="margin:.6rem 0;padding:.8rem;border:1px solid var(--color-border);border-radius:6px">
      <div style="display:flex;gap:.5rem;margin-bottom:.5rem">
        <select value=${kind} onChange=${(e) => setKind(e.target.value)} style="padding:.35rem .5rem">
          ${KIND_OPTIONS.filter((o) => o.id).map((o) => html`<option key=${o.id} value=${o.id}>${o.label}</option>`)}
        </select>
        <select value=${scope} onChange=${(e) => setScope(e.target.value)} style="padding:.35rem .5rem">
          ${SCOPE_OPTIONS.filter((o) => o.id).map((o) => html`<option key=${o.id} value=${o.id}>${o.label}</option>`)}
        </select>
      </div>
      <textarea
        placeholder="事实内容（一句话陈述句）…"
        value=${text}
        onInput=${(e) => setText(e.target.value)}
        rows="3"
        style="width:100%;padding:.5rem;font:inherit;box-sizing:border-box"
      ></textarea>
      <div style="display:flex;gap:.5rem;justify-content:flex-end;margin-top:.5rem">
        <button type="button" class="xmc-h-btn" onClick=${() => setOpen(false)}>取消</button>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--primary"
          disabled=${busy || !text.trim()}
          onClick=${submit}
        >${busy ? "保存中…" : "保存"}</button>
      </div>
    </div>
  `;
}


// ── Main tab component ────────────────────────────────────────────


export function FactsV2Tab({ token }) {
  const [status, setStatus] = useState(null);
  const [facts, setFacts] = useState([]);
  const [filters, setFilters] = useState({ kind: "", scope: "", q: "" });
  const [loading, setLoading] = useState(false);
  const [selectedFactId, setSelectedFactId] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.kind) params.set("kind", filters.kind);
      if (filters.scope) params.set("scope", filters.scope);
      if (filters.q) params.set("q", filters.q);
      params.set("limit", "100");
      const url = `/api/v2/memory/v2/facts?${params.toString()}`;
      const r = await apiGet(url, token);
      if (r && Array.isArray(r.facts)) {
        setFacts(r.facts);
      }
    } catch (e) {
      // 503 → v2 disabled, status banner will catch it
      if (!String(e?.message || "").includes("503")) {
        toast.error("加载失败: " + (e?.message || e));
      }
    } finally {
      setLoading(false);
    }
  }, [filters, token]);

  const refreshStatus = useCallback(async () => {
    try {
      const r = await apiGet("/api/v2/memory/v2/status", token);
      setStatus(r);
    } catch (e) {
      setStatus({ enabled: false, reason: String(e?.message || e) });
    }
  }, [token]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    if (status && status.enabled) {
      refresh();
    }
  }, [status, refresh]);

  const handleDelete = async (fact) => {
    if (!window.confirm(`删除 "${fact.text.slice(0, 60)}"？`)) return;
    try {
      await apiDelete(`/api/v2/memory/v2/facts/${encodeURIComponent(fact.id)}`, token);
      toast.success("已删除");
      refresh();
      refreshStatus();
    } catch (e) {
      toast.error("删除失败: " + (e?.message || e));
    }
  };

  return html`
    <section>
      <${StatusBanner} status=${status} />
      ${status && status.enabled
        ? html`
            <${AddFactForm} token=${token} onCreated=${() => { refresh(); refreshStatus(); }} />
            <${FilterBar} ...${filters} onChange=${setFilters} />
            ${loading
              ? html`<div style="opacity:.7;padding:1rem 0">加载中…</div>`
              : facts.length === 0
                ? html`<div style="opacity:.7;padding:1rem 0">暂无事实（过滤条件不匹配，或库为空）。</div>`
                : html`
                    <div style="margin:.6rem 0;font-size:.78rem;color:var(--xmc-fg-muted)">
                      显示 ${facts.length} 条
                    </div>
                    ${facts.map((f) => html`
                      <${FactRow}
                        key=${f.id}
                        fact=${f}
                        onDelete=${handleDelete}
                        onSelect=${setSelectedFactId}
                      />
                    `)}
                  `
            }
            ${selectedFactId
              ? html`
                  <div style="margin-top:1rem;padding:.8rem;border:1px dashed var(--color-border);border-radius:6px;background:var(--xmc-bg-elev-2,var(--xmc-bg-elev))">
                    <div style="font-size:.78rem;color:var(--xmc-fg-muted);margin-bottom:.4rem">
                      事实关系子图（Phase 5c 力导向 vis-network 即将到位）
                    </div>
                    <code style="font-size:.72rem;word-break:break-all">${selectedFactId}</code>
                    <button
                      type="button"
                      class="xmc-h-btn"
                      onClick=${() => setSelectedFactId(null)}
                      style="margin-left:.5rem;font-size:.72rem"
                    >×</button>
                  </div>
                `
              : null}
          `
        : null}
    </section>
  `;
}
