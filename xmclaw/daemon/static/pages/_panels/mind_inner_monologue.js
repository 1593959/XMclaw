// XMclaw — Mind ▸ "内心独白" panel (R6, 2026-05-10).
//
// Surfaces the agent's running self-talk emitted by R1's
// ReflectionCycle.reflect_recent — the user can finally see what
// the agent is thinking about between explicit user turns. This is
// the "贾维斯" affordance the user asked for: trust through
// transparency.
//
// Data: GET /api/v2/events?types=inner_monologue,reflection_cycle_ran
// Polled every 8s while mounted (low volume; no need for WS push).

const { h } = window.__xmc.preact;
const { useState, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { useSafeFetch } from "../../lib/use_safe_fetch.js";

const KIND_COLORS = {
  reflection: "#3498db",
  wonder:     "#9b59b6",
  concern:    "#e74c3c",
  plan:       "#2ecc71",
  observation:"#7f8c8d",
};

const KIND_ICONS = {
  reflection: "🤔",
  wonder:     "❓",
  concern:    "⚠️",
  plan:       "📋",
  observation:"👁",
};

function fmtTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) { return String(ts); }
}

function ThoughtRow({ ev }) {
  const p = ev.payload || {};
  const kind = String(p.kind || "observation").toLowerCase();
  const color = KIND_COLORS[kind] || "#888";
  const icon = KIND_ICONS[kind] || "💭";
  return html`
    <div style=${`border-left:3px solid ${color};padding:.5rem .75rem;margin-bottom:.4rem;background:var(--color-background);border-radius:0 6px 6px 0`}>
      <div style="display:flex;justify-content:space-between;font-size:.7rem;opacity:.7">
        <span>${icon} <strong>${kind}</strong> · ${fmtTime(ev.ts)}</span>
        <span style="opacity:.6">${p.trigger || ""}</span>
      </div>
      <div style="font-size:.88rem;margin-top:.2rem;word-break:break-word">
        ${p.text || ""}
      </div>
    </div>
  `;
}

function CycleRow({ ev }) {
  const p = ev.payload || {};
  const patterns = Array.isArray(p.patterns_found) ? p.patterns_found : [];
  return html`
    <div style="border-left:3px solid #f39c12;padding:.5rem .75rem;margin-bottom:.4rem;background:var(--color-background);border-radius:0 6px 6px 0">
      <div style="font-size:.7rem;opacity:.7">
        🔄 ReflectionCycle · ${fmtTime(ev.ts)} · 看了最近 ${p.lookback_n ?? "?"} 件事 · ${p.elapsed_ms ?? "?"}ms
      </div>
      ${patterns.length > 0 ? html`
        <ul style="margin:.3rem 0 0 .8rem;padding:0;font-size:.78rem;opacity:.85">
          ${patterns.map((s, i) => html`<li key=${i}>${s}</li>`)}
        </ul>
      ` : html`<div style="font-size:.78rem;opacity:.6;margin-top:.2rem">没找到值得反思的</div>`}
    </div>
  `;
}

export function InnerMonologuePanel({ token }) {
  const [data, setData] = useState({ events: [] });
  const url = "/api/v2/events?types=inner_monologue,reflection_cycle_ran&limit=200";
  const { loading, error, refresh } = useSafeFetch(url, token, setData);
  const onRefresh = useCallback(() => { refresh(); }, [refresh]);

  if (loading && (!data.events || data.events.length === 0)) {
    return html`
      <div class="xmc-h-loading" role="status" aria-live="polite" style="padding:2rem;text-align:center">
        加载内心独白…
      </div>
    `;
  }
  if (error) {
    return html`
      <div class="xmc-h-error" role="alert">
        <strong>内心独白加载失败</strong>
        <div style="font-size:.78rem;opacity:.85;margin-top:4px">
          ${String(error.message || error)}
        </div>
        <button type="button" class="xmc-h-btn" style="margin-top:.5rem" onClick=${onRefresh}>重试</button>
      </div>
    `;
  }

  const events = (data.events || []).slice().reverse();
  const thoughts = events.filter((e) => e.type === "inner_monologue");
  const cycles = events.filter((e) => e.type === "reflection_cycle_ran");

  return html`
    <section>
      <div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:8px;padding:.75rem 1rem;margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
          <div>
            <strong>💭 Agent 的内心独白</strong>
            <div style="font-size:.78rem;opacity:.7;margin-top:.2rem">
              ${thoughts.length} 段思考 · ${cycles.length} 次反思周期
            </div>
          </div>
          <button type="button" class="xmc-h-btn" onClick=${onRefresh}>
            ${loading ? "加载中…" : "刷新"}
          </button>
        </div>
        <div style="font-size:.72rem;opacity:.6;margin-top:.5rem;line-height:1.4">
          这是 ReflectionCycle (R1) 周期性产生的 agent 自言自语。每 5 分钟跑一次反思，
          让 LLM 看最近的事件给出 1-3 段「内心独白」。
          <strong>不需要用户对话也在思考。</strong>
        </div>
      </div>
      ${events.length === 0 ? html`
        <div style="opacity:.6;font-size:.9rem;padding:2rem;text-align:center;border:1px dashed var(--color-border);border-radius:8px">
          还没有内心独白 — daemon 重启后 5 分钟内出现第一条
        </div>
      ` : html`
        <div>
          ${events.map((ev) => ev.type === "inner_monologue"
            ? html`<${ThoughtRow} key=${ev.id || ev.ts} ev=${ev} />`
            : html`<${CycleRow} key=${ev.id || ev.ts} ev=${ev} />`)}
        </div>
      `}
    </section>
  `;
}
