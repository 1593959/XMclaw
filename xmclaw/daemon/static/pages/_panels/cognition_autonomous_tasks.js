// Epic #26 Phase C (2026-05-19): Autonomous Tasks panel.
//
// Renders the persistent plan history from /api/v2/cognition/plans.
// Pre-Phase-C autonomous work was invisible: HTN plans ran through
// the dispatcher and vanished — no audit trail, no "what was the
// agent doing 5 minutes ago?" view, no recovery signal from
// daemon restart. Now this panel shows plans-in-flight + recent
// completed/failed/orphaned plans with goal_id / step progress /
// cost spent / budget cap.
//
// Polls /api/v2/cognition/plans every 5s — cheap (sqlite query
// over a typically-tiny table) and gives near-live updates without
// needing a websocket pipe specifically for plans.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";

function _fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _fmtDuration(start, end) {
  if (!start) return "—";
  const ms = ((end || Date.now() / 1000) - start) * 1000;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60000)}m`;
}

function _statusTone(status) {
  return ({
    "executing": "info",
    "completed": "success",
    "failed": "danger",
    "budget_exceeded": "warning",
    "orphaned_at_restart": "warning",
  })[status] || "neutral";
}

function _statusLabel(status) {
  return ({
    "executing": "执行中",
    "completed": "已完成",
    "failed": "失败",
    "budget_exceeded": "预算超支",
    "orphaned_at_restart": "重启遗孤",
  })[status] || status;
}

export function AutonomousTasksPanel({ token }) {
  const [plans, setPlans] = useState(null);
  const [counts, setCounts] = useState({});
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState(null);  // null = all

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const qs = filter ? `?limit=50&status=${filter}` : "?limit=50";
        const d = await apiGet(`/api/v2/cognition/plans${qs}`, token);
        if (cancelled) return;
        setPlans(d.plans || []);
        setCounts(d.counts || {});
        setError(null);
      } catch (e) {
        if (cancelled) return;
        // Token-not-ready is a transient first-render state — don't
        // surface as a hard error.
        if (e && e.tokenNotReady) return;
        setError(String((e && e.message) || e));
      }
    };
    load();
    // Poll every 5s for near-live updates.
    const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [token, filter]);

  const totalPlans = Object.values(counts).reduce((a, b) => a + b, 0);

  return html`
    <div>
      <div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.8rem">
        <strong style="font-size:.95rem">${totalPlans} 个自主任务</strong>
        ${["executing", "completed", "failed", "budget_exceeded", "orphaned_at_restart"].map((s) => {
          if (!counts[s]) return null;
          const active = filter === s;
          return html`
            <button
              type="button"
              onClick=${() => setFilter(active ? null : s)}
              style=${
                "appearance:none;border:1px solid var(--color-border);"
                + "border-radius:6px;padding:.2rem .55rem;cursor:pointer;"
                + "font-size:.78rem;"
                + (active
                  ? "background:var(--color-primary);color:white"
                  : "background:transparent;color:var(--xmc-fg-muted)")
              }
            >
              ${_statusLabel(s)} ${counts[s]}
            </button>
          `;
        })}
        ${filter
          ? html`
            <button
              type="button"
              onClick=${() => setFilter(null)}
              style="appearance:none;border:none;background:none;cursor:pointer;color:var(--xmc-fg-muted);font-size:.78rem"
            >
              清除过滤
            </button>
          `
          : null}
      </div>
      ${error
        ? html`<div style="color:var(--xmc-danger);font-size:.85rem;margin-bottom:.5rem">⚠ ${error}</div>`
        : null}
      ${plans === null
        ? html`<div style="opacity:.6;font-size:.9rem">加载中...</div>`
        : plans.length === 0
          ? html`<div style="opacity:.6;font-size:.9rem">尚无自主任务记录${filter ? "（当前过滤下）" : ""}。Agent 跑自主 plan 后会出现在这里。</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:.5rem">
              ${plans.map((p) => {
                const tone = _statusTone(p.status);
                const toneColors = {
                  "info": "var(--xmc-accent, #3498db)",
                  "success": "#2ecc71",
                  "warning": "#f39c12",
                  "danger": "#e74c3c",
                  "neutral": "var(--xmc-fg-muted)",
                };
                const color = toneColors[tone];
                const pct = p.n_steps > 0 ? Math.round((p.n_completed / p.n_steps) * 100) : 0;
                return html`
                  <div key=${p.plan_id} style="border:1px solid ${color}44;border-left:3px solid ${color};border-radius:6px;padding:.55rem .75rem;background:${color}0c">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:.4rem;flex-wrap:wrap">
                      <span style="font-family:var(--xmc-mono, monospace);font-size:.82rem;opacity:.85">
                        ${p.plan_id}
                      </span>
                      <span style="font-size:.74rem;padding:.05rem .4rem;border-radius:4px;background:${color}22;color:${color};border:1px solid ${color}55">
                        ${_statusLabel(p.status)}
                      </span>
                    </div>
                    <div style="font-size:.78rem;opacity:.7;margin-top:.2rem">
                      goal: ${p.goal_id || "—"}
                      · 步骤 ${p.n_completed}/${p.n_steps} (${pct}%)
                      · 耗时 ${_fmtDuration(p.started_at, p.finished_at)}
                      ${p.spent_usd != null
                        ? html` · 花费 \$${(p.spent_usd || 0).toFixed(4)}${p.budget_usd ? ` / \$${p.budget_usd.toFixed(2)}` : ""}`
                        : null}
                    </div>
                    ${p.error
                      ? html`
                        <div style="font-size:.78rem;color:${color};margin-top:.3rem;background:${color}11;padding:.25rem .5rem;border-radius:4px;font-family:var(--xmc-mono, monospace)">
                          ${p.error}
                        </div>
                      `
                      : null}
                  </div>
                `;
              })}
            </div>
          `}
    </div>
  `;
}
