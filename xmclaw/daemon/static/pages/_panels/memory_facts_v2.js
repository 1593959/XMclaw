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
import { FactsGraphView } from "./memory_facts_v2_graph.js";
import { EmbedderInfoPanel } from "./memory_facts_v2_embedder.js";


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
  { id: "lesson", label: "lesson (经验教训)" },
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
  lesson: "#5cb3a6",
};


// ── Sub-component: status banner ──────────────────────────────────


function StatusBanner({ status, onRetry }) {
  if (!status) return null;
  // 2026-05-26 (followup): three distinct failure modes used to all
  // render as "Memory v2 未启用" — confusing both for users (the
  // pairing-token hotfix surfaced this) and operators trying to
  // debug. Now we distinguish:
  //   * transportError → 401 / network / token-not-ready / generic
  //   * enabled === false  → real config flag off (503 + body
  //                          ``error: memory_v2_disabled``)
  //   * healthy === false  → service constructed but degraded
  if (status.transportError) {
    return html`
      <div
        class="xmc-h-error"
        role="alert"
        style="margin:.6rem 0;padding:.7rem .9rem;border:1px solid var(--color-destructive);border-radius:6px;background:color-mix(in srgb, var(--color-destructive) 8%, transparent)"
      >
        <strong>无法加载 Memory v2 状态</strong>
        <div style="font-size:.85rem;margin-top:.3rem;line-height:1.5">
          ${status.reason || "（未知错误）"}
        </div>
        <div style="font-size:.78rem;margin-top:.35rem;color:var(--xmc-fg-muted)">
          常见原因：daemon 未启动 · 配对 token 不一致 ·
          /api/v2/pair 返回了非 hex 格式。检查 daemon 日志 +
          浏览器控制台。
        </div>
        ${onRetry ? html`
          <button
            type="button"
            onClick=${onRetry}
            style="margin-top:.5rem;padding:.3rem .7rem;font-size:.8rem"
          >重试</button>
        ` : null}
      </div>
    `;
  }
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


function FactRow({
  fact, onForget, onHardDelete, onCorrect, onRestore, onSelect,
}) {
  const color = KIND_COLOR[fact.kind] || "#888";
  const tsStr = new Date(fact.ts_last * 1000).toISOString().replace("T", " ").slice(0, 19);
  // 2026-05-26: distinguish soft-forget tombstone vs real supersede
  // (which points at a survivor fact_id). Surface both so the user
  // can either restore (if they regret deletion) or follow the
  // supersede chain.
  const isForgotten = fact.forgotten || fact.superseded_by === "__forgotten__";
  const isSuperseded = !isForgotten && Boolean(fact.superseded_by);
  return html`
    <article
      class="xmc-mem-fact"
      style=${
        `display:flex;gap:.6rem;align-items:flex-start;padding:.6rem .8rem;`
        + `border:1px solid var(--color-border);border-radius:6px;`
        + `margin-bottom:.4rem;background:var(--xmc-bg-elev);`
        + (isForgotten || isSuperseded ? "opacity:.55;" : "")
      }
    >
      <span
        class="xmc-mem-fact__kind-dot"
        style=${`width:.5rem;height:.5rem;border-radius:50%;background:${color};flex-shrink:0;margin-top:.4rem`}
        title=${fact.kind}
      ></span>
      <div style="flex:1;min-width:0">
        <div style="font-size:.92rem;line-height:1.5;word-break:break-word">
          ${isForgotten || isSuperseded
            ? html`<s>${fact.text}</s>`
            : fact.text}
        </div>
        <div style="margin-top:.3rem;display:flex;gap:.8rem;font-size:.72rem;color:var(--xmc-fg-muted);flex-wrap:wrap">
          <span><code>${fact.kind}</code> · <code>${fact.scope}</code></span>
          ${fact.bucket
            ? html`<span title="renders into persona MD bucket">→ <code>${fact.bucket}</code></span>`
            : null}
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
        ${isForgotten
          ? html`
              <div style="margin-top:.25rem;font-size:.72rem;color:var(--color-warning,#c98a3a)">
                🗑 已 forget — recall / persona render 跳过
              </div>
            `
          : isSuperseded
          ? html`
              <div style="margin-top:.25rem;font-size:.72rem;color:var(--xmc-fg-muted)">
                ↻ 已被 <code>${fact.superseded_by.slice(0, 18)}…</code> 替代
              </div>
            `
          : null}
      </div>
      <div style="display:flex;gap:.3rem;flex-shrink:0;flex-wrap:wrap;max-width:160px;justify-content:flex-end">
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${() => onSelect(fact.id)}
          style="font-size:.72rem;padding:.25rem .55rem"
          title="查看关系图"
        >🔗</button>
        ${(isForgotten || isSuperseded)
          ? html`
              <button
                type="button"
                class="xmc-h-btn"
                onClick=${() => onRestore(fact)}
                style="font-size:.72rem;padding:.25rem .55rem"
                title="撤销 forget/supersede，让 fact 重新生效"
              >↺ 恢复</button>
              <button
                type="button"
                class="xmc-h-btn"
                onClick=${() => onHardDelete(fact)}
                style="font-size:.72rem;padding:.25rem .55rem"
                title="彻底删除（不可恢复）"
              >🗑×</button>
            `
          : html`
              <button
                type="button"
                class="xmc-h-btn"
                onClick=${() => onCorrect(fact)}
                style="font-size:.72rem;padding:.25rem .55rem"
                title="改正这条事实（旧的标 superseded，新的写入）"
              >✎ 改正</button>
              <button
                type="button"
                class="xmc-h-btn"
                onClick=${() => onForget(fact)}
                style="font-size:.72rem;padding:.25rem .55rem"
                title="软删除（可恢复）"
              >🗑</button>
            `}
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
  // 2026-05-26: surface forgotten / superseded rows so the user can
  // audit + restore. Default off — most-of-the-time view is the
  // active set; flip on when debugging or after an accidental
  // forget.
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selectedFactId, setSelectedFactId] = useState(null);
  // Phase 5c — view toggle: list | graph. Default list for low-jank
  // first paint (graph viz needs a CDN fetch on first open).
  const [view, setView] = useState("list");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.kind) params.set("kind", filters.kind);
      if (filters.scope) params.set("scope", filters.scope);
      if (filters.q) params.set("q", filters.q);
      if (showSuperseded) params.set("include_superseded", "true");
      params.set("limit", "100");
      const url = `/api/v2/memory/v2/facts?${params.toString()}`;
      const r = await apiGet(url, token);
      if (r && Array.isArray(r.facts)) {
        setFacts(r.facts);
      }
    } catch (e) {
      // 503 → v2 disabled, status banner will catch it. Use the
      // structured ``err.status`` rather than substring-matching
      // the message (which broke when error formatting changed).
      // TokenNotReadyError is also silent — the auth slice will
      // catch up and re-trigger refresh via the callback dep.
      const silent = (
        e?.status === 503
        || e?.name === "TokenNotReadyError"
      );
      if (!silent) {
        toast.error("加载失败: " + (e?.message || e));
      }
    } finally {
      setLoading(false);
    }
  }, [filters, showSuperseded, token]);

  const refreshStatus = useCallback(async () => {
    try {
      const r = await apiGet("/api/v2/memory/v2/status", token);
      setStatus(r);
    } catch (e) {
      // 2026-05-26 (followup): distinguish "v2 config flag is off"
      // (server's deliberate 503 with body.error === memory_v2_disabled)
      // from every other failure mode (401, network, token-not-
      // ready, generic). Pre-fix all of these collapsed to
      // ``enabled: false`` which surfaced as "Memory v2 未启用" —
      // the same misleading banner the user hit after the F1
      // hotfix landed, even though the service was fine.
      const isDisabled = (
        e?.status === 503
        && e?.body?.error === "memory_v2_disabled"
      );
      if (isDisabled) {
        setStatus({ enabled: false, reason: e?.body?.detail || "" });
      } else if (e?.name === "TokenNotReadyError") {
        // Token still propagating from the auth slice — don't
        // render anything yet. The useCallback dep on ``token``
        // will trigger another refresh once the value lands.
        setStatus(null);
      } else {
        setStatus({
          transportError: true,
          reason: String(e?.message || e),
          status: e?.status || null,
        });
      }
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

  // 2026-05-26 (F1 hotfix): default action is SOFT forget — the
  // row stays on disk under a tombstone so the user can restore.
  // Hard delete is a separate "permanently remove" action exposed
  // via shift-click on the trash icon (escape hatch for ops / GDPR).
  const handleForget = async (fact) => {
    if (!window.confirm(
      `软删除 "${fact.text.slice(0, 60)}"？\n\n` +
      `(标 forgotten；下次 recall / persona render 跳过它，` +
      `但保留在 LanceDB 可恢复)`
    )) return;
    try {
      await apiPost(
        `/api/v2/memory/v2/facts/${encodeURIComponent(fact.id)}/forget`,
        { reason: "user clicked trash in memory panel" },
        token,
      );
      toast.success("已 forget（可恢复）");
      refresh();
      refreshStatus();
    } catch (e) {
      toast.error("forget 失败: " + (e?.message || e));
    }
  };

  const handleHardDelete = async (fact) => {
    if (!window.confirm(
      `**彻底删除** "${fact.text.slice(0, 60)}"？\n\n` +
      `这会从 LanceDB 物理移除该行，无法恢复。` +
      `一般用 forget 即可；只在需要满足 GDPR 风格的 ` +
      `"必须彻底擦除" 场景才用 hard delete。`
    )) return;
    try {
      await apiDelete(
        `/api/v2/memory/v2/facts/${encodeURIComponent(fact.id)}`,
        token,
      );
      toast.success("已彻底删除");
      refresh();
      refreshStatus();
    } catch (e) {
      toast.error("delete 失败: " + (e?.message || e));
    }
  };

  const handleRestore = async (fact) => {
    try {
      await apiPost(
        `/api/v2/memory/v2/facts/${encodeURIComponent(fact.id)}/restore`,
        {},
        token,
      );
      toast.success("已恢复");
      refresh();
      refreshStatus();
    } catch (e) {
      toast.error("restore 失败: " + (e?.message || e));
    }
  };

  const handleCorrect = async (fact) => {
    const newText = window.prompt(
      `改正 "${fact.text.slice(0, 80)}"\n\n` +
      `输入正确的事实文本（新事实会标 high-confidence, ` +
      `旧事实会被 superseded）：`,
      fact.text,
    );
    if (!newText || newText.trim() === "" || newText.trim() === fact.text) return;
    try {
      const r = await apiPost(
        "/api/v2/memory/v2/facts/correct",
        {
          old_text: fact.text,
          new_text: newText.trim(),
          kind: fact.kind,
          scope: fact.scope,
          bucket: fact.bucket,
        },
        token,
      );
      if (r.matched) {
        toast.success(`已纠正（distance=${r.distance}）`);
      } else {
        toast.info("匹配距离太大，已作为新事实写入（未 supersede 旧的）");
      }
      refresh();
      refreshStatus();
    } catch (e) {
      toast.error("correct 失败: " + (e?.message || e));
    }
  };

  const runDedup = async (dryRun) => {
    try {
      const r = await apiPost(
        "/api/v2/memory/v2/deduplicate",
        { dry_run: dryRun },
        token,
      );
      if (dryRun) {
        const lines = [
          `dry-run 完成 — 扫描 ${r.scanned} 条`,
          `发现 ${r.clusters_found} 组近似事实`,
          `若执行将合并掉 ${r.merged} 条 (保留 evidence + supersedes 链)`,
        ];
        if (r.actions && r.actions.length) {
          lines.push("");
          lines.push("示例 (前 3 组):");
          for (const a of r.actions.slice(0, 3)) {
            lines.push(`• 保留: ${a.survivor_text}`);
            for (const t of (a.loser_texts || []).slice(0, 2)) {
              lines.push(`   合并: ${t}`);
            }
          }
        }
        window.alert(lines.join("\n"));
      } else {
        toast.success(`已合并 ${r.merged} 条近似事实 (扫描 ${r.scanned} 条)`);
        refresh();
        refreshStatus();
      }
    } catch (e) {
      toast.error("dedup 失败: " + (e?.message || e));
    }
  };

  // Sub-view toggle buttons: list | graph | embedder.
  const ViewToggle = () => html`
    <div style="display:flex;gap:.4rem;margin:.6rem 0;flex-wrap:wrap">
      <button
        type="button"
        class=${"xmc-h-btn" + (view === "list" ? " xmc-h-btn--primary" : "")}
        onClick=${() => setView("list")}
        style="font-size:.85rem"
      >📋 列表</button>
      <button
        type="button"
        class=${"xmc-h-btn" + (view === "graph" ? " xmc-h-btn--primary" : "")}
        onClick=${() => setView("graph")}
        style="font-size:.85rem"
      >🕸 图谱</button>
      <button
        type="button"
        class=${"xmc-h-btn" + (view === "embedder" ? " xmc-h-btn--primary" : "")}
        onClick=${() => setView("embedder")}
        style="font-size:.85rem"
      >🧬 向量模型</button>
    </div>
  `;

  return html`
    <section>
      <${StatusBanner} status=${status} onRetry=${refreshStatus} />
      ${status && status.enabled
        ? html`
            <${ViewToggle} />
            ${view === "list"
              ? html`
                  <${AddFactForm} token=${token} onCreated=${() => { refresh(); refreshStatus(); }} />
                  <div style="display:flex;gap:.4rem;margin:.4rem 0;flex-wrap:wrap;align-items:center">
                    <span style="font-size:.78rem;color:var(--xmc-fg-muted)">整理:</span>
                    <button
                      type="button"
                      class="xmc-h-btn"
                      onClick=${() => runDedup(true)}
                      style="font-size:.78rem;padding:.25rem .6rem"
                      title="预览 — 看一下有多少近似事实会被合并，不实际写入"
                    >🔍 dry-run</button>
                    <button
                      type="button"
                      class="xmc-h-btn"
                      onClick=${() => {
                        if (window.confirm("执行去重合并？近似事实将合并到 evidence_count 最高的那条，被合并方标 superseded_by。")) runDedup(false);
                      }}
                      style="font-size:.78rem;padding:.25rem .6rem"
                      title="扫描所有事实，把 cosine 距离 < 0.15 的近似项合并到同一行"
                    >🧹 一键去重</button>
                  </div>
                  <${FilterBar} ...${filters} onChange=${setFilters} />
                  <label
                    style="display:inline-flex;align-items:center;gap:.3rem;margin:.3rem 0 .6rem;font-size:.78rem;color:var(--xmc-fg-muted);cursor:pointer"
                    title="包含已 forget 或被 supersede 的 fact（默认隐藏）"
                  >
                    <input
                      type="checkbox"
                      checked=${showSuperseded}
                      onChange=${(e) => setShowSuperseded(e.target.checked)}
                    />
                    显示 forgotten / superseded 事实
                  </label>
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
                              onForget=${handleForget}
                              onHardDelete=${handleHardDelete}
                              onCorrect=${handleCorrect}
                              onRestore=${handleRestore}
                              onSelect=${(fid) => { setSelectedFactId(fid); setView("graph"); }}
                            />
                          `)}
                        `
                  }
                `
              : view === "graph"
              ? html`
                  <${FactsGraphView}
                    token=${token}
                    focusFactId=${selectedFactId}
                    onFocusFact=${setSelectedFactId}
                  />
                `
              : html`<${EmbedderInfoPanel} token=${token} />`}
          `
        : null}
    </section>
  `;
}
