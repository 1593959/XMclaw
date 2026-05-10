// XMclaw — Memory ▸ "记忆活动" tab (2026-05-10).
//
// Shipped per user feedback: "我的目的是给他自己用，不是光给我用."
// Pre-this-tab Memory page only exposed surfaces the OPERATOR drives
// (identity / notes / journal / hand-typed unified-query). This tab
// shows what the AGENT itself is doing with UnifiedMemorySystem —
// the live timeline of MEMORY_RECALL (auto-read on turn start) +
// MEMORY_PUT_AUTO (auto-write after MemoryExtractor approves).
//
// Data source: GET /api/v2/events?types=memory_recall,memory_put_auto.
// Polls every 5 s while mounted. No WS subscribe — events.db is the
// canonical history surface and the volume is low enough (~1 RECALL
// per turn + much rarer PUT_AUTO) that polling is honest.

const { h } = window.__xmc.preact;
const { useState, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { useSafeFetch } from "../../lib/use_safe_fetch.js";

function fmtTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) {
    return String(ts);
  }
}

function AxesBadge({ axes }) {
  if (!axes || !axes.length) {
    return html`<span style="opacity:.5;font-size:.7rem">—</span>`;
  }
  const colors = {
    semantic: "#3498db",
    relation: "#9b59b6",
    temporal: "#f39c12",
  };
  return html`
    <span style="display:inline-flex;gap:4px;flex-wrap:wrap">
      ${axes.map((a) => html`
        <span key=${a} style=${`font-size:.7rem;padding:1px 6px;border-radius:3px;background:${(colors[a] || "#888")}22;color:${colors[a] || "#888"};border:1px solid ${(colors[a] || "#888")}55`}>
          ${a}
        </span>
      `)}
    </span>
  `;
}

function RecallRow({ ev }) {
  const p = ev.payload || {};
  const hits = Array.isArray(p.hits) ? p.hits : [];
  return html`
    <div style="border-left:3px solid #3498db;padding:.5rem .75rem;margin-bottom:.4rem;background:var(--color-background);border-radius:0 6px 6px 0">
      <div style="display:flex;justify-content:space-between;font-size:.7rem;opacity:.7">
        <span>📥 RECALL · ${fmtTime(ev.ts)}</span>
        <span>${p.elapsed_ms != null ? `${p.elapsed_ms}ms` : ""}</span>
      </div>
      <div style="font-size:.85rem;margin-top:.2rem;word-break:break-word">
        <span style="opacity:.6">查询：</span>${p.query || "(空)"}
      </div>
      <div style="font-size:.75rem;opacity:.7;margin-top:.2rem">
        命中 ${hits.length} 条 · 上限 ${p.limit ?? "?"}
      </div>
      ${hits.length > 0 ? html`
        <details style="margin-top:.4rem">
          <summary style="cursor:pointer;font-size:.75rem;opacity:.7">展开命中</summary>
          <div style="display:flex;flex-direction:column;gap:.3rem;margin-top:.4rem">
            ${hits.map((h) => html`
              <div key=${h.id} style="font-size:.78rem;padding:.3rem .5rem;background:var(--color-surface);border-radius:4px">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
                  <${AxesBadge} axes=${h.matched_axes} />
                  <span style="opacity:.6;font-size:.7rem">score ${h.score} · ${h.layer || "?"}</span>
                </div>
                <div style="margin-top:.2rem;word-break:break-word">${h.text}</div>
              </div>
            `)}
          </div>
        </details>
      ` : null}
    </div>
  `;
}

function PutRow({ ev }) {
  const p = ev.payload || {};
  return html`
    <div style="border-left:3px solid #2ecc71;padding:.5rem .75rem;margin-bottom:.4rem;background:var(--color-background);border-radius:0 6px 6px 0">
      <div style="display:flex;justify-content:space-between;font-size:.7rem;opacity:.7">
        <span>💾 PUT · ${fmtTime(ev.ts)}</span>
        <span style="opacity:.6">${p.layer || "?"} / ${p.node_type || "event"}</span>
      </div>
      <div style="font-size:.85rem;margin-top:.2rem;word-break:break-word">
        ${p.text || "(empty)"}
      </div>
      ${p.reason ? html`
        <div style="font-size:.72rem;opacity:.65;margin-top:.2rem;font-style:italic">
          理由：${p.reason}
        </div>
      ` : null}
      <div style="font-size:.7rem;opacity:.5;margin-top:.2rem;font-family:var(--xmc-font-mono, monospace)">
        id ${p.id || "?"}
      </div>
    </div>
  `;
}

export function ActivityTab({ token }) {
  const [data, setData] = useState({ events: [] });
  const url = "/api/v2/events?types=memory_recall,memory_put_auto&limit=200";
  const { loading, error, refresh } = useSafeFetch(url, token, setData);

  const onRefresh = useCallback(() => { refresh(); }, [refresh]);

  if (loading && (!data.events || data.events.length === 0)) {
    return html`
      <div class="xmc-h-loading" role="status" aria-live="polite" style="padding:2rem;text-align:center">
        加载记忆活动…
      </div>
    `;
  }
  if (error) {
    return html`
      <div class="xmc-h-error" role="alert" style="margin:1rem 0">
        <strong>记忆活动加载失败</strong>
        <div style="font-size:.78rem;opacity:.85;margin-top:4px">
          ${String(error.message || error)}
        </div>
        <button type="button" class="xmc-h-btn" style="margin-top:.5rem" onClick=${onRefresh}>重试</button>
      </div>
    `;
  }

  // Newest first; events.db returns ascending by default.
  const events = (data.events || []).slice().reverse();
  const recallCount = events.filter((e) => e.type === "memory_recall").length;
  const putCount = events.filter((e) => e.type === "memory_put_auto").length;

  return html`
    <section>
      <div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:8px;padding:.75rem 1rem;margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
          <div>
            <strong>Agent 自动读/写 UnifiedMemorySystem 的最近活动</strong>
            <div style="font-size:.78rem;opacity:.7;margin-top:.2rem">
              📥 RECALL ${recallCount} 次 · 💾 PUT ${putCount} 次（最近 200 条）
            </div>
          </div>
          <button type="button" class="xmc-h-btn" onClick=${onRefresh}>
            ${loading ? "加载中…" : "刷新"}
          </button>
        </div>
        <div style="font-size:.72rem;opacity:.6;margin-top:.5rem;line-height:1.4">
          这是 agent 自己驱动的记忆 trace（Phase A 自动 recall + Phase B 自动 put）。
          想手填查询请用旁边的<strong>统一查询 (调试)</strong> tab。
          没有事件？说明 daemon 启动后还没有 turn，或者 ${"`"}cfg.memory.unified_recall.enabled = false${"`"}。
        </div>
      </div>

      ${events.length === 0 ? html`
        <div style="opacity:.6;font-size:.9rem;padding:2rem;text-align:center;border:1px dashed var(--color-border);border-radius:8px">
          暂无记忆活动 — 跟 agent 对一轮看看
        </div>
      ` : html`
        <div>
          ${events.map((ev) => ev.type === "memory_recall"
            ? html`<${RecallRow} key=${ev.id || ev.ts} ev=${ev} />`
            : html`<${PutRow} key=${ev.id || ev.ts} ev=${ev} />`)}
        </div>
      `}
    </section>
  `;
}
